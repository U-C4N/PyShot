"""One native recorder per broker, with immutable recording IDs and ownership."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .capture import resolve_target
from .errors import PyShotError
from .output import shots_dir, unique_path


def helper_path():
    roots = [Path(sys.executable).parent] if getattr(sys, "frozen", False) else []
    roots += [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])),
              Path(__file__).resolve().parents[1] / "native" / "pyshot-recorder" / "target" / "release"]
    for root in roots:
        path = root / "pyshot-recorder.exe"
        if path.is_file():
            return path
    raise PyShotError("recorder_unavailable", "Native recorder is missing. Install the complete PyShot bundle.")


def helper_query(*args, timeout=30):
    try:
        result = subprocess.run([str(helper_path()), *args], capture_output=True, text=True,
                                encoding="utf-8", timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise PyShotError("native_error", result.stderr.strip() or result.stdout.strip() or "Native helper failed")
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PyShotError("native_error", str(exc)) from exc


class RecordingController:
    def __init__(self):
        self._lock = threading.RLock()
        self._records = {}
        self._active = None
        self._closed = False

    def start(self, owner, target, audio="none", microphone_id=None, system_audio_id=None,
              duration_seconds=None):
        if audio not in ("none", "microphone", "system", "both"):
            raise PyShotError("invalid_audio", "Audio must be none, microphone, system or both")
        if duration_seconds is not None and (isinstance(duration_seconds, bool)
                or not isinstance(duration_seconds, (int, float))
                or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 86400):
            raise PyShotError("invalid_duration", "Duration must be between 0 and 86400 seconds")
        resolved = resolve_target(target)
        executable = helper_path()
        with self._lock:
            if self._closed:
                raise PyShotError("shutting_down", "PyShot is shutting down")
            if self._active:
                raise PyShotError("recording_active", "A recording is already active",
                                  {"recording_id": self._active})
            ident = uuid.uuid4().hex
            path = unique_path(shots_dir() / "Videos", ".mp4")
            partial = path.with_suffix(".partial.mp4")
            record = {"recording_id": ident, "status": "starting", "path": str(path),
                      "partial_path": str(partial), "duration_seconds": 0.0, "frames": 0,
                      "audio": audio, "target": resolved, "fps": 30, "interrupted": None,
                      "error": None, "_owner": owner, "_ready": threading.Event(),
                      "_done": threading.Event(), "_started_at": time.monotonic()}
            config = {"target": resolved, "output_path": str(partial), "fps": 30, "audio": audio,
                      "microphone_id": microphone_id, "system_audio_id": system_audio_id,
                      "duration_seconds": duration_seconds}
            try:
                process = subprocess.Popen([str(executable), "record"], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           encoding="utf-8", bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW)
                record["_process"] = process
                self._records[ident] = record
                self._active = ident
                process.stdin.write(json.dumps(config) + "\n")
                process.stdin.flush()
            except OSError as exc:
                self._active = None
                if "_process" in record:
                    record["_process"].terminate()
                raise PyShotError("recorder_start_failed", str(exc)) from exc
            threading.Thread(target=self._read, args=(record,), daemon=True).start()
        if not record["_ready"].wait(20):
            try:
                self.stop(ident)
            except PyShotError:
                # A helper that never announced readiness cannot own the session
                # indefinitely. Keep any partial file for diagnosis.
                process.terminate()
                record["_done"].wait(5)
            raise PyShotError("recorder_timeout", "Recorder did not start within 20 seconds")
        result = self.status(ident)
        if result["error"]:
            raise PyShotError("recorder_start_failed", result["error"], result)
        return result

    def _read(self, record):
        process = record["_process"]
        errors = []
        def drain():
            for line in process.stderr:
                errors.append(line.rstrip())
                del errors[:-30]
        stderr_thread = threading.Thread(target=drain, daemon=True)
        stderr_thread.start()
        completed = False
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    errors.append("Invalid recorder response: " + line[:200])
                    continue
                with self._lock:
                    kind = event.pop("event", None)
                    if kind in ("started", "progress", "completed"):
                        record.update({k: v for k, v in event.items() if k in (
                            "width", "height", "duration_seconds", "frames", "interrupted")})
                    if kind == "started":
                        if record["status"] == "starting":
                            record["status"] = "recording"
                        record["_ready"].set()
                    elif kind == "completed":
                        completed = True
                    elif kind == "error":
                        record["error"] = event.get("message", "Native recorder failed")
            returncode = process.wait()
            stderr_thread.join(2)
            with self._lock:
                if completed and returncode == 0 and Path(record["partial_path"]).is_file():
                    os.replace(record["partial_path"], record["path"])
                    record["status"] = "completed"
                else:
                    record["status"] = "failed"
                    record["error"] = record["error"] or "\n".join(errors) or f"Recorder exited ({returncode}) before finalizing"
        except Exception as exc:
            with self._lock:
                record["status"] = "failed"
                record["error"] = str(exc)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            with self._lock:
                if self._active == record["recording_id"]:
                    self._active = None
                record["_ready"].set()
                record["_done"].set()

    def status(self, recording_id=None):
        with self._lock:
            ident = recording_id or self._active
            if ident is None:
                return {"status": "idle", "recording_id": None}
            if ident not in self._records:
                raise PyShotError("unknown_recording", "Unknown recording ID")
            record = self._records[ident]
            result = {k: v for k, v in record.items() if not k.startswith("_")}
            if record["status"] in ("recording", "stopping"):
                result["elapsed_seconds"] = round(time.monotonic()-record["_started_at"], 3)
            if record["status"] != "completed":
                result.pop("path", None)
            return result

    def stop(self, recording_id):
        with self._lock:
            self.status(recording_id)
            record = self._records[recording_id]
            if record["status"] in ("starting", "recording"):
                record["status"] = "stopping"
                try:
                    record["_process"].stdin.write('{"op":"stop"}\n')
                    record["_process"].stdin.flush()
                except (OSError, ValueError):
                    pass  # Reader reports helper exit and preserves partial files.
        if not record["_done"].wait(30):
            raise PyShotError("finalizing", "Recording is still finalizing; poll its status",
                              {"recording_id": recording_id})
        return self.status(recording_id)

    def disconnect(self, owner):
        with self._lock:
            ident = self._active
            if ident and self._records[ident]["_owner"] != owner:
                ident = None
        if ident:
            self.stop(ident)

    def close(self):
        with self._lock:
            self._closed = True
            ident = self._active
        if ident:
            try:
                self.stop(ident)
            except PyShotError as exc:
                if exc.code != "finalizing":
                    raise
                # GUI shutdown runs on a worker. Preserve the reader until the
                # native file is finalized and its atomic rename finishes.
                self._records[ident]["_done"].wait()

    def frames(self, recording_id, timestamps_seconds):
        record = self.status(recording_id)
        if record["status"] != "completed":
            raise PyShotError("not_completed", "Frames require a completed recording")
        if not isinstance(timestamps_seconds, list) or not 1 <= len(timestamps_seconds) <= 8:
            raise PyShotError("invalid_timestamps", "Request one to eight timestamps")
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t)
               or t < 0 or t >= record["duration_seconds"] for t in timestamps_seconds):
            raise PyShotError("invalid_timestamps", "Timestamps must lie within the recording")
        directory = Path(record["path"]).with_suffix("")
        directory.mkdir(exist_ok=True)
        result = helper_query("frames", "--path", record["path"], "--timestamps",
                              json.dumps(timestamps_seconds), "--output-dir", str(directory), timeout=60)
        return {**result, "recording_id": recording_id}
