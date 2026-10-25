"""Exercise the controller against a real child process speaking helper JSONL."""
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pyshot_core import recording
from pyshot_core.errors import PyShotError


@pytest.fixture
def controller(tmp_path, monkeypatch):
    helper = tmp_path / "recorder.py"
    helper.write_text('''import sys, json, time
from pathlib import Path
config = json.loads(sys.stdin.readline())
Path(config['output_path']).write_bytes(b'fake-mp4')
print(json.dumps(dict(event='started',width=640,height=480)), flush=True)
started = time.monotonic()
sys.stdin.readline()
print(json.dumps(dict(event='completed',frames=1,duration_seconds=time.monotonic()-started,width=640,height=480,interrupted=None)),flush=True)
''', encoding="utf-8")
    original = subprocess.Popen
    def launch(*args, **kwargs):
        return original([sys.executable, str(helper)], **kwargs)
    monkeypatch.setattr(recording.subprocess, "Popen", launch)
    monkeypatch.setattr(recording, "helper_path", lambda: helper)
    monkeypatch.setattr(recording, "shots_dir", lambda: tmp_path)
    monkeypatch.setattr(recording, "resolve_target", lambda t: t)
    result = recording.RecordingController()
    yield result
    result.close()


def test_single_recording_and_owner_disconnect(controller):
    first = controller.start("one", {"kind": "monitor"})
    with pytest.raises(PyShotError) as exc:
        controller.start("two", {"kind": "monitor"})
    assert exc.value.details["recording_id"] == first["recording_id"]
    controller.disconnect("two")
    assert controller.status()["status"] == "recording"
    controller.disconnect("one")
    final = controller.status(first["recording_id"])
    assert final["status"] == "completed"
    assert Path(final["path"]).exists()
    assert not Path(final["partial_path"]).exists()


def test_old_id_cannot_stop_new_recording(controller):
    first = controller.start("one", {})
    stopped = controller.stop(first["recording_id"])
    second = controller.start("two", {})
    assert controller.stop(first["recording_id"]) == stopped
    assert controller.status()["recording_id"] == second["recording_id"]


def test_twenty_start_stop_cycles(controller):
    paths = set()
    for _ in range(20):
        start = controller.start("one", {})
        result = controller.stop(start["recording_id"])
        assert result["status"] == "completed"
        assert result["path"] not in paths
        paths.add(result["path"])


def test_simultaneous_stop_is_idempotent(controller):
    start = controller.start("one", {})
    results = []
    threads = [threading.Thread(target=lambda: results.append(controller.stop(start["recording_id"]))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(results) == 2 and results[0] == results[1]


def test_frame_timestamps_validate_before_native_call(controller):
    start = controller.start("one", {})
    with pytest.raises(PyShotError, match="completed"):
        controller.frames(start["recording_id"], [0])
    controller.stop(start["recording_id"])
    with pytest.raises(PyShotError, match="eight"):
        controller.frames(start["recording_id"], [0]*9)
    with pytest.raises(PyShotError, match="within"):
        controller.frames(start["recording_id"], [-1])


def test_shutdown_rejects_inflight_start(controller, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def resolve(target):
        entered.set()
        assert release.wait(3)
        return target
    monkeypatch.setattr(recording, "resolve_target", resolve)
    errors = []
    def start():
        try:
            controller.start("one", {})
        except PyShotError as exc:
            errors.append(exc.code)
    thread = threading.Thread(target=start)
    thread.start()
    assert entered.wait(3)
    controller.close()
    release.set()
    thread.join(3)
    assert errors == ["shutting_down"]
    assert controller.status()["status"] == "idle"


def test_shutdown_waits_through_slow_finalization(monkeypatch):
    controller = recording.RecordingController()
    done = threading.Event()
    controller._active = "slow"
    controller._records["slow"] = {"_done": done}
    def stop(_):
        raise PyShotError("finalizing", "Still writing")
    monkeypatch.setattr(controller, "stop", stop)
    closed = threading.Event()
    thread = threading.Thread(target=lambda: (controller.close(), closed.set()))
    thread.start()
    assert not closed.wait(0.05)
    done.set()
    thread.join(3)
    assert closed.is_set()
