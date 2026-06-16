import ctypes
def _set_dpi_awareness():
    try:  # Windows 10 1703+ : Per-Monitor v2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1+ : Per-Monitor
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:  # Vista+ : System aware
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


_set_dpi_awareness()

import io
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import mss               # raw, lossless screen capture
import win32clipboard    # clipboard (CF_DIB + PNG)
from PIL import Image, ImageEnhance, ImageTk

HOTKEY_CAPTURE = "ctrl+shift+h"
HOTKEY_QUIT = "ctrl+shift+q"
POLL_MS = 40       # poll interval for the hotkey queue (ms)
RENDER_MS = 15     # refresh interval of the selection drawing (caps at ~60 fps)
DIM = 0.35         # dim level for the area outside the selection
MIN_SEL = 3        # px; a selection smaller than this = accidental click → cancel
LOG_FILE = Path(tempfile.gettempdir()) / "pyshot.log"

# Hotkeys are captured on a SEPARATE thread (the RegisterHotKey message loop);
# touching tkinter from that thread would crash. That thread only drops events
# onto this queue; the main (tkinter) loop reads the queue via root.after and
# does the work itself.
events = queue.Queue()


def _log_error():
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now()} ---\n")
            f.write(traceback.format_exc())
    except OSError:
        pass


def _log_msg(text):
    # For logging a plain message when there is no exception (e.g. RegisterHotKey failed)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now()} ---\n{text}\n")
    except OSError:
        pass


MUTEX_NAME = "PyShot_SingleInstance"   # running-copy detection
QUIT_EVENT_NAME = "PyShot_Quit"        # quit signal from --debug to the running copy

_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_K32.CreateMutexW.restype = ctypes.c_void_p
_K32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
_K32.CreateEventW.restype = ctypes.c_void_p
_K32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
_K32.OpenMutexW.restype = ctypes.c_void_p
_K32.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
_K32.OpenEventW.restype = ctypes.c_void_p
_K32.OpenEventW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
_K32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_K32.SetEvent.argtypes = [ctypes.c_void_p]
_K32.CloseHandle.argtypes = [ctypes.c_void_p]
_K32.GetCurrentThreadId.restype = wintypes.DWORD


# ---------------------------------------------------------------------------
# Global hotkeys — Windows NATIVE RegisterHotKey API.
#
# Why NOT the 'keyboard' library: that library installs a system-wide low-level
# keyboard hook (WH_KEYBOARD_LL) that captures and re-emits EVERY key event.
# That corrupts the state/timing of modifiers like Ctrl/Shift/Alt, making
# operations such as Ctrl+click, Ctrl+scroll-zoom, Ctrl+drag unreliable in
# OTHER applications (a known side effect of that library).
#
# RegisterHotKey, by contrast, registers only the EXACT combination
# (Ctrl+Shift+H) at the operating-system level and installs NO global hook → it
# never touches any other key usage. Since WM_HOTKEY is delivered only to the
# message queue of the thread that did the registration, the registration +
# GetMessage loop runs on a dedicated thread and drops events onto the shared
# 'events' queue (without ever touching tkinter).
# ---------------------------------------------------------------------------
_U32 = ctypes.WinDLL("user32", use_last_error=True)
_U32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_U32.RegisterHotKey.restype = wintypes.BOOL
_U32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_U32.UnregisterHotKey.restype = wintypes.BOOL
_U32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
_U32.GetMessageW.restype = ctypes.c_int
_U32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
_U32.PeekMessageW.restype = wintypes.BOOL
_U32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_U32.PostThreadMessageW.restype = wintypes.BOOL

WM_HOTKEY = 0x0312
_WM_REG_ESC = 0x0400 + 1       # WM_USER+1: overlay opened → temporarily capture Esc
_WM_UNREG_ESC = 0x0400 + 2     # WM_USER+2: overlay closed → release Esc
_WM_STOP = 0x0400 + 3          # WM_USER+3: terminate the thread

MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000          # don't re-trigger when held down
VK_ESCAPE = 0x1B
VK_H = 0x48                    # 'H' virtual key code
VK_Q = 0x51                    # 'Q' virtual key code

_ID_CAPTURE, _ID_QUIT, _ID_ESC = 1, 2, 3


class HotkeyManager:
    # Manages global hotkeys via RegisterHotKey on its own message-loop thread.
    # Esc is registered only while the overlay is open (it swallows Esc then to
    # provide cancel); register/unregister requests are forwarded from the main
    # thread to this thread via PostThreadMessage (RegisterHotKey binds to the
    # thread it is called on).
    def __init__(self, events):
        self.events = events
        self._tid = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(2.0)  # wait until the message queue is set up (for Post)

    def _run(self):
        self._tid = _K32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Force-create the message queue so the main thread can safely Post
        _U32.PeekMessageW(ctypes.byref(msg), None, 0x0400, 0x0400, 0)
        mods = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
        ok_c = _U32.RegisterHotKey(None, _ID_CAPTURE, mods, VK_H)
        ok_q = _U32.RegisterHotKey(None, _ID_QUIT, mods, VK_Q)
        if not (ok_c and ok_q):
            _log_msg("RegisterHotKey failed — the shortcut may already be "
                     "registered by another application (Ctrl+Shift+H / Ctrl+Shift+Q).")
        self._ready.set()
        while True:
            ret = _U32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):  # WM_QUIT or error → end the loop
                break
            m = msg.message
            if m == WM_HOTKEY:
                hid = msg.wParam
                if hid == _ID_CAPTURE:
                    self.events.put("capture")
                elif hid == _ID_QUIT:
                    self.events.put("quit")
                elif hid == _ID_ESC:
                    self.events.put("cancel")
            elif m == _WM_REG_ESC:
                _U32.RegisterHotKey(None, _ID_ESC, MOD_NOREPEAT, VK_ESCAPE)
            elif m == _WM_UNREG_ESC:
                _U32.UnregisterHotKey(None, _ID_ESC)
            elif m == _WM_STOP:
                break
        _U32.UnregisterHotKey(None, _ID_CAPTURE)
        _U32.UnregisterHotKey(None, _ID_QUIT)
        _U32.UnregisterHotKey(None, _ID_ESC)

    def _post(self, message):
        if self._tid:
            _U32.PostThreadMessageW(self._tid, message, 0, 0)

    def capture_esc(self):
        # temporarily capture Esc system-wide while the overlay is open
        self._post(_WM_REG_ESC)

    def release_esc(self):
        self._post(_WM_UNREG_ESC)

    def stop(self):
        self._post(_WM_STOP)


def _single_instance_or_exit():
    # If a second copy is launched (e.g. startup shortcut + manual run),
    # exit silently; otherwise every hotkey fires twice.
    handle = _K32.CreateMutexW(None, False, MUTEX_NAME)
    err = ctypes.get_last_error()
    # 183 = ERROR_ALREADY_EXISTS; NULL + 5 (ERROR_ACCESS_DENIED) = the mutex
    # exists but couldn't be opened (e.g. an elevated copy) — both mean
    # "already running"
    if err == 183 or (not handle and err == 5):
        sys.exit(0)
    return handle


def _desktop_dir():
    # Shell API instead of Path.home()/"Desktop": also correctly finds a desktop
    # that has been moved to OneDrive.
    CSIDL_DESKTOPDIRECTORY = 0x0010
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buf) == 0:
        return Path(buf.value)
    return Path.home() / "Desktop"


def _save_png(img):
    out_dir = _desktop_dir() / "Screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = out_dir / f"{stamp}.png"
    seq = 2
    while path.exists():  # a second shot within the same second
        path = out_dir / f"{stamp}_{seq}.png"
        seq += 1
    img.save(path, "PNG")  # PNG is always lossless
    return path


def _copy_to_clipboard(img):
    # CF_DIB: for classic targets like Outlook/Gmail/Word.
    # Registered "PNG" format: for modern apps like Discord/Slack.
    # Both are set.
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, "BMP")  # an RGBA BMP shows up black in some apps
    dib = bmp.getvalue()[14:]  # drop the BITMAPFILEHEADER (14 bytes) → valid CF_DIB
    png = io.BytesIO()
    img.save(png, "PNG")
    fmt_png = win32clipboard.RegisterClipboardFormat("PNG")
    for attempt in range(10):  # if the clipboard is locked by another app, retry
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
                win32clipboard.SetClipboardData(fmt_png, png.getvalue())
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            if attempt == 9:
                _log_error()
            else:
                time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# Startup (auto-run) management — pyshot.py --debug
# ---------------------------------------------------------------------------

def _startup_lnk_path():
    CSIDL_STARTUP = 0x0007
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buf) == 0:
        return Path(buf.value) / "PyShot.lnk"
    return (Path.home() / "AppData/Roaming/Microsoft/Windows"
            / "Start Menu/Programs/Startup/PyShot.lnk")


def _add_to_startup():
    import win32com.client  # ships with pywin32
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)
    script = Path(__file__).resolve()
    shell = win32com.client.Dispatch("WScript.Shell")
    lnk = shell.CreateShortCut(str(_startup_lnk_path()))
    lnk.TargetPath = str(target)  # pythonw → no console window opens
    lnk.Arguments = f'"{script}"'
    lnk.WorkingDirectory = str(script.parent)
    lnk.Description = "PyShot — background screenshot tool (Ctrl+Shift+H)"
    lnk.Save()


def _remove_from_startup():
    lnk = _startup_lnk_path()
    if lnk.exists():
        lnk.unlink()
        return True
    return False


def _sync_startup_path():
    # If the file is moved/renamed, the startup shortcut keeps pointing at the
    # OLD path; at boot pythonw finds no file there, exits silently, and the
    # program never runs (the shortcut "dies"). If registered and the shortcut
    # doesn't point at the current location/pythonw, update it to the current
    # location. This way startup repairs itself every time you run the program
    # from its new location.
    try:
        lnk = _startup_lnk_path()
        if not lnk.exists():
            return  # not registered at startup — leave it alone
        import win32com.client  # ships with pywin32
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(str(lnk))
        cur_script = Path(__file__).resolve()
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        cur_target = pythonw if pythonw.exists() else Path(sys.executable)
        old_script = sc.Arguments.strip().strip('"')
        # Path equality on Windows ignores case and separator differences
        same = (old_script and Path(old_script) == cur_script
                and sc.TargetPath and Path(sc.TargetPath) == cur_target)
        if not same:
            _add_to_startup()  # rewrite from scratch with the current path
    except Exception:
        _log_error()  # if the repair fails, don't stop the program; just log


def _is_running():
    SYNCHRONIZE = 0x00100000
    h = _K32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if h:
        _K32.CloseHandle(h)
        return True
    return False


def _signal_quit():
    # "quit" signal to the running copy: the poll() loop listens for this event.
    EVENT_MODIFY_STATE = 0x0002
    h = _K32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT_NAME)
    if not h:
        return False
    _K32.SetEvent(h)
    _K32.CloseHandle(h)
    return True


def _debug_mode():
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    lnk = _startup_lnk_path()
    running = _is_running()
    status = "running" if running else "not running"
    try:
        if lnk.exists():
            msg = (f"PyShot is REGISTERED at startup; currently {status} in the background.\n\n"
                   "Remove completely?\n"
                   "• It will be removed from startup (won't run on boot anymore)")
            if running:
                msg += "\n• The running copy will be closed"
            msg += f"\n\nNote: the {Path(__file__).name} file itself is not deleted."
            if messagebox.askyesno("PyShot — Remove completely", msg, parent=root):
                try:
                    _remove_from_startup()
                    result = "Removed from startup."
                except OSError:
                    _log_error()
                    result = ("Could not delete the startup shortcut! "
                              "Details: %TEMP%\\pyshot.log")
                # Don't trust the 'running' snapshot: the state may have changed
                # while the dialog was open; if nobody is running it returns False anyway.
                if _signal_quit():
                    result += "\nThe running copy was closed."
                messagebox.showinfo("PyShot", result, parent=root)
        else:
            msg = (f"PyShot is NOT registered at startup; currently {status} in the background.\n\n"
                   "Add to startup?\n"
                   "(It will run automatically in the background every time the PC boots)")
            if messagebox.askyesno("PyShot — Add to startup", msg, parent=root):
                try:
                    _add_to_startup()
                    messagebox.showinfo(
                        "PyShot", "Added to startup.\nIt will run automatically "
                        "after the PC restarts.", parent=root)
                except Exception:
                    _log_error()
                    messagebox.showerror(
                        "PyShot", "Could not add to startup! "
                        "Details: %TEMP%\\pyshot.log", parent=root)
    finally:
        root.destroy()


class PyShot:
    def __init__(self, root, hotkeys):
        self.root = root
        self.hotkeys = hotkeys
        self.active = False
        self.top = None
        self.cv = None
        self.img = None
        self.ph_dim = None
        self.ph_sel = None
        self.start = None
        self.toast = None
        self._esc_active = False
        self._render_job = None
        self._pending = None

    # ---- capture flow (always called from the main/tkinter thread) ----

    def start_capture(self):
        if self.active:
            return
        self.active = True
        try:
            # If the previous shot's toast is still on screen, don't let it enter the new frame
            if self._kill_toast():
                self.root.update()
                time.sleep(0.05)  # let DWM actually remove the window
            # mss >= 10 uses the name 'MSS', older ones use 'mss'
            with getattr(mss, "MSS", mss.mss)() as sct:
                # Primary monitor: the one flagged is_primary; otherwise the one at (0,0)
                mon = (next((m for m in sct.monitors[1:] if m.get("is_primary")), None)
                       or next((m for m in sct.monitors[1:]
                                if m["left"] == 0 and m["top"] == 0),
                               sct.monitors[1]))
                shot = sct.grab(mon)  # raw BGRA, physical (native) resolution
            self.img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            self._build_overlay()
        except Exception:
            self._close_overlay()  # also cleans up a half-built overlay
            raise

    def _build_overlay(self):
        w, h = self.img.size
        top = tk.Toplevel(self.root)
        self.top = top
        top.withdraw()
        top.attributes("-fullscreen", True)
        top.attributes("-topmost", True)
        top.configure(bg="black")

        cv = tk.Canvas(top, width=w, height=h, highlightthickness=0,
                       bd=0, bg="black", cursor="crosshair")
        cv.pack(fill="both", expand=True)
        self.cv = cv

        # Frozen screen: the dimmed copy underneath, the selected region cropped
        # from the original and overlaid bright on top (the Lightshot look).
        self.ph_dim = ImageTk.PhotoImage(ImageEnhance.Brightness(self.img).enhance(DIM))
        cv.create_image(0, 0, image=self.ph_dim, anchor="nw")
        self.hint_id = cv.create_text(
            w // 2, 28, text="Select an area with the mouse  •  Esc: cancel",
            fill="#e0e0e0", font=("Segoe UI", 12))

        self.start = None
        self.ph_sel = None
        self._pending = None
        self._render_job = None
        # Creation order = stacking order (the later one stays on top)
        self.sel_img_id = cv.create_image(0, 0, anchor="nw", state="hidden")
        self.rect_id = cv.create_rectangle(0, 0, 0, 0, outline="#3aa3ff",
                                           width=1, state="hidden")
        self.size_bg_id = cv.create_rectangle(0, 0, 0, 0, fill="#202020",
                                              outline="", state="hidden")
        self.size_txt_id = cv.create_text(0, 0, anchor="nw", fill="#ffffff",
                                          font=("Consolas", 10), state="hidden")

        cv.bind("<ButtonPress-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_move)
        cv.bind("<ButtonRelease-1>", self._on_release)
        top.bind("<Escape>", self._cancel)
        top.bind("<Button-3>", self._cancel)

        top.deiconify()
        top.lift()
        top.focus_force()
        try:
            # Windows doesn't always give focus to background processes
            # (foreground lock); try to actually bring the window to the front.
            top.update_idletasks()
            hwnd = ctypes.windll.user32.GetAncestor(top.winfo_id(), 2)  # GA_ROOT
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        # If focus still doesn't arrive, tk's <Escape> binding never fires; a
        # temporary system-wide registration that captures Esc while the overlay
        # is open provides the safety net (released in _close_overlay when the
        # overlay closes).
        self.hotkeys.capture_esc()
        self._esc_active = True

    # ---- mouse/keyboard events ----

    def _sel_box(self, e):
        # Event coordinates are INCLUSIVE pixel indices (the cursor can't go past
        # the last pixel of the screen); PIL crop EXCLUDES the right/bottom edge,
        # so +1 is added — this way the last row/column can also be selected.
        w, h = self.img.size
        x = min(max(e.x, 0), w - 1)
        y = min(max(e.y, 0), h - 1)
        x1, y1 = self.start
        return (min(x1, x), min(y1, y), max(x1, x) + 1, max(y1, y) + 1)

    def _on_press(self, e):
        w, h = self.img.size
        self.start = (min(max(e.x, 0), w - 1), min(max(e.y, 0), h - 1))
        self.cv.itemconfigure(self.hint_id, state="hidden")

    def _on_move(self, e):
        if not self.start or self.cv is None:
            return
        # Producing a full PhotoImage on every mouse event stutters on large
        # selections; the events are coalesced and the latest box is drawn once
        # every RENDER_MS.
        self._pending = self._sel_box(e)
        if self._render_job is None:
            self._render_job = self.cv.after(RENDER_MS, self._render)

    def _render(self):
        self._render_job = None
        if self.cv is None or self.img is None or self._pending is None:
            return
        cv = self.cv
        l, t, r, b = self._pending
        sw, sh = r - l, b - t
        self.ph_sel = ImageTk.PhotoImage(self.img.crop((l, t, r, b)))
        cv.itemconfigure(self.sel_img_id, image=self.ph_sel, state="normal")
        cv.coords(self.sel_img_id, l, t)
        cv.itemconfigure(self.rect_id, state="normal")
        cv.coords(self.rect_id, l, t, r, b)
        # live size label — place it first, then re-clamp using its real width so
        # it doesn't overflow the edge (the font can grow with DPI)
        iw, ih = self.img.size
        cv.itemconfigure(self.size_txt_id, text=f" {sw} × {sh} px ",
                         state="normal")
        ty = t - 24 if t >= 28 else b + 6
        cv.coords(self.size_txt_id, l, ty)
        bb = cv.bbox(self.size_txt_id)
        lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        tx = max(2, min(l, iw - lw - 2))
        ty = max(2, min(ty, ih - lh - 2))
        cv.coords(self.size_txt_id, tx, ty)
        cv.coords(self.size_bg_id, *cv.bbox(self.size_txt_id))
        cv.itemconfigure(self.size_bg_id, state="normal")

    def _on_release(self, e):
        if not self.start:
            return
        box = self._sel_box(e)
        self.start = None
        l, t, r, b = box
        if (r - l) < MIN_SEL or (b - t) < MIN_SEL:
            self._cancel()
            return
        region = self.img.crop(box)
        self._close_overlay()
        self.root.update_idletasks()  # make the overlay disappear instantly, then save
        try:
            path = _save_png(region)
        except Exception:
            _log_error()
            self._toast("✗ Save error!  Details: %TEMP%\\pyshot.log")
            return
        ok = _copy_to_clipboard(region)
        msg = f"✓ {path.name} saved"
        msg += "  •  copied to clipboard" if ok else "  •  clipboard copy failed!"
        self._toast(msg)

    def _cancel(self, event=None):
        self._close_overlay()

    def _close_overlay(self):
        if self._render_job is not None and self.cv is not None:
            try:
                self.cv.after_cancel(self._render_job)
            except tk.TclError:
                pass
        self._render_job = None
        self._pending = None
        if self._esc_active:
            self.hotkeys.release_esc()
            self._esc_active = False
        if self.top is not None:
            self.top.destroy()
            self.top = None
        self.cv = None
        self.ph_dim = self.ph_sel = None
        self.img = None
        self.start = None
        self.active = False

    def _kill_toast(self):
        if self.toast is None:
            return False
        try:
            self.toast.destroy()
        except tk.TclError:
            pass
        self.toast = None
        return True

    def _toast(self, msg):
        self._kill_toast()  # close any overlapping old toast
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        tk.Label(t, text=msg, bg="#1f1f1f", fg="#ffffff",
                 font=("Segoe UI", 10), padx=14, pady=9).pack()
        t.update_idletasks()
        x = t.winfo_screenwidth() - t.winfo_width() - 24
        y = t.winfo_screenheight() - t.winfo_height() - 72
        t.geometry(f"+{x}+{y}")
        self.toast = t
        t.after(2500, self._kill_toast)


def main():
    _single_instance_or_exit()
    # If the file was moved, silently update the startup shortcut to the current path
    _sync_startup_path()
    # --debug "remove completely" can close us by signaling this event
    quit_event = _K32.CreateEventW(None, False, False, QUIT_EVENT_NAME)
    if quit_event and ctypes.get_last_error() == 183:
        # If the event was inherited from an old process, consume the stale
        # signal; otherwise we'd close instantly on startup.
        _K32.WaitForSingleObject(quit_event, 0)
    root = tk.Tk()
    root.withdraw()
    # Under pythonw there is no stderr, so tkinter callback errors are normally
    # swallowed silently; route them all to the log file.
    root.report_callback_exception = lambda *exc: _log_error()

    # Ctrl+Shift+H / Ctrl+Shift+Q: the RegisterHotKey thread. As WM_HOTKEY
    # arrives it only drops events onto the 'events' queue without touching tkinter.
    hotkeys = HotkeyManager(events)
    hotkeys.start()
    app = PyShot(root, hotkeys)

    def poll():
        # quit signal from --debug (0 = WAIT_OBJECT_0: signaled)
        if quit_event and _K32.WaitForSingleObject(quit_event, 0) == 0:
            hotkeys.stop()
            root.destroy()
            return
        try:
            while True:
                ev = events.get_nowait()
                if ev == "quit":
                    hotkeys.stop()
                    root.destroy()
                    return
                if ev == "capture":
                    try:
                        app.start_capture()
                    except Exception:
                        _log_error()
                elif ev == "cancel" and app.active:
                    app._cancel()
        except queue.Empty:
            pass
        root.after(POLL_MS, poll)

    root.after(POLL_MS, poll)
    root.mainloop()


if __name__ == "__main__":
    try:
        if "--debug" in sys.argv[1:]:
            _debug_mode()
        else:
            main()
    except SystemExit:
        raise
    except Exception:
        _log_error()
