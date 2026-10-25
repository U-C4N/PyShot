"""Opt-in Windows integration test; captures only this script's synthetic window.

Run from an interactive desktop after building the Rust helper. No microphone
or system audio device is opened. Outputs remain in a temporary test directory.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import argparse
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import tkinter as tk

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "native/pyshot-recorder/target/release/pyshot-recorder.exe"


def invoke(*args):
    result = subprocess.run([str(HELPER), *map(str, args)], capture_output=True,
                            text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def record(window, target, output, duration=None, manual_stop=False, change_size=False,
           stop_after=1, close_target=None, expected_interruption=None):
    child = subprocess.Popen([str(HELPER), "record"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    events = queue.Queue()
    def reader():
        for line in child.stdout:
            events.put(json.loads(line))
    threading.Thread(target=reader, daemon=True).start()
    child.stdin.write(json.dumps({"target": target, "output_path": str(output),
                                 "fps": 30, "audio": "none", "duration_seconds": duration}) + "\n")
    child.stdin.flush()
    deadline = time.monotonic() + (duration or 0) + 20
    started = None
    start_time = None
    stopped = moved = resized = False
    stopped_at = None
    try:
        while time.monotonic() < deadline:
            window.update()
            while not events.empty():
                event = events.get()
                if event["event"] == "error":
                    raise AssertionError(event)
                if event["event"] == "started":
                    started = event
                    start_time = time.monotonic()
                if event["event"] == "completed":
                    child.wait(timeout=5)
                    assert child.returncode == 0
                    assert event["interrupted"] == expected_interruption, event
                    assert (event["width"], event["height"]) == (started["width"], started["height"])
                    if stopped_at is not None:
                        event["stop_latency_seconds"] = time.monotonic() - stopped_at
                    return event
            if start_time:
                elapsed = time.monotonic() - start_time
                if change_size and elapsed > .7 and not moved:
                    window.geometry("319x239+130+140")
                    moved = True
                if change_size and elapsed > 1.4 and not resized:
                    window.geometry("401x301+130+140")
                    resized = True
                if manual_stop and elapsed > stop_after and not stopped:
                    stopped_at = time.monotonic()
                    if close_target is not None:
                        close_target.destroy()
                    else:
                        child.stdin.write('{"op":"stop"}\n')
                        child.stdin.flush()
                    stopped = True
            if child.poll() is not None and events.empty():
                raise AssertionError(child.stderr.read())
            time.sleep(.005)
        raise AssertionError("Native recorder did not stop within 20 seconds")
    finally:
        if child.poll() is None:
            try:
                child.stdin.write('{"op":"stop"}\n')
                child.stdin.flush()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
                child.wait(timeout=5)
        child.stdin.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stress", action="store_true", help="Also run a 60-second static recording and 20 start/stop cycles")
    parser.add_argument("--cycles-only", action="store_true", help="Run 20 short start/stop cycles only")
    args = parser.parse_args()
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    ctypes.windll.user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ctypes.windll.user32.GetAncestor.restype = ctypes.c_void_p
    directory = Path(tempfile.mkdtemp(prefix="pyshot-native-video-test-"))
    window = tk.Tk()
    window.title("PyShot synthetic video integration test")
    window.geometry("319x239+80+90")
    window.configure(background="#ff0000")
    bottom = tk.Frame(window, background="#0000ff")
    bottom.place(relx=0, rely=.5, relwidth=1, relheight=.5)
    window.update()
    target = {"kind": "window", "hwnd": ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)}
    try:
        if args.cycles_only:
            cycles = [record(window, target, directory / f"cycle_{i:02}.mp4", manual_stop=True,
                             stop_after=.1) for i in range(20)]
            assert all(c["stop_latency_seconds"] < 2 for c in cycles), cycles
            print(json.dumps({"ok": True, "directory": str(directory), "cycles": len(cycles),
                              "max_stop_latency_seconds": max(c["stop_latency_seconds"] for c in cycles)}))
            return
        # This second test window obscures the target; HWND capture must still see red.
        cover = tk.Toplevel(window)
        cover.title("PyShot overlap test")
        cover.geometry("250x180+100+110")
        cover.configure(background="#00ff00")
        cover.update()
        shot = invoke("screenshot", "--target", json.dumps(target), "--output", directory / "window.png")
        shot_image = Image.open(shot["path"]).convert("RGB")
        r, g, b = shot_image.getpixel((shot_image.width // 2, shot_image.height // 3))
        assert r > 200 and g < 40 and b < 40, (r, g, b)
        cover.destroy()
        window.lift()
        # Region capture reads visible monitor pixels, so keep this small synthetic
        # window unobscured while the region test runs and wait for composition.
        window.attributes("-topmost", True)
        window.update()
        ctypes.windll.dwmapi.DwmFlush()
        ctypes.windll.user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ctypes.windll.user32.MonitorFromWindow.restype = ctypes.c_void_p
        ctypes.windll.user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
        client_origin = wintypes.POINT(0, 0)
        assert ctypes.windll.user32.ClientToScreen(target["hwnd"], ctypes.byref(client_origin))
        region = {"kind": "region", "hmonitor": ctypes.windll.user32.MonitorFromWindow(target["hwnd"], 2),
                  "x": client_origin.x + 40, "y": client_origin.y + 40, "width": 127, "height": 75}
        region_video = record(window, region, directory / "region.mp4", duration=.3)
        window.attributes("-topmost", False)
        assert (region_video["width"], region_video["height"]) == (128, 76), region_video
        region_frame = invoke("frames", "--path", directory / "region.mp4", "--timestamps", "[0]",
                              "--output-dir", directory / "region-frame")
        r, g, b = Image.open(region_frame["frames"][0]["path"]).getpixel((30, 30))
        assert r > 200 and g < 40 and b < 40, ((r, g, b), region, str(directory))
        moving = record(window, target, directory / "moving.mp4", duration=3, change_size=True)
        assert 2.8 <= moving["duration_seconds"] <= 3.15, moving
        assert moving["frames"] >= 65, moving
        static = record(window, target, directory / "static.mp4", manual_stop=True)
        assert .8 <= static["duration_seconds"] <= 1.5, static
        assert static["frames"] >= 25, static
        extracted = invoke("frames", "--path", directory / "moving.mp4",
                           "--timestamps", "[0,1,2.8]", "--output-dir", directory / "frames")
        assert len(extracted["frames"]) == 3
        assert all((frame["width"], frame["height"]) == (moving["width"], moving["height"])
                   for frame in extracted["frames"])
        results = {"ok": True, "directory": str(directory), "region": region_video, "moving": moving, "static": static}
        immediate = record(window, target, directory / "immediate.mp4", manual_stop=True, stop_after=0)
        assert immediate["stop_latency_seconds"] < 2, immediate
        results["immediate"] = immediate
        closing = tk.Toplevel(window)
        closing.title("PyShot target-closed test")
        closing.geometry("200x150+500+120")
        closing.configure(background="#ff00ff")
        window.update()
        closing_target = {"kind": "window", "hwnd": ctypes.windll.user32.GetAncestor(closing.winfo_id(), 2)}
        results["closed"] = record(window, closing_target, directory / "closed.mp4", manual_stop=True,
                                   stop_after=.3, close_target=closing, expected_interruption="target_closed")
        if args.stress:
            long_static = record(window, target, directory / "long_static.mp4", duration=60)
            assert 59.8 <= long_static["duration_seconds"] <= 60.15, long_static
            assert long_static["frames"] >= 1750, long_static
            results["long_static"] = long_static
            cycles = [record(window, target, directory / f"cycle_{i:02}.mp4", manual_stop=True,
                             stop_after=.1) for i in range(20)]
            assert all(c["stop_latency_seconds"] < 2 for c in cycles), cycles
            results["cycles"] = {"completed": len(cycles), "max_stop_latency_seconds": max(c["stop_latency_seconds"] for c in cycles)}
        print(json.dumps(results))
    finally:
        window.destroy()


if __name__ == "__main__":
    main()
