"""Opt-in packaged GUI/MCP integration; captures only an owned test window.

Run after building the bundle and closing any older PyShot process normally.
The subprocess PATH has no Python, Rust or FFmpeg. This is not a clean-VM test.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tkinter as tk


class MCP:
    def __init__(self, executable, env, window):
        self.window = window
        self.counter = 0
        self.messages = queue.Queue()
        self.process = subprocess.Popen([str(executable), "serve"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        self.send("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "pyshot-package-test", "version": "1"}})
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.process.stdin.flush()

    def send(self, method, params):
        self.counter += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            self.window.update()
            try:
                result = self.messages.get(timeout=0.02)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise AssertionError(self.process.stderr.read())
                continue
            if result.get("id") == self.counter:
                assert "error" not in result, result
                return result["result"]
        raise TimeoutError(method)

    def tool(self, name, params=None):
        result = self.send("tools/call", {"name": name, "arguments": params or {}})
        assert not result.get("isError"), result
        return result

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=20)
        assert self.process.returncode == 0, self.process.stderr.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path("dist/PyShot-3.0.0-win-x64"))
    options = parser.parse_args()
    executable = (options.bundle / "pyshot-mcp.exe").resolve()
    env = dict(os.environ)
    windows_directory = os.environ["SystemRoot"]
    env["PATH"] = os.path.join(windows_directory, "System32") + ";" + windows_directory
    env.pop("PYTHONPATH", None)
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    root = tk.Tk()
    root.title("PyShot packaged integration target")
    root.geometry("320x240+150+150")
    root.configure(background="#ef3030")
    root.update()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    target = {"kind": "window", "window_id": str(hwnd)}
    first = MCP(executable, env, root)
    second = MCP(executable, env, root)
    report = {"environment": "packaged processes with system-only PATH", "target": target}
    try:
        report["capabilities"] = first.tool("get_capabilities")["structuredContent"]
        shot = first.tool("capture_screenshot", {"target": target})
        assert any(block["type"] == "image" for block in shot["content"])
        report["screenshot"] = shot["structuredContent"]
        start = first.tool("recording_start", {"target": target})["structuredContent"]
        ident = start["recording_id"]
        other = second.tool("recording_status")["structuredContent"]
        assert other["recording_id"] == ident
        conflict = second.send("tools/call", {"name": "recording_start", "arguments": {"target": target}})
        assert conflict["isError"] and conflict["structuredContent"]["error"]["code"] == "recording_active"
        until = time.monotonic() + 1
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        stop = second.tool("recording_stop", {"recording_id": ident})["structuredContent"]
        assert stop["status"] == "completed", stop
        report["recording"] = stop
        frames = second.tool("recording_frames", {"recording_id": ident, "timestamps_seconds": [0, 0.5]})
        assert sum(block["type"] == "image" for block in frames["content"]) == 2
        report["frames"] = frames["structuredContent"]
        newer = first.tool("recording_start", {"target": target})["structuredContent"]
        repeated = second.tool("recording_stop", {"recording_id": ident})["structuredContent"]
        assert repeated == stop
        assert second.tool("recording_status")["structuredContent"]["recording_id"] == newer["recording_id"]
        first.close()
        first = None
        until = time.monotonic() + 15
        while time.monotonic() < until:
            state = second.tool("recording_status", {"recording_id": newer["recording_id"]})["structuredContent"]
            if state["status"] == "completed":
                break
            time.sleep(0.05)
        assert state["status"] == "completed", state
        report["owner_disconnect"] = state
        report["passed"] = True
        output = Path("build/package-test-report.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"passed": True, "report": str(output.resolve())}))
    finally:
        if first:
            first.close()
        second.close()
        root.destroy()


if __name__ == "__main__":
    main()
