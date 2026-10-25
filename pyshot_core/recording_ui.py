"""Tk selection and recording controls; all native work runs off the Tk thread."""
import ctypes
from ctypes import wintypes
import sys
import threading
import tkinter as tk
from tkinter import ttk

from .capture import list_displays, list_windows


class RecordingUI:
    def __init__(self, app, service, events):
        self.app = app
        self.root = app.root
        self.service = service
        self.events = events
        self.dialog = None
        self.busy = False

    def _work(self, operation, done):
        self.busy = True
        def run():
            try:
                result = operation()
            except Exception as exc:
                result = exc
            self.events.put(("callback", lambda: finish(result)))
        def finish(result):
            self.busy = False
            if isinstance(result, Exception):
                self.app._toast(f"Recording: {result}", ms=9000)
            else:
                done(result)
        threading.Thread(target=run, daemon=True).start()

    def toggle(self):
        if self.busy:
            return
        status = self.service.recordings.status()
        if status["status"] != "idle":
            self.app._toast("Finalizing recording…")
            self._work(lambda: self.service.recordings.stop(status["recording_id"]), self._stopped)
            return
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        if self.app.active:
            return
        self._select()

    def _select(self):
        from tkinter import messagebox
        displays = list_displays()
        windows = list_windows()
        self.dialog = dialog = tk.Toplevel(self.root)
        dialog.title("PyShot — Record video")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        kind = tk.StringVar(value="region")
        audio = tk.StringVar(value="none")
        ttk.Label(frame, text="Record at 30 FPS · Ctrl+Shift+R to stop").grid(row=0, columnspan=2, pady=(0, 12))
        ttk.Label(frame, text="Target").grid(row=1, column=0, sticky="w")
        mode = ttk.Combobox(frame, textvariable=kind, values=("region", "monitor", "window"), state="readonly", width=36)
        mode.grid(row=1, column=1, pady=4)
        target = ttk.Combobox(frame, state="readonly", width=36)
        target.grid(row=2, column=1, pady=4)
        def update(*_):
            if kind.get() == "window":
                target["values"] = [f'{w["title"][:45]} ({w["window_id"]})' for w in windows]
            else:
                target["values"] = [f'{m["monitor_id"]} — {m["width"]}×{m["height"]}' for m in displays]
            if target["values"]:
                target.current(0)
        mode.bind("<<ComboboxSelected>>", update)
        update()
        ttk.Label(frame, text="Audio").grid(row=3, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=audio, values=("none", "microphone", "system", "both"),
                     state="readonly", width=36).grid(row=3, column=1, pady=4)
        ttk.Label(frame, text="Audio uses the default Windows devices.").grid(row=4, columnspan=2, pady=4)
        def start():
            index = target.current()
            if index < 0:
                messagebox.showerror("PyShot", "No available target", parent=dialog)
                return
            selected_kind, selected_audio = kind.get(), audio.get()
            selected = windows[index] if selected_kind == "window" else displays[index]
            dialog.destroy()
            self.dialog = None
            if selected_kind == "region":
                self.root.after(150, lambda: self.app.start_capture(
                    selection_callback=lambda rect: self._start({"kind": "region", "region": rect}, selected_audio),
                    monitor=selected))
            else:
                spec = {"kind": selected_kind,
                        "window_id" if selected_kind == "window" else "monitor_id":
                        selected["window_id" if selected_kind == "window" else "monitor_id"]}
                self.root.after(150, lambda: self._start(spec, selected_audio))
        ttk.Button(frame, text="Select region / Start recording", command=start).grid(row=5, columnspan=2, pady=(12, 0))

    def _start(self, target, audio):
        self.app._kill_toast()
        self._work(lambda: self.service.recordings.start("gui", target, audio), self._started)

    def _started(self, result):
        # Older Windows versions cannot exclude a window from capture.
        if sys.getwindowsversion().build >= 19041:
            self.app._toast("● Recording — Ctrl+Shift+R to stop", ms=4000)
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            hwnd = user32.GetParent(self.app.toast.winfo_id())
            if not user32.SetWindowDisplayAffinity(hwnd, 0x11):
                self.app._kill_toast()
        self._watch(result["recording_id"])

    def _watch(self, ident):
        state = self.service.recordings.status(ident)
        if state["status"] in ("completed", "failed"):
            if not self.busy:
                self._stopped(state)
        else:
            self.root.after(1000, lambda: self._watch(ident))

    def _stopped(self, result):
        from pathlib import Path
        if result.get("error"):
            self.app._toast(f'Recording failed: {result["error"]}', ms=9000)
        else:
            reason = result.get("interrupted")
            message = f'Video saved ({result["duration_seconds"]:.1f}s)'
            if reason:
                message += f" — interrupted: {reason}"
            self.app._toast(message, path=Path(result["path"]), ms=8000)
