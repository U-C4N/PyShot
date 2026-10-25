"""Opt-in Windows hardware check: visible test window + WASAPI recording.

Run with Python and the built helper, for example:
    python scripts/test_native_audio.py --seconds 3
    python scripts/test_native_audio.py --seconds 1800 --modes both

This intentionally records the selected real audio endpoints. It does not change
device settings or play sounds. Outputs remain in the reported temporary folder.
No FFmpeg executable or Python video decoder is required.
"""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time
import tkinter as tk


def _atoms(stream, start, end):
    position = start
    while position + 8 <= end:
        stream.seek(position)
        length, kind = struct.unpack(">I4s", stream.read(8))
        header = 8
        if length == 1:
            length = struct.unpack(">Q", stream.read(8))[0]
            header = 16
        elif length == 0:
            length = end - position
        if length < header or position + length > end:
            raise ValueError("Invalid MP4 atom boundary")
        yield kind, position + header, length - header
        position += length


def mp4_durations(path: Path) -> dict:
    """Read ISO BMFF track times without decoding or loading the mdat payload."""
    with path.open("rb") as stream:
        top = list(_atoms(stream, 0, path.stat().st_size))
        moov = next(atom for atom in top if atom[0] == b"moov")
        children = list(_atoms(stream, moov[1], moov[1] + moov[2]))
        mvhd = next(atom for atom in children if atom[0] == b"mvhd")
        stream.seek(mvhd[1])
        header = stream.read(32)
        scale_at = 20 if header[0] else 12
        movie_scale = struct.unpack_from(">I", header, scale_at)[0]
        result = {}
        for _, offset, length in (atom for atom in children if atom[0] == b"trak"):
            track = list(_atoms(stream, offset, offset + length))
            tkhd = next(atom for atom in track if atom[0] == b"tkhd")
            stream.seek(tkhd[1])
            header = stream.read(40)
            duration = struct.unpack_from(">Q" if header[0] else ">I", header,
                                          28 if header[0] else 20)[0]
            mdia = next(atom for atom in track if atom[0] == b"mdia")
            media = list(_atoms(stream, mdia[1], mdia[1] + mdia[2]))
            hdlr = next(atom for atom in media if atom[0] == b"hdlr")
            stream.seek(hdlr[1] + 8)
            kind = stream.read(4).decode("ascii")
            mdhd = next(atom for atom in media if atom[0] == b"mdhd")
            stream.seek(mdhd[1])
            header = stream.read(36)
            scale_at = 20 if header[0] else 12
            scale = struct.unpack_from(">I", header, scale_at)[0]
            media_duration = struct.unpack_from(">Q" if header[0] else ">I", header,
                                                scale_at + 4)[0]
            result[kind] = {"duration_seconds": duration / movie_scale,
                            "media_duration_seconds": media_duration / scale,
                            "timescale": scale}
        if "vide" in result and "soun" in result:
            result["av_duration_gap_ms"] = abs(result["vide"]["duration_seconds"] -
                                                result["soun"]["duration_seconds"]) * 1000
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, default=Path(__file__).resolve().parents[1] /
                        "native/pyshot-recorder/target/release/pyshot-recorder.exe")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--modes", default="microphone,system,both")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not args.helper.is_file():
        parser.error("Build pyshot-recorder.exe first")
    modes = args.modes.split(",")
    if any(mode not in {"none", "microphone", "system", "both"} for mode in modes):
        parser.error("Unknown audio mode")
    if not 0.2 <= args.seconds <= 1800:
        parser.error("Duration must be between 0.2 and 1800 seconds")
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="pyshot-audio-check-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    root = tk.Tk()
    root.title("PyShot native audio test")
    root.geometry("322x242+80+80")
    canvas = tk.Canvas(root, width=322, height=242, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_rectangle(0, 0, 322, 121, fill="#bf3030", outline="")
    canvas.create_rectangle(0, 121, 322, 242, fill="#3050bf", outline="")
    label = canvas.create_text(161, 121, text="PyShot audio test", fill="white")
    root.update()
    ctypes.windll.user32.GetAncestor.restype = ctypes.c_void_p
    hwnd = ctypes.windll.user32.GetAncestor(ctypes.c_void_p(root.winfo_id()), 2)
    results = []
    done = threading.Event()

    def record_modes():
        try:
            for mode in modes:
                path = output_dir / f"{mode}.mp4"
                result = {"mode": mode, "path": str(path)}
                started = time.monotonic()
                try:
                    with (output_dir / f"{mode}.stdout.jsonl").open("w+", encoding="utf-8") as out, \
                         (output_dir / f"{mode}.stderr.log").open("w+", encoding="utf-8") as err:
                        process = subprocess.Popen([str(args.helper), "record"], stdin=subprocess.PIPE,
                                                   stdout=out, stderr=err, text=True,
                                                   creationflags=subprocess.CREATE_NO_WINDOW)
                        try:
                            request = {"target": {"kind": "window", "hwnd": hwnd},
                                       "output_path": str(path), "audio": mode, "fps": 30,
                                       "duration_seconds": args.seconds}
                            process.stdin.write(json.dumps(request) + "\n")
                            process.stdin.flush()
                            process.wait(timeout=args.seconds + 30)
                        except Exception:
                            process.kill()
                            process.wait()
                            raise
                        finally:
                            process.stdin.close()
                        out.seek(0)
                        events = [json.loads(line) for line in out if line.strip()]
                        result.update(returncode=process.returncode, events=events)
                        err.seek(0)
                        result["stderr"] = err.read()
                    completed = next((event for event in events if event.get("event") == "completed"), None)
                    if process.returncode != 0 or completed is None:
                        raise RuntimeError("Recorder did not complete")
                    result["tracks"] = mp4_durations(path)
                    if mode != "none" and "soun" not in result["tracks"]:
                        raise RuntimeError("Audio track missing")
                    if result["tracks"].get("av_duration_gap_ms", 0) >= 100:
                        raise RuntimeError("Audio/video track duration differs by at least 100 ms")
                    if abs(completed["duration_seconds"] - args.seconds) > 0.15:
                        raise RuntimeError("Recorded duration differs from requested duration")
                    decoded = subprocess.run([str(args.helper), "frames", "--path", str(path),
                                              "--timestamps", json.dumps([0.0, args.seconds / 2]),
                                              "--output-dir", str(output_dir / mode)],
                                             capture_output=True, text=True, timeout=30,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                    if decoded.returncode:
                        raise RuntimeError(f"Frame decoding failed: {decoded.stdout} {decoded.stderr}")
                    result["decoded"] = json.loads(decoded.stdout)
                    result["ok"] = True
                except Exception as error:
                    result.update(ok=False, error=str(error))
                result["wall_seconds"] = time.monotonic() - started
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            done.set()

    def tick():
        if done.is_set():
            root.destroy()
            return
        canvas.itemconfigure(label, text=f"PyShot audio test\n{time.monotonic():.1f}")
        root.after(100, tick)

    threading.Thread(target=record_modes, daemon=True).start()
    root.after(100, tick)
    root.mainloop()
    report = {"output_dir": str(output_dir), "results": results,
              "ok": len(results) == len(modes) and all(result["ok"] for result in results)}
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"report": str(output_dir / "report.json"), "ok": report["ok"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
