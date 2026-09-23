import ctypes


def _set_dpi_awareness():
    # Returns which level was actually reached, so a blurry capture on a HiDPI
    # display has something to blame in the log.
    try:  # Windows 10 1703+ : Per-Monitor v2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1+ : Per-Monitor
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:  # Vista+ : System aware
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except (AttributeError, OSError):
        pass
    return "none"


_DPI_LEVEL = _set_dpi_awareness()

# Everything is imported inside ONE guard on purpose. Under pythonw there is no
# console and no stderr, so a missing package would otherwise produce literally
# nothing on screen — and _log_error() is not even defined this early, so not
# even a log line would be possible. MessageBoxW is used instead of
# tkinter.messagebox precisely because it needs only ctypes (already imported
# above), so it still works when tkinter or Pillow is the broken piece.
try:
    import functools
    import io
    import math
    import os
    import queue
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    import tkinter as tk
    import traceback
    from ctypes import wintypes
    from dataclasses import dataclass
    from datetime import datetime
    from pathlib import Path

    import mss               # raw, lossless screen capture
    import win32clipboard    # clipboard (CF_DIB + PNG + text)
    import win32gui          # window enumeration for click-to-grab
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageTk
except ImportError as _exc:
    ctypes.windll.user32.MessageBoxW(
        None,
        "PyShot could not start: missing dependency "
        f"'{getattr(_exc, 'name', None) or _exc}'.\n\n"
        "Install the requirements and try again:\n\n"
        "    pip install -r requirements.txt\n"
        "    (or: pip install mss Pillow pywin32)",
        "PyShot", 0x10)
    raise SystemExit(1)


__version__ = "2.1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOTKEY_CAPTURE = "ctrl+shift+h"   # e.g. "printscreen", "ctrl+alt+f9", "ctrl+shift+f12"
                                  # (Win+<key> combos reserved by the shell, like
                                  #  win+s, cannot be registered by any app)
HOTKEY_QUIT = "ctrl+shift+q"
CAPTURE_TARGET = "cursor"   # "cursor" = the monitor the mouse is on | "primary" | "all"
CAPTURE_MODE = "both"       # "both" | "clipboard" (no file) | "file" (no clipboard)
WINDOW_SNAP = True          # highlight the window under the cursor; click to grab it
LOUPE = True                # magnifier + crosshair + pixel coordinate/colour readout
ANNOTATE = True             # T in the overlay: draw on the capture before it is delivered
TELL_IF_RUNNING = True      # a second launch says "already running" instead of exiting mutely
POLL_MS = 40                # poll interval for the hotkey queue (ms)
RENDER_MS = 15              # refresh interval of the selection drawing (caps at ~60 fps)
TOAST_MS = 2500             # how long the confirmation toast stays on screen (ms)
DIM = 0.35                  # dim level for the area outside the selection
MIN_SEL = 3                 # px; a selection smaller than this = click, not a drag
LOG_FILE = Path(tempfile.gettempdir()) / "pyshot.log"

LOUPE_SRC = 17              # source pixels sampled by the loupe — keep it ODD, so
                            # there is a true centre pixel to read the colour from
LOUPE_ZOOM = 8              # magnification factor
LOUPE_PAD = 20              # gap between the cursor and the loupe

# Hotkeys are captured on a SEPARATE thread (the RegisterHotKey message loop);
# touching tkinter from that thread would crash. That thread only drops events
# onto this queue; the main (tkinter) loop reads the queue via root.after and
# does the work itself. Every item is a (kind, payload) pair.
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


def _tell(text, error=False):
    # Console when there is one (python), message box when there is not (pythonw).
    if sys.stdout is not None and sys.stderr is not None:
        print(text, file=sys.stderr if error else sys.stdout)
    else:
        ctypes.windll.user32.MessageBoxW(None, text, "PyShot", 0x10 if error else 0x40)


MUTEX_NAME = "PyShot_SingleInstance"   # running-copy detection
QUIT_EVENT_NAME = "PyShot_Quit"        # quit signal from --startup to the running copy

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
_U32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_U32.GetCursorPos.restype = wintypes.BOOL
_U32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_U32.MonitorFromPoint.restype = wintypes.HANDLE
_U32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
_U32.SystemParametersInfoW.restype = wintypes.BOOL
_U32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
_U32.SetWindowPos.restype = wintypes.BOOL
_U32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_U32.GetAncestor.restype = wintypes.HWND
_U32.SetForegroundWindow.argtypes = [wintypes.HWND]
_U32.SetForegroundWindow.restype = wintypes.BOOL
_U32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
_U32.MessageBoxW.restype = ctypes.c_int
_U32.GetDoubleClickTime.argtypes = []
_U32.GetDoubleClickTime.restype = wintypes.UINT


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_U32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
_U32.GetMonitorInfoW.restype = wintypes.BOOL

# DWM is only used to read window frames; if it is missing (it is not on
# pre-Vista) window snapping silently degrades to GetWindowRect.
try:
    _DWM = ctypes.WinDLL("dwmapi")
    _DWM.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD]
    _DWM.DwmGetWindowAttribute.restype = ctypes.c_long   # HRESULT
except OSError:
    _DWM = None

WM_HOTKEY = 0x0312
_WM_REG_ESC = 0x0400 + 1       # WM_USER+1: overlay opened → temporarily capture Esc
_WM_UNREG_ESC = 0x0400 + 2     # WM_USER+2: overlay closed → release Esc
_WM_STOP = 0x0400 + 3          # WM_USER+3: terminate the thread

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000          # don't re-trigger when held down
VK_ESCAPE = 0x1B

MONITOR_DEFAULTTONEAREST = 2
SPI_GETWORKAREA = 0x0030
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = ctypes.c_void_p(-1)
GA_ROOT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
# The desktop host windows plus the taskbars: never snap targets (see _window_rects)
_SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")

_ID_CAPTURE, _ID_QUIT, _ID_ESC = 1, 2, 3

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT,
              "alt": MOD_ALT, "win": MOD_WIN, "super": MOD_WIN}
_NAMED_KEYS = {"printscreen": 0x2C, "prtsc": 0x2C, "prtscr": 0x2C, "snapshot": 0x2C,
               "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
               "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
               "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
               "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "pause": 0x13,
               "scrolllock": 0x91, "numlock": 0x90,
               "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}
_NAMED_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})   # f1 = 0x70
# A distinct fallback per key: sharing one would make a typo in HOTKEY_QUIT
# collide with the capture hotkey, so quit would fail to register as well.
_FALLBACK = {"capture": (MOD_CONTROL | MOD_SHIFT, 0x48),
             "quit": (MOD_CONTROL | MOD_SHIFT, 0x51)}


# ---------------------------------------------------------------------------
# Pure helpers — no Win32, no tkinter, no filesystem side effects beyond the
# path probing in next_free_path(). These are what tests/test_pure.py covers.
# ---------------------------------------------------------------------------

def sel_box(start, x, y, w, h):
    """Two INCLUSIVE cursor positions → an EXCLUSIVE PIL crop box.

    Event coordinates are inclusive pixel indices (the cursor can't go past the
    last pixel of the screen) while PIL crop excludes the right/bottom edge, so
    +1 is added — this way the last row/column can also be selected.
    """
    x = min(max(x, 0), w - 1)
    y = min(max(y, 0), h - 1)
    x1 = min(max(start[0], 0), w - 1)
    y1 = min(max(start[1], 0), h - 1)
    return (min(x1, x), min(y1, y), max(x1, x) + 1, max(y1, y) + 1)


def next_free_path(out_dir, stamp, ext=".png"):
    """First unused '<stamp>.png', '<stamp>_2.png', … in out_dir."""
    path = out_dir / f"{stamp}{ext}"
    seq = 2
    while path.exists():  # a second shot within the same second
        path = out_dir / f"{stamp}_{seq}{ext}"
        seq += 1
    return path


def to_dib(img):
    """Pillow image → CF_DIB payload.

    An RGBA BMP shows up black in some apps, hence the convert("RGB"); the
    14-byte BITMAPFILEHEADER is dropped because CF_DIB starts at the
    BITMAPINFOHEADER.
    """
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, "BMP")
    return bmp.getvalue()[14:]


def parse_hotkey(spec, fallback=(MOD_CONTROL | MOD_SHIFT, 0x48)):
    """'ctrl+shift+h' → (modifier mask, virtual key code).

    Falls back to the given combination (Ctrl+Shift+H by default) and logs why
    when the spec cannot be parsed, so a typo can never leave PyShot with no
    working capture hotkey at all.
    """
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    mods = 0
    problem = None
    if not parts:
        problem = "empty hotkey"
    else:
        for name in parts[:-1]:
            if name not in _MODIFIERS:
                problem = f"unknown modifier {name!r}"
                break
            mods |= _MODIFIERS[name]
    if problem is None:
        key = parts[-1]
        if len(key) == 1 and key.isascii() and key.isalnum():
            return mods | MOD_NOREPEAT, ord(key.upper())   # A-Z 0x41-0x5A, 0-9 0x30-0x39
        if key in _NAMED_KEYS:
            return mods | MOD_NOREPEAT, _NAMED_KEYS[key]
        problem = f"unknown key {key!r}"
    _log_msg(f"Could not parse hotkey {spec!r} ({problem}); "
             "falling back to the built-in default.")
    return fallback[0] | MOD_NOREPEAT, fallback[1]


def pretty_hotkey(spec):
    """'ctrl+shift+h' → 'Ctrl+Shift+H' (for messages shown to the user)."""
    return "+".join(p.strip().title() for p in str(spec).split("+") if p.strip())


# ---------------------------------------------------------------------------
# Annotation — pure layer. A mark is data and Pillow draws it; the overlay and
# the delivered PNG both come out of these functions, so they cannot disagree.
# No Win32, no tkinter: tests/test_annotate.py runs all of it headlessly.
# ---------------------------------------------------------------------------

_MARKER_ALPHA = 102         # Marker opacity, 0-255 (~40 %)
_FONT_FILES = (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
               "arial.ttf")


@dataclass(frozen=True)
class Mark:
    """One annotation, in pixel coordinates relative to the captured region.

    kind  "pen" | "marker" | "arrow" | "box" | "fill" | "text" | "blur"
    pts   pen/marker: the stroke; arrow: (tail, tip); box/fill/blur: two
          opposite corners, inclusive; text: (top-left,)
    size  stroke width, font size in px, or the blur's block size
    """
    kind: str
    color: str
    size: int
    pts: tuple
    text: str = ""


def _arrow_dims(size):
    """(head length, head half-width) for a stroke width — shared with the Tk preview."""
    return 3 * size + 8, 1.5 * size + 4


def arrow_head(x0, y0, x1, y1, size):
    """(tip, left, right) of the head of an arrow from (x0, y0) to (x1, y1).

    None for a zero-length arrow, which has no direction to point in. The head
    never reaches back past the tail, so a very short arrow is all head.
    """
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length < 1:
        return None
    head, half = _arrow_dims(size)
    head = min(head, length)
    ux, uy = dx / length, dy / length
    bx, by = x1 - ux * head, y1 - uy * head          # centre of the head's base
    return ((x1, y1), (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half))


def _box(pts):
    """Two opposite corners, in either order → (left, top, right, bottom)."""
    (xa, ya), (xb, yb) = pts[0], pts[-1]
    return min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)


def pixelate(img, box, block):
    """Pixelate the EXCLUSIVE box of img in place, in block × block squares.

    The box is clamped to the image, so one hanging off an edge is fine and an
    empty one is a no-op. Squares are aligned to the box's top-left corner.
    """
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(img.width, int(box[2])), min(img.height, int(box[3]))
    if x1 <= x0 or y1 <= y0:
        return
    block = max(1, int(block))
    w, h = x1 - x0, y1 - y0
    small = img.crop((x0, y0, x1, y1)).resize((-(-w // block), -(-h // block)), Image.BOX)
    big = small.resize((small.width * block, small.height * block), Image.NEAREST)
    img.paste(big.crop((0, 0, w, h)), (x0, y0))


@functools.lru_cache(maxsize=16)
def _font(size):
    """Segoe UI at `size` px, else Arial, else Pillow's own font. Never raises."""
    for name in _FONT_FILES:
        try:
            return ImageFont.truetype(str(name), size)
        except Exception:       # a missing file, or a Pillow built without FreeType
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def _stroke(draw, pts, fill, width):
    # Pillow has no round line caps: joint="curve" rounds the corners, a disc
    # rounds each end, and a one-point stroke (a click) is just that disc.
    if len(pts) > 1:
        draw.line(pts, fill=fill, width=width, joint="curve")
    r = (width - 1) / 2
    for x, y in (pts[0], pts[-1]):
        if r:
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        else:
            draw.point((x, y), fill=fill)   # a zero-radius ellipse draws nothing


def _draw_mark(img, mark):
    """Draw one mark onto img IN PLACE (apply_mark is the copying version)."""
    size = max(1, int(mark.size))
    draw = ImageDraw.Draw(img)
    kind, pts = mark.kind, mark.pts
    if kind == "pen":
        _stroke(draw, pts, mark.color, size)
    elif kind == "marker":
        # Stroked at full strength into a mask and blended ONCE, so the places
        # where a stroke crosses itself do not come out darker. Only the
        # stroke's own neighbourhood is touched: undo replays every mark, and a
        # full-size mask per Marker made 20 strokes on a 4K region take ~0.6 s.
        pad = size // 2 + 1
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, y0 = max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad)
        x1 = min(img.width, int(max(xs)) + pad + 1)
        y1 = min(img.height, int(max(ys)) + pad + 1)
        if x1 > x0 and y1 > y0:
            mask = Image.new("L", (x1 - x0, y1 - y0), 0)
            _stroke(ImageDraw.Draw(mask), [(x - x0, y - y0) for x, y in pts],
                    _MARKER_ALPHA, size)
            img.paste(Image.new("RGB", mask.size, mark.color), (x0, y0), mask)
    elif kind == "arrow":
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        head = arrow_head(x0, y0, x1, y1, size)
        if head is not None:
            tip, left, right = head
            neck = ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
            _stroke(draw, [(x0, y0), neck], mark.color, size)
            draw.polygon([tip, left, right], fill=mark.color)
    elif kind == "box":
        x0, y0, x1, y1 = _box(pts)
        # Pillow grows an outline inward; widening the box by half a stroke
        # centres the line on the edge the user dragged, as Tk's preview does.
        h = size // 2
        draw.rectangle((x0 - h, y0 - h, x1 + h, y1 + h), outline=mark.color, width=size)
    elif kind == "fill":
        draw.rectangle(_box(pts), fill=mark.color)
    elif kind == "text":
        if mark.text:
            draw.multiline_text(pts[0], mark.text, fill=mark.color, font=_font(size))
    elif kind == "blur":
        x0, y0, x1, y1 = _box(pts)
        pixelate(img, (x0, y0, x1 + 1, y1 + 1), size)


def apply_mark(img, mark):
    """A copy of img with one mark drawn on it; img itself is never modified."""
    out = img.copy()
    _draw_mark(out, mark)
    return out


def render_marks(base, marks):
    """base with every mark drawn in order: the same result as chaining apply_mark."""
    out = base.copy()
    for mark in marks:
        _draw_mark(out, mark)
    return out


# ---------------------------------------------------------------------------
# Monitors and windows
# ---------------------------------------------------------------------------

def _monitor_under_cursor():
    """Rect + work area of the monitor the mouse is on, or None on failure.

    Keys match what mss.grab() wants (left/top/width/height) so the bitmap and
    the overlay window can never be sized from two different sources.
    """
    pt = wintypes.POINT()
    if not _U32.GetCursorPos(ctypes.byref(pt)):
        return None
    hmon = _U32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not _U32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    m, w = mi.rcMonitor, mi.rcWork
    return {"left": m.left, "top": m.top,
            "width": m.right - m.left, "height": m.bottom - m.top,
            "work": (w.left, w.top, w.right, w.bottom)}


def _work_area():
    """(left, top, right, bottom) of the work area under the cursor.

    The work area excludes the taskbar wherever it is docked, which a hardcoded
    bottom margin cannot do — and it is per-monitor, so the toast follows the
    screen the user is actually looking at.
    """
    mon = _monitor_under_cursor()
    if mon:
        return mon["work"]
    r = wintypes.RECT()
    if _U32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return (r.left, r.top, r.right, r.bottom)
    return None


def _grab(sct, rect):
    shot = sct.grab({k: rect[k] for k in ("left", "top", "width", "height")})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _primary_monitor(sct):
    # The one flagged is_primary; otherwise the one at (0,0)
    return (next((m for m in sct.monitors[1:] if m.get("is_primary")), None)
            or next((m for m in sct.monitors[1:] if m["left"] == 0 and m["top"] == 0),
                    sct.monitors[1]))


def _capture(sct):
    """(rect, image) for the configured CAPTURE_TARGET, at native resolution."""
    if CAPTURE_TARGET == "all":
        vs = dict(sct.monitors[0])   # a bounding BOX of every monitor, not their union
        img = Image.new("RGB", (vs["width"], vs["height"]), "black")
        for m in sct.monitors[1:]:   # so an L-shaped layout keeps the gaps black
            img.paste(_grab(sct, m), (m["left"] - vs["left"], m["top"] - vs["top"]))
        vs["work"] = None
        return vs, img
    if CAPTURE_TARGET == "cursor":
        mon = _monitor_under_cursor()
        if mon:
            return mon, _grab(sct, mon)
    mon = dict(_primary_monitor(sct))
    return mon, _grab(sct, mon)


def _window_rects(skip=()):
    """Frames of the visible top-level windows, front of the z-order first.

    DWMWA_EXTENDED_FRAME_BOUNDS instead of GetWindowRect: the latter over-reports
    by the ~7 px invisible resize border, so a snapped window would carry a
    transparent margin. Cloaked windows (suspended UWP apps) report as visible
    but are not on screen, so they are skipped.

    The shell's own surfaces are skipped too. Progman/WorkerW (the desktop) span
    the whole monitor and sit at the back of the z-order, so keeping them would
    make "the window under the cursor" match EVERYWHERE: clicking bare desktop
    would save a full-screen PNG instead of cancelling, and the dimmed backdrop
    would be lit up the moment the overlay opened. Click-through windows
    (WS_EX_TRANSPARENT) are not what the user is pointing at either.
    """
    out = []

    def visit(hwnd, _arg):
        try:
            if hwnd in skip or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            if win32gui.GetClassName(hwnd) in _SHELL_CLASSES:
                return True
            if win32gui.GetWindowLong(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
                return True
            if _DWM is not None:
                cloaked = ctypes.c_int(0)
                if (_DWM.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                               ctypes.sizeof(cloaked)) == 0
                        and cloaked.value):
                    return True
            r = wintypes.RECT()
            got = (_DWM is not None
                   and _DWM.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                                  ctypes.byref(r), ctypes.sizeof(r)) == 0)
            if not got:
                r.left, r.top, r.right, r.bottom = win32gui.GetWindowRect(hwnd)
            if r.right - r.left > 8 and r.bottom - r.top > 8:
                out.append((r.left, r.top, r.right, r.bottom))
        except Exception:
            pass    # a window can die mid-enumeration; it just isn't snappable
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        _log_error()
    return out


def _window_at(rects, x, y):
    """The frontmost window frame containing the screen point (x, y).

    Frontmost rather than smallest: what is under the cursor is whatever is
    drawn there, so a small window hidden behind a maximised one must not win.
    """
    for x0, y0, x1, y1 in rects:
        if x0 <= x < x1 and y0 <= y < y1:
            return (x0, y0, x1, y1)
    return None


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
        # Reported per key: capture can be perfectly fine while only quit was
        # stolen, and the log-only report of a stolen capture hotkey used to be
        # indistinguishable from "PyShot is not running". The failures are
        # collected and posted as ONE event, because two toasts queued in the
        # same tick would replace each other and only the second would be seen.
        failed = []
        for spec, hid, what in ((HOTKEY_CAPTURE, _ID_CAPTURE, "capture"),
                                (HOTKEY_QUIT, _ID_QUIT, "quit")):
            mods, vk = parse_hotkey(spec, _FALLBACK[what])
            if not _U32.RegisterHotKey(None, hid, mods, vk):
                _log_msg(f"RegisterHotKey failed for the {what} hotkey "
                         f"({pretty_hotkey(spec)}), error={ctypes.get_last_error()} — "
                         "it is probably already registered by another application.")
                failed.append((what, spec))
        if failed:
            self.events.put(("hotkey_failed", failed))
        self._ready.set()
        while True:
            ret = _U32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):  # WM_QUIT or error → end the loop
                break
            m = msg.message
            if m == WM_HOTKEY:
                hid = msg.wParam
                if hid == _ID_CAPTURE:
                    self.events.put(("capture", None))
                elif hid == _ID_QUIT:
                    self.events.put(("quit", None))
                elif hid == _ID_ESC:
                    self.events.put(("cancel", None))
            elif m == _WM_REG_ESC:
                if not _U32.RegisterHotKey(None, _ID_ESC, MOD_NOREPEAT, VK_ESCAPE):
                    _log_msg("Esc could not be registered while the overlay is open "
                             "— right-click still cancels a capture.")
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
    # exit; otherwise every hotkey fires twice.
    handle = _K32.CreateMutexW(None, False, MUTEX_NAME)
    err = ctypes.get_last_error()
    # 183 = ERROR_ALREADY_EXISTS; NULL + 5 (ERROR_ACCESS_DENIED) = the mutex
    # exists but couldn't be opened (e.g. an elevated copy) — both mean
    # "already running"
    if err == 183 or (not handle and err == 5):
        if TELL_IF_RUNNING:
            # PyShot has no window and no tray icon, so a second launch is the
            # only moment it can answer "is it already running?"
            _U32.MessageBoxW(
                None,
                f"PyShot {__version__} is already running in the background.\n\n"
                f"{pretty_hotkey(HOTKEY_CAPTURE)} — capture\n"
                f"{pretty_hotkey(HOTKEY_QUIT)} — quit\n\n"
                f"Startup settings: {_launch_cmd()} --startup",
                "PyShot", 0x40)
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


def _shots_dir():
    return _desktop_dir() / "Screenshots"


def _save_png(img):
    out_dir = _shots_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = next_free_path(out_dir, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
    img.save(path, "PNG")  # PNG is always lossless
    return path


def _clipboard_write(entries):
    """Put [(format, data), …] on the clipboard in a single session.

    If the clipboard is momentarily locked by another app, retry.
    """
    for attempt in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                for fmt, data in entries:
                    win32clipboard.SetClipboardData(fmt, data)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            if attempt == 9:
                _log_error()
            else:
                time.sleep(0.1)
    return False


def _copy_to_clipboard(img):
    # CF_DIB: for classic targets like Outlook/Gmail/Word.
    # Registered "PNG" format: for modern apps like Discord/Slack.
    # Both are set.
    png = io.BytesIO()
    img.save(png, "PNG")
    return _clipboard_write([
        (win32clipboard.CF_DIB, to_dib(img)),
        (win32clipboard.RegisterClipboardFormat("PNG"), png.getvalue()),
    ])


def _copy_text(text):
    return _clipboard_write([(win32clipboard.CF_UNICODETEXT, text)])


# ---------------------------------------------------------------------------
# Startup (auto-run) management — pyshot.py --startup
# ---------------------------------------------------------------------------

def _frozen():
    # True in a PyInstaller build, where __file__ is a temp dir that is deleted
    # when the process exits and must never be written into a shortcut.
    return bool(getattr(sys, "frozen", False))


def _script_name():
    return Path(sys.executable).name if _frozen() else Path(__file__).name


def _launch_cmd(background=False):
    """How to invoke this copy: 'python pyshot.py' for a script, 'PyShot.exe' frozen."""
    if _frozen():
        return _script_name()
    return f"{'pythonw' if background else 'python'} {_script_name()}"


def _expected_startup_target():
    """(executable, argument) the Startup shortcut should point at."""
    if _frozen():
        return Path(sys.executable), ""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return exe, str(Path(__file__).resolve())     # pythonw → no console window opens


def _startup_lnk_path():
    CSIDL_STARTUP = 0x0007
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buf) == 0:
        return Path(buf.value) / "PyShot.lnk"
    return (Path.home() / "AppData/Roaming/Microsoft/Windows"
            / "Start Menu/Programs/Startup/PyShot.lnk")


def _add_to_startup():
    import win32com.client  # ships with pywin32
    exe, arg = _expected_startup_target()
    shell = win32com.client.Dispatch("WScript.Shell")
    lnk = shell.CreateShortCut(str(_startup_lnk_path()))
    lnk.TargetPath = str(exe)
    lnk.Arguments = f'"{arg}"' if arg else ""
    lnk.WorkingDirectory = str((Path(arg).parent if arg else exe.parent))
    lnk.Description = (f"PyShot — background screenshot tool "
                       f"({pretty_hotkey(HOTKEY_CAPTURE)})")
    lnk.Save()


def _remove_from_startup():
    lnk = _startup_lnk_path()
    if lnk.exists():
        lnk.unlink()
        return True
    return False


def _startup_state():
    """(registered, points_at_this_copy, what_it_points_at_now)."""
    lnk = _startup_lnk_path()
    if not lnk.exists():
        return False, False, ""
    try:
        import win32com.client  # ships with pywin32
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(str(lnk))
        exe, arg = _expected_startup_target()
        cur_exe = (sc.TargetPath or "").strip()
        cur_arg = (sc.Arguments or "").strip().strip('"')
        # Path equality on Windows ignores case and separator differences
        same_exe = bool(cur_exe) and Path(cur_exe) == exe
        if not arg or not cur_arg:
            same_arg = cur_arg == arg
        else:
            same_arg = Path(cur_arg) == Path(arg)
        return True, bool(same_exe and same_arg), cur_arg or cur_exe
    except Exception:
        _log_error()
        return True, False, ""


def _sync_startup_path():
    # If the file is moved/renamed, the startup shortcut keeps pointing at the
    # OLD path; at boot pythonw finds no file there, exits silently, and the
    # program never runs (the shortcut "dies"). If registered and the shortcut
    # doesn't point at the current location/pythonw, update it. Note this can
    # only repair itself once the moved copy is run — the stale shortcut cannot
    # fix itself at boot, because nothing starts.
    try:
        registered, ok, _old = _startup_state()
        if registered and not ok:
            _add_to_startup()   # rewrite from scratch with the current path
            return True
    except Exception:
        _log_error()  # if the repair fails, don't stop the program; just log
    return False


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


def _startup_dialog():
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    # Read the old target BEFORE repairing, so the dialog can name what the
    # shortcut used to point at; the diagnostic screen should heal, then report.
    registered, points_here, old_target = _startup_state()
    repaired = _sync_startup_path() if registered and not points_here else False
    if repaired:
        registered, points_here, _now = _startup_state()
    running = _is_running()
    status = "running" if running else "not running"
    try:
        if registered:
            if repaired:
                where = f"\n• The shortcut pointed at {old_target or 'another path'} " \
                        "and has just been repaired to this copy."
            elif points_here:
                where = "\n• The shortcut points at this copy."
            else:
                where = (f"\n• WARNING: the shortcut points at {old_target or 'another path'} "
                         "and could not be repaired (see %TEMP%\\pyshot.log).")
            msg = (f"PyShot is REGISTERED at startup; currently {status} "
                   f"in the background.{where}\n\n"
                   "Remove completely?\n"
                   "• It will be removed from startup (won't run on boot anymore)")
            if running:
                msg += "\n• The running copy will be closed"
            msg += f"\n\nNote: the {_script_name()} file itself is not deleted."
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
                elif _is_running():
                    result += ("\nThe running copy could NOT be closed — press "
                               f"{pretty_hotkey(HOTKEY_QUIT)}, or try again as "
                               "administrator.")
                messagebox.showinfo("PyShot", result, parent=root)
        else:
            msg = (f"PyShot is NOT registered at startup; currently {status} "
                   "in the background.\n\n"
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


# ---------------------------------------------------------------------------
# Annotation — edit phase (Tk). The Annotator owns the toolbar and the live
# preview; every committed mark goes through apply_mark(), so the region on
# screen is always exactly the image that on_done() delivers.
# ---------------------------------------------------------------------------

ANNOT_ACCENT = "#ff9f0a"    # annotation mode's colour: outline, active tool, Done
_PALETTE = ("#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#000000", "#ffffff")
# tool: (default, min, max, wheel step) in 96-DPI pixels
_TOOL_SIZES = {"pen": (4, 1, 30, 1), "marker": (18, 6, 60, 2), "arrow": (4, 1, 30, 1),
               "box": (3, 1, 30, 1), "text": (20, 10, 96, 2), "blur": (12, 6, 48, 2)}
_TOOLBAR_ROWS = (("pen", "marker"), ("arrow", "box"), ("text", "blur"),
                 ("color0", "color1"), ("color2", "color3"), ("color4", "color5"),
                 ("size",), ("undo", "cancel"), ("done",))
_ANNOT_ON_HINT = "Annotate ON: select an area to draw on  •  T: off  •  Esc: cancel"
_ANNOT_HELP = ("Enter: done  •  Ctrl+Z: undo  •  wheel: size  •  "
               "Shift+box: solid  •  Esc: cancel")
_TK_SHIFT = 0x0001
# Control (0x0004) and Alt (0x20000) in a Tk event.state on Windows. Not Mod1
# (0x0008): Tk reports NumLock there, and masking it made T and C do nothing
# whenever NumLock was on.
_TK_CTRL_ALT = 0x0004 | 0x20000


def _ui_scale(widget):
    """System DPI / 96, never below 1: the factor for the toolbar and tool sizes."""
    try:
        return max(1.0, widget.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


class Annotator:
    """The edit phase: a toolbar beside the region and marks drawn inside it.

    Everything it puts on the canvas is tagged "annot". It schedules no after()
    timers, so tearing it down can never leave one armed on a destroyed widget.
    """

    def __init__(self, cv, box, base, on_done, on_cancel, scale=1.0):
        self.cv = cv
        self.box = box                  # EXCLUSIVE (x0, y0, x1, y1), canvas coordinates
        self.base = base                # the clean region; undo replays onto it
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.scale = scale
        self.marks = []
        self.render = base.copy()       # base + marks: what is shown and what is delivered
        self.photo = None
        self.tool = "pen"
        self.color = _PALETTE[0]
        self.sizes = {tool: self._px(spec[0]) for tool, spec in _TOOL_SIZES.items()}
        self.preview = None             # canvas item of the stroke being dragged
        self.points = []                # its canvas coordinates
        self.text_id = None             # canvas item of the text being typed
        self.text_at = None             # its region-relative top-left
        self.text = ""
        self.buttons = []               # (x0, y0, x1, y1, action) toolbar hit boxes
        self.bar = (0, 0, 0, 0)
        self._cursor = None
        self._wheel = 0                 # wheel delta not yet turned into a size step
        self.img_id = cv.create_image(box[0], box[1], anchor="nw", tags="annot")
        self._refresh()
        self._draw_help()
        self._draw_toolbar()

    @property
    def typing(self):
        return self.text_id is not None

    # ---- geometry ----

    def _px(self, value):
        return max(1, round(value * self.scale))

    def _in_region(self, x, y):
        x0, y0, x1, y1 = self.box
        return x0 <= x < x1 and y0 <= y < y1

    def _in_bar(self, x, y):
        x0, y0, x1, y1 = self.bar
        return x0 <= x < x1 and y0 <= y < y1

    def _clamp(self, x, y):
        x0, y0, x1, y1 = self.box
        return min(max(x, x0), x1 - 1), min(max(y, y0), y1 - 1)

    def _rel(self, pts):
        return tuple((x - self.box[0], y - self.box[1]) for x, y in pts)

    # ---- mouse ----

    def on_press(self, e):
        if self._in_bar(e.x, e.y):
            self._click(self._hit(e.x, e.y))
            return
        self._commit_text()             # any other click ends the text being typed
        if not self._in_region(e.x, e.y):
            return
        x, y = self._clamp(e.x, e.y)
        if self.tool == "text":
            self._start_text(x, y)
            return
        self.points = [(x, y)]
        self.preview = self._new_preview(x, y, e.state)

    def on_drag(self, e):
        if self.preview is None:
            return
        x, y = self._clamp(e.x, e.y)
        if self.tool in ("pen", "marker"):
            if (x, y) != self.points[-1]:
                self.points.append((x, y))
        else:
            self.points[1:] = [(x, y)]
        flat = [c for p in self.points for c in p]
        if len(flat) == 2:
            flat *= 2                   # a Tk line needs at least two points
        self.cv.coords(self.preview, *flat)
        if self.tool == "box":
            self.cv.itemconfigure(self.preview,
                                  fill=self.color if e.state & _TK_SHIFT else "")

    def on_release(self, e):
        if self.preview is None:
            return
        self.on_drag(e)
        self.cv.delete(self.preview)
        pts, self.preview, self.points = self._rel(self.points), None, []
        kind = self.tool
        if kind == "box" and e.state & _TK_SHIFT:
            kind = "fill"
        if kind not in ("pen", "marker"):          # for pen and marker a click is a dot
            (xa, ya), (xb, yb) = pts[0], pts[-1]
            if kind == "arrow" and math.hypot(xb - xa, yb - ya) < 4:
                return                  # a click, not an arrow
            if kind != "arrow" and (abs(xb - xa) < 2 or abs(yb - ya) < 2):
                return                  # nothing to box or blur
        self._commit(Mark(kind, self.color, self.sizes[self.tool], pts))

    def on_motion(self, e):
        cursor = "arrow" if self._in_bar(e.x, e.y) else (
            "xterm" if self.tool == "text" else "crosshair")
        if cursor != self._cursor:
            self._cursor = cursor
            self.cv.configure(cursor=cursor)

    def on_wheel(self, e):
        # A size step is one notch (a delta of 120), however many events it
        # arrives in: precision touchpads send many small deltas.
        self._wheel += e.delta
        notches = int(self._wheel / 120)        # truncates toward zero
        if not notches:
            return
        self._wheel -= notches * 120
        _default, lo, hi, step = _TOOL_SIZES[self.tool]
        size = self.sizes[self.tool] + notches * step
        self.sizes[self.tool] = min(max(size, self._px(lo)), self._px(hi))
        if self.typing:
            self.cv.itemconfigure(self.text_id, font=self._text_font())
        self._draw_toolbar()

    # ---- keys ----

    def on_key(self, e):
        """A key with no binding of its own: only typing uses these."""
        if not self.typing:
            return
        if e.keysym == "BackSpace":
            self.text = self.text[:-1]
        elif e.char and e.char.isprintable():
            self.text += e.char
        else:
            return
        self._show_text()

    def on_return(self, e):
        if not self.typing:
            self.finish()
        elif e.state & _TK_SHIFT:
            self.text += "\n"
            self._show_text()
        else:
            self._commit_text()

    def undo(self, e=None):
        if self.typing:
            self._drop_text()           # first take back the unfinished text
            return
        if not self.marks:
            return
        mark = self.marks.pop()
        try:
            self.render = render_marks(self.base, self.marks)
        except Exception:
            _log_error()
            self.marks.append(mark)     # keep the list and the screen in step
            return
        self._refresh()

    def end_text(self):
        self._commit_text()

    def finish(self):
        self._commit_text()
        self.on_done(self.render)

    def destroy(self):
        try:
            self.cv.delete("annot")
        except tk.TclError:
            pass                        # the canvas is already gone
        self.photo = None

    # ---- text ----

    def _text_font(self):
        return ("Segoe UI", -self.sizes["text"])      # negative = pixels, as in Pillow

    def _start_text(self, x, y):
        self.text_at = (x - self.box[0], y - self.box[1])
        self.text = ""
        self.text_id = self.cv.create_text(x, y, anchor="nw", fill=self.color,
                                           font=self._text_font(), tags="annot")
        self._show_text()

    def _show_text(self):
        self.cv.itemconfigure(self.text_id, text=self.text + "|")     # "|" is the caret

    def _drop_text(self):
        if self.text_id is not None:
            self.cv.delete(self.text_id)
        self.text_id, self.text_at, self.text = None, None, ""

    def _commit_text(self):
        if not self.typing:
            return
        text, at = self.text, self.text_at
        self._drop_text()
        if text.strip():
            self._commit(Mark("text", self.color, self.sizes["text"], (at,), text))

    # ---- marks and the region image ----

    def _commit(self, mark):
        try:
            self.render = apply_mark(self.render, mark)
        except Exception:
            _log_error()                # a mark that cannot be drawn is dropped
            return
        self.marks.append(mark)
        self._refresh()

    def _refresh(self):
        self.photo = ImageTk.PhotoImage(self.render)
        self.cv.itemconfigure(self.img_id, image=self.photo)

    def _new_preview(self, x, y, state):
        cv, colour, w, tool = self.cv, self.color, self.sizes[self.tool], self.tool
        if tool in ("pen", "marker"):
            return cv.create_line(x, y, x, y, fill=colour, width=w, capstyle="round",
                                  joinstyle="round", tags="annot")
        if tool == "arrow":
            head, half = _arrow_dims(w)
            return cv.create_line(x, y, x, y, fill=colour, width=w, arrow="last",
                                  arrowshape=(head, head, max(1, half - w / 2)), tags="annot")
        if tool == "box":
            return cv.create_rectangle(x, y, x, y, outline=colour, width=w,
                                       fill=colour if state & _TK_SHIFT else "", tags="annot")
        return cv.create_rectangle(x, y, x, y, outline="#ffffff", dash=(4, 4), tags="annot")

    # ---- toolbar and key help ----

    def _hit(self, x, y):
        for x0, y0, x1, y1, action in self.buttons:
            if x0 <= x < x1 and y0 <= y < y1:
                return action
        return None

    def _click(self, action):
        if action == "undo":
            self.undo()
        elif action == "cancel":
            self.on_cancel()
        elif action == "done":
            self.finish()
        else:
            self._commit_text()         # like any other click
            if action in _TOOL_SIZES:
                self.tool = action
                self._cursor = None     # pick the cursor again on the next motion
            elif action is not None and action.startswith("color"):
                self.color = _PALETTE[int(action[5:])]
            else:
                return                  # the size label or the bar's background
            self._draw_toolbar()

    def _toolbar_origin(self, tw, th):
        x0, y0, x1, _y1 = self.box
        width, height = int(self.cv["width"]), int(self.cv["height"])
        gap = self._px(8)
        if x1 + gap + tw <= width:
            x = x1 + gap                # right of the region
        elif x0 - gap - tw >= 0:
            x = x0 - gap - tw           # left of it
        else:
            x = max(0, x1 - gap - tw)   # inside its right edge
        return x, min(max(0, y0), max(0, height - th))

    def _draw_toolbar(self):
        cv = self.cv
        cell, pad = self._px(32), self._px(4)
        tw = 2 * cell + 3 * pad
        th = len(_TOOLBAR_ROWS) * (cell + pad) + pad
        x, y = self._toolbar_origin(tw, th)
        cv.delete("annot-bar")
        self.bar = (x, y, x + tw, y + th)
        self.buttons = []
        cv.create_rectangle(x, y, x + tw, y + th, fill="#202020", outline="#3a3a3a",
                            tags=("annot", "annot-bar"))
        for r, row in enumerate(_TOOLBAR_ROWS):
            by = y + pad + r * (cell + pad)
            for c, action in enumerate(row):
                bw = cell if len(row) == 2 else tw - 2 * pad
                self._draw_button(action, x + pad + c * (cell + pad), by, bw, cell)

    def _draw_button(self, action, x, y, w, h):
        cv, tags = self.cv, ("annot", "annot-bar")
        fg, m, lw = "#e0e0e0", self._px(8), self._px(2)
        cx, cy = x + w / 2, y + h / 2
        if action == "size":            # a label, not a button
            cv.create_text(cx, cy, text=f"{self.sizes[self.tool]} px", fill=fg,
                           font=("Segoe UI", -self._px(12)), tags=tags)
            return
        lit = action == self.tool or action == "done"
        cv.create_rectangle(x, y, x + w, y + h, outline="",
                            fill=ANNOT_ACCENT if lit else "#2c2c2c", tags=tags)
        self.buttons.append((x, y, x + w, y + h, action))
        x0, y0, x1, y1 = x + m, y + m, x + w - m, y + h - m
        if action == "pen":
            cv.create_line(x0, y1, x0 + (x1 - x0) * 0.35, y0, x0 + (x1 - x0) * 0.65, y1,
                           x1, y0, fill=fg, width=lw, smooth=True, tags=tags)
        elif action == "marker":
            cv.create_line(x0, cy, x1, cy, fill="#ffcc00", width=self._px(8), tags=tags)
        elif action == "arrow":
            cv.create_line(x0, y1, x1, y0, fill=fg, width=lw, arrow="last",
                           arrowshape=(self._px(8), self._px(8), self._px(4)), tags=tags)
        elif action == "box":
            cv.create_rectangle(x0, y0 + lw, x1, y1 - lw, outline=fg, width=lw, tags=tags)
        elif action == "text":
            cv.create_text(cx, cy, text="T", fill=fg, tags=tags,
                           font=("Segoe UI", -self._px(18), "bold"))
        elif action == "blur":
            q = (x1 - x0) / 3
            for i in range(3):
                for j in range(3):
                    cv.create_rectangle(x0 + i * q, y0 + j * q, x0 + (i + 1) * q,
                                        y0 + (j + 1) * q, outline="", tags=tags,
                                        fill="#9a9a9a" if (i + j) % 2 else "#5a5a5a")
        elif action.startswith("color"):
            colour = _PALETTE[int(action[5:])]
            chosen = colour == self.color
            ring = self._px(3)
            cv.create_rectangle(x + ring, y + ring, x + w - ring, y + h - ring, fill=colour,
                                outline="#ffffff" if chosen else "#3a3a3a",
                                width=ring if chosen else 1, tags=tags)
        else:                           # undo / cancel / done: Segoe UI Symbol glyphs
            glyph = {"undo": "↶", "cancel": "✕", "done": "✓"}[action]
            cv.create_text(cx, cy, text=glyph, tags=tags,
                           fill="#ffffff" if action == "done" else fg,
                           font=("Segoe UI Symbol", -self._px(18)))

    def _draw_help(self):
        cv = self.cv
        width, height = int(cv["width"]), int(cv["height"])
        band = self._px(56)
        _x0, y0, _x1, y1 = self.box
        if y0 >= band:
            y = band // 2               # at the top, where the select-phase hint was
        elif y1 <= height - band:
            y = height - band // 2      # the region covers the top: use the bottom
        else:
            return                      # it covers both: no room for the help line
        cv.create_text(width // 2, y, text=_ANNOT_HELP, fill="#e0e0e0",
                       font=("Segoe UI", 12), tags="annot")


class PyShot:
    def __init__(self, root, hotkeys):
        self.root = root
        self.hotkeys = hotkeys
        self.active = False
        self.top = None
        self.cv = None
        self.img = None
        self.mon = None
        self.win_rects = []
        self.ph_dim = None
        self.ph_sel = None
        self.ph_loupe = None
        self.start = None
        self.toast = None
        self._toast_job = None
        self._esc_active = False
        self._render_job = None
        self._pending = None
        self._drawn = None
        self._cursor = None
        self._snap_at_press = None
        self.annotate = False       # T pressed: release opens the Annotator instead of saving
        self.editor = None          # the Annotator, while the edit phase is on screen
        self._hint_text = ""
        self._ignore_press_until = 0.0  # see _begin_edit: a double-click's second half

    # ---- capture flow (always called from the main/tkinter thread) ----

    def start_capture(self):
        if self.active:
            if self.top is not None and self.top.winfo_exists():
                return          # a real overlay is already up
            # Stale flag: the overlay was destroyed behind our back (Alt+F4,
            # a Tk error) and _close_overlay never ran. Reset instead of
            # refusing every capture until the next Esc.
            self._close_overlay()
        self.active = True
        try:
            # If the previous shot's toast is still on screen, don't let it enter the new frame
            if self._kill_toast():
                self.root.update()
                time.sleep(0.05)  # let DWM actually remove the window
            # mss >= 10 uses the name 'MSS', older ones use 'mss'. The default
            # must stay lazy — `getattr(mss, "MSS", mss.mss)` would evaluate the
            # legacy name eagerly and raise once upstream drops it.
            with (getattr(mss, "MSS", None) or mss.mss)() as sct:
                self.mon, self.img = _capture(sct)
            # Enumerated AFTER the grab and BEFORE the overlay exists, so
            # PyShot's own topmost window is never a snap candidate.
            self.win_rects = _window_rects() if WINDOW_SNAP else []
            self._build_overlay()
        except Exception:
            self._close_overlay()  # also cleans up a half-built overlay
            raise

    def _build_overlay(self):
        w, h = self.img.size
        top = tk.Toplevel(self.root)
        self.top = top
        top.withdraw()
        # overrideredirect + explicit geometry instead of -fullscreen: Tk's
        # fullscreen always lands on the primary monitor, which would put the
        # overlay on a different screen than the bitmap it is showing.
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg="black")
        top.geometry(f"{w}x{h}+{self.mon['left']}+{self.mon['top']}")

        cv = tk.Canvas(top, width=w, height=h, highlightthickness=0,
                       bd=0, bg="black", cursor="crosshair")
        cv.pack(fill="both", expand=True)
        self.cv = cv

        # Frozen screen: the dimmed copy underneath, the selected region cropped
        # from the original and overlaid bright on top (the Lightshot look).
        self.ph_dim = ImageTk.PhotoImage(ImageEnhance.Brightness(self.img).enhance(DIM))
        cv.create_image(0, 0, image=self.ph_dim, anchor="nw")
        hint = "Drag to select an area"
        if WINDOW_SNAP:
            hint += "  •  click a window to grab it"
        if LOUPE:
            hint += "  •  C: copy pixel colour"
        if ANNOTATE:
            hint += "  •  T: annotate"
        hint += "  •  Esc / right-click: cancel"
        self._hint_text = hint
        self.hint_id = cv.create_text(w // 2, 28, text=hint,
                                      fill="#e0e0e0", font=("Segoe UI", 12))

        self.annotate = False
        self.start = None
        self.ph_sel = None
        self.ph_loupe = None
        self._pending = None
        self._drawn = None
        self._snap_at_press = None
        self._render_job = None
        # Creation order = stacking order (the later one stays on top)
        self.sel_img_id = cv.create_image(0, 0, anchor="nw", state="hidden")
        self.rect_id = cv.create_rectangle(0, 0, 0, 0, outline="#3aa3ff",
                                           width=1, state="hidden")
        self.size_bg_id = cv.create_rectangle(0, 0, 0, 0, fill="#202020",
                                              outline="", state="hidden")
        self.size_txt_id = cv.create_text(0, 0, anchor="nw", fill="#ffffff",
                                          font=("Consolas", 10), state="hidden")
        # The loupe block is created last so it stays above the selection
        self.guide_h_id = cv.create_line(0, 0, 0, 0, fill="#3aa3ff", state="hidden")
        self.guide_v_id = cv.create_line(0, 0, 0, 0, fill="#3aa3ff", state="hidden")
        self.loupe_img_id = cv.create_image(0, 0, anchor="nw", state="hidden")
        self.loupe_frame_id = cv.create_rectangle(0, 0, 0, 0, outline="#3aa3ff",
                                                  width=1, state="hidden")
        self.loupe_pix_id = cv.create_rectangle(0, 0, 0, 0, outline="#ff3b30",
                                                width=1, state="hidden")
        self.loupe_bg_id = cv.create_rectangle(0, 0, 0, 0, fill="#202020",
                                               outline="", state="hidden")
        self.loupe_txt_id = cv.create_text(0, 0, anchor="nw", fill="#ffffff",
                                           font=("Consolas", 10), state="hidden")
        # The hint was created first (it is the backdrop's caption) but the hover
        # preview is live from the first frame now, so without this the hint —
        # the only in-overlay advertisement of Esc/right-click/C — is painted over.
        cv.tag_raise(self.hint_id)

        cv.bind("<ButtonPress-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_move)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Motion>", self._on_hover)
        top.bind("<Escape>", self.escape)
        top.bind("<Button-3>", self._cancel)
        top.bind("<KeyPress-c>", self._copy_colour)
        top.bind("<KeyPress-C>", self._copy_colour)
        if ANNOTATE:
            top.bind("<KeyPress-t>", self._toggle_annotate)
            top.bind("<KeyPress-T>", self._toggle_annotate)
        # Alt+F4 would otherwise let Tk destroy the overlay behind our back,
        # leaving active=True and every later hotkey a no-op.
        top.protocol("WM_DELETE_WINDOW", self._cancel)

        # Start with the loupe already under the cursor instead of waiting for
        # the first <Motion> event.
        pt = wintypes.POINT()
        if _U32.GetCursorPos(ctypes.byref(pt)):
            self._cursor = (pt.x - self.mon["left"], pt.y - self.mon["top"])
            self._snap_at_press = self._snap_box(*self._cursor)
            self._pending = self._snap_at_press

        top.deiconify()
        top.lift()
        top.focus_force()
        try:
            # Windows doesn't always give focus to background processes
            # (foreground lock); try to actually bring the window to the front.
            top.update_idletasks()
            hwnd = _U32.GetAncestor(top.winfo_id(), GA_ROOT)
            # Tk's geometry parser is unreliable for negative origins (a monitor
            # left of / above the primary), so position and size are enforced here.
            _U32.SetWindowPos(hwnd, HWND_TOPMOST, self.mon["left"], self.mon["top"],
                              w, h, SWP_SHOWWINDOW)
            _U32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        # If focus still doesn't arrive, tk's <Escape> binding never fires; a
        # temporary system-wide registration that captures Esc while the overlay
        # is open provides the safety net (released in _close_overlay when the
        # overlay closes). The same caveat applies to the C shortcut, which has
        # no such net — it simply does nothing when focus was denied.
        self.hotkeys.capture_esc()
        self._esc_active = True
        self._schedule_render()

    # ---- mouse/keyboard events ----

    def _snap_box(self, x, y):
        """The window frame under (x, y) as an image-space crop box, or None."""
        if not self.win_rects or self.img is None:
            return None
        hit = _window_at(self.win_rects, x + self.mon["left"], y + self.mon["top"])
        if hit is None:
            return None
        iw, ih = self.img.size
        box = (max(0, hit[0] - self.mon["left"]), max(0, hit[1] - self.mon["top"]),
               min(iw, hit[2] - self.mon["left"]), min(ih, hit[3] - self.mon["top"]))
        if box[2] - box[0] < MIN_SEL or box[3] - box[1] < MIN_SEL:
            return None
        return box

    def _on_hover(self, e):
        if self.start is not None or self.img is None:
            return              # a drag is in progress: _on_move owns the box
        self._cursor = (e.x, e.y)
        self._pending = self._snap_box(e.x, e.y) if WINDOW_SNAP else None
        self._schedule_render()

    def _on_press(self, e):
        if self.img is None or self.cv is None:
            return
        w, h = self.img.size
        self.start = (min(max(e.x, 0), w - 1), min(max(e.y, 0), h - 1))
        self._cursor = (e.x, e.y)
        # Remembered here so a click with no drag can still commit the window
        # under the cursor even after the 1x1 drag box has replaced _pending.
        self._snap_at_press = self._snap_box(e.x, e.y) if WINDOW_SNAP else None
        self.cv.itemconfigure(self.hint_id, state="hidden")

    def _on_move(self, e):
        if not self.start or self.cv is None:
            return
        # Producing a full PhotoImage on every mouse event stutters on large
        # selections; the events are coalesced and the latest box is drawn once
        # every RENDER_MS.
        self._cursor = (e.x, e.y)
        self._pending = sel_box(self.start, e.x, e.y, *self.img.size)
        self._schedule_render()

    def _schedule_render(self):
        if self._render_job is None and self.cv is not None:
            # Scheduled on the long-lived root rather than on the canvas: a
            # timer that outlives the overlay then calls a command that is
            # still registered and no-ops below, instead of firing
            # "invalid command name" into Tk's bgerror dialog — which
            # report_callback_exception does not intercept.
            self._render_job = self.root.after(RENDER_MS, self._render)

    def _render(self):
        self._render_job = None
        if self.cv is None or self.img is None or self.editor is not None:
            return              # (in the edit phase the Annotator owns the canvas)
        self._draw_selection(self._pending)
        self._draw_loupe(self._cursor)

    def _draw_selection(self, box):
        cv = self.cv
        items = (self.sel_img_id, self.rect_id, self.size_bg_id, self.size_txt_id)
        if box is None:
            if self._drawn is not None:
                for i in items:
                    cv.itemconfigure(i, state="hidden")
                self._drawn = None
            return
        if box == self._drawn:
            return              # nothing moved — don't rebuild the PhotoImage
        self._drawn = box
        x0, y0, x1, y1 = box
        self.ph_sel = ImageTk.PhotoImage(self.img.crop(box))
        cv.itemconfigure(self.sel_img_id, image=self.ph_sel, state="normal")
        cv.coords(self.sel_img_id, x0, y0)
        cv.itemconfigure(self.rect_id, state="normal")
        cv.coords(self.rect_id, x0, y0, x1, y1)
        # live size label — place it first, then re-clamp using its real width so
        # it doesn't overflow the edge (the font can grow with DPI)
        iw, ih = self.img.size
        mode = "  · annotate" if self.annotate else ""
        cv.itemconfigure(self.size_txt_id, state="normal",
                         text=f" {x1 - x0} × {y1 - y0} px{mode} ")
        ty = y0 - 24 if y0 >= 28 else y1 + 6
        cv.coords(self.size_txt_id, x0, ty)
        bb = cv.bbox(self.size_txt_id)
        lw, lh = bb[2] - bb[0], bb[3] - bb[1]
        tx = max(2, min(x0, iw - lw - 2))
        ty = max(2, min(ty, ih - lh - 2))
        cv.coords(self.size_txt_id, tx, ty)
        cv.coords(self.size_bg_id, *cv.bbox(self.size_txt_id))
        cv.itemconfigure(self.size_bg_id, state="normal")

    def _draw_loupe(self, cursor):
        cv = self.cv
        block = (self.guide_h_id, self.guide_v_id, self.loupe_img_id,
                 self.loupe_frame_id, self.loupe_pix_id,
                 self.loupe_bg_id, self.loupe_txt_id)
        if not LOUPE or cursor is None:
            for i in block:
                cv.itemconfigure(i, state="hidden")
            return
        iw, ih = self.img.size
        cx = min(max(cursor[0], 0), iw - 1)
        cy = min(max(cursor[1], 0), ih - 1)
        # Guides only before the drag starts: during a drag the selection
        # rectangle already shows both axes and the lines just add noise.
        if self.start is None:
            cv.coords(self.guide_h_id, 0, cy, iw, cy)
            cv.coords(self.guide_v_id, cx, 0, cx, ih)
            cv.itemconfigure(self.guide_h_id, state="normal")
            cv.itemconfigure(self.guide_v_id, state="normal")
        else:
            cv.itemconfigure(self.guide_h_id, state="hidden")
            cv.itemconfigure(self.guide_v_id, state="hidden")
        half = LOUPE_SRC // 2
        side = LOUPE_SRC * LOUPE_ZOOM
        # PIL crop pads out-of-range coordinates with black instead of raising,
        # so the loupe degrades to a black margin at the screen edges.
        src = self.img.crop((cx - half, cy - half, cx + half + 1, cy + half + 1))
        self.ph_loupe = ImageTk.PhotoImage(src.resize((side, side), Image.NEAREST))
        r, g, b = self.img.getpixel((cx, cy))
        # The readout is measured, not guessed: its size grows with the DPI, so a
        # hardcoded reserve would clip it against the bottom edge (the selection
        # label above re-measures for the same reason).
        cv.itemconfigure(self.loupe_txt_id, state="normal",
                         text=f" {cx},{cy}  #{r:02x}{g:02x}{b:02x} ")
        cv.coords(self.loupe_txt_id, 0, 0)
        tb = cv.bbox(self.loupe_txt_id)
        bw = max(side, tb[2] - tb[0])                 # whole block, loupe + bar
        bh = side + 3 + (tb[3] - tb[1])
        lx, ly = cx + LOUPE_PAD, cy + LOUPE_PAD       # flip away from the far edges
        if lx + bw > iw:
            lx = cx - LOUPE_PAD - bw
        if ly + bh > ih:
            ly = cy - LOUPE_PAD - bh
        lx = min(max(lx, 0), max(0, iw - bw))
        ly = min(max(ly, 0), max(0, ih - bh))
        cv.coords(self.loupe_img_id, lx, ly)
        cv.itemconfigure(self.loupe_img_id, image=self.ph_loupe, state="normal")
        cv.coords(self.loupe_frame_id, lx, ly, lx + side, ly + side)
        px, py = lx + half * LOUPE_ZOOM, ly + half * LOUPE_ZOOM
        cv.coords(self.loupe_pix_id, px, py, px + LOUPE_ZOOM, py + LOUPE_ZOOM)
        cv.coords(self.loupe_txt_id, lx, ly + side + 3)
        cv.coords(self.loupe_bg_id, *cv.bbox(self.loupe_txt_id))
        for i in (self.loupe_frame_id, self.loupe_pix_id, self.loupe_bg_id):
            cv.itemconfigure(i, state="normal")

    def _on_release(self, e):
        if not self.start:
            return
        box = sel_box(self.start, e.x, e.y, *self.img.size)
        self.start = None
        clicked = (box[2] - box[0]) < MIN_SEL or (box[3] - box[1]) < MIN_SEL
        if clicked:
            # Not a drag but a click: grab the window under it, or treat it as
            # an accidental click and cancel.
            box = self._snap_at_press
            if box is None:
                self._cancel()
                return
        if self.annotate:
            self._begin_edit(box, clicked)
            return
        region = self.img.crop(box)
        self._close_overlay()
        self.root.update_idletasks()  # make the overlay disappear instantly, then save
        self._deliver(region)

    def _deliver(self, region):
        """Save and/or copy one capture, then report exactly what happened."""
        want_file = CAPTURE_MODE in ("both", "file")
        want_clip = CAPTURE_MODE in ("both", "clipboard")
        path = None
        if want_file:
            try:
                path = _save_png(region)
            except Exception:
                _log_error()
        # The clipboard needs no filesystem, so it runs even when saving failed:
        # a full/read-only/offline Screenshots folder must not throw the capture away.
        ok = _copy_to_clipboard(region) if want_clip else None
        bits = []
        if want_file:
            bits.append(f"✓ {path.name} saved" if path
                        else "✗ Save error!  Details: %TEMP%\\pyshot.log")
        if want_clip:
            bits.append("copied to clipboard" if ok else "clipboard copy failed!")
        self._toast("  •  ".join(bits) or "✓ Captured", path=path)

    # ---- the edit phase (annotation mode) ----

    def _begin_edit(self, box, clicked=False):
        """Hand the overlay's canvas to an Annotator for the selected region.

        clicked: the edit phase was opened by a click on a window rather than a
        drag, so the next press may be the second half of a double-click.
        """
        cv, top = self.cv, self.top
        if self._render_job is not None:
            try:
                self.root.after_cancel(self._render_job)
            except tk.TclError:
                pass
            self._render_job = None
        # A window click may not have been drawn yet: show the final box, and
        # take the select-phase furniture off the screen.
        self._pending = box
        self._draw_selection(box)
        for i in (self.hint_id, self.guide_h_id, self.guide_v_id, self.loupe_img_id,
                  self.loupe_frame_id, self.loupe_pix_id, self.loupe_bg_id,
                  self.loupe_txt_id):
            cv.itemconfigure(i, state="hidden")
        self.win_rects = []
        self.editor = ed = Annotator(cv, box, self.img.crop(box), on_done=self._finish_edit,
                                     on_cancel=self._cancel, scale=_ui_scale(top))
        for i in (self.rect_id, self.size_bg_id, self.size_txt_id):
            cv.tag_raise(i)             # the outline and the size stay visible
        cv.tag_raise("annot-bar")
        # The Annotator draws the region itself; the select-phase copy underneath
        # would only hold memory (tens of MB for a 4K region).
        cv.itemconfigure(self.sel_img_id, image="")
        self.ph_sel = None
        # The press that ended the selection gave the overlay keyboard focus, so
        # Tk's own <Escape> binding covers Esc from here on. Keeping the global
        # hotkey through a drawing session that can last minutes would swallow
        # Esc in every other app (on another monitor, say) and cancel the drawing.
        if self._esc_active:
            self.hotkeys.release_esc()
            self._esc_active = False
        # A double-click on a window would otherwise open the editor with its
        # first click and leave a pen dot with its second.
        self._ignore_press_until = (
            time.monotonic() + _U32.GetDoubleClickTime() / 1000 if clicked else 0.0)
        cv.bind("<ButtonPress-1>", self._edit_press)
        cv.bind("<B1-Motion>", ed.on_drag)
        cv.bind("<ButtonRelease-1>", ed.on_release)
        cv.bind("<Motion>", ed.on_motion)
        # Tk runs only the MOST specific binding for a key: while <KeyPress-t>
        # and <KeyPress-c> exist, typing "t" or "c" into a text mark would toggle
        # the mode, or copy a colour and close the overlay, instead of typing.
        for seq in ("<KeyPress-t>", "<KeyPress-T>", "<KeyPress-c>", "<KeyPress-C>"):
            top.unbind(seq)
        top.bind("<KeyPress>", ed.on_key)
        top.bind("<Return>", ed.on_return)
        top.bind("<KP_Enter>", ed.on_return)
        top.bind("<Control-z>", ed.undo)
        top.bind("<Control-Z>", ed.undo)
        top.bind("<MouseWheel>", ed.on_wheel)
        # Right-click cancels while selecting; here it would throw the drawing away.
        top.bind("<Button-3>", lambda _e: None)

    def _finish_edit(self, image):
        self._close_overlay()
        self.root.update_idletasks()    # make the overlay disappear instantly, then save
        self._deliver(image)

    def _edit_press(self, e):
        # Ignore the second half of the double-click that opened the editor.
        if time.monotonic() >= self._ignore_press_until:
            self.editor.on_press(e)

    def _toggle_annotate(self, event=None):
        if self.cv is None or self.editor is not None:
            return
        if event is not None and event.state & _TK_CTRL_ALT:
            return                      # Ctrl+T / Alt+T belong to other apps, as with C
        self.annotate = not self.annotate
        self.cv.itemconfigure(self.rect_id,
                              outline=ANNOT_ACCENT if self.annotate else "#3aa3ff")
        self.cv.itemconfigure(self.hint_id,
                              text=_ANNOT_ON_HINT if self.annotate else self._hint_text)
        self._drawn = None              # the size label names the mode: draw it again
        self._schedule_render()

    def _copy_colour(self, event=None):
        if not LOUPE or self.img is None or self._cursor is None:
            return
        if self.start is not None:
            return          # mid-drag: the user is selecting, not picking
        # Tk delivers <KeyPress-c> for Ctrl+C and Alt+C too. Ctrl+C especially
        # means "copy" everywhere else, and honouring it here would silently
        # throw the pending capture away and overwrite the clipboard with a
        # colour string. Only a bare 'c' picks.
        if event is not None and event.state & _TK_CTRL_ALT:
            return
        iw, ih = self.img.size
        cx = min(max(self._cursor[0], 0), iw - 1)
        cy = min(max(self._cursor[1], 0), ih - 1)
        r, g, b = self.img.getpixel((cx, cy))
        hexcol = f"#{r:02x}{g:02x}{b:02x}"
        self._close_overlay()
        self.root.update_idletasks()
        ok = _copy_text(hexcol)
        self._toast(f"✓ {hexcol} copied to clipboard" if ok
                    else f"✗ {hexcol} — clipboard copy failed!")

    def escape(self, event=None):
        """Esc, from the Tk binding or the global hotkey: end a text mark, else cancel."""
        if self.editor is not None and self.editor.typing:
            self.editor.end_text()
        else:
            self._cancel()

    def _cancel(self, event=None):
        self._close_overlay()

    def _close_overlay(self):
        if self.editor is not None:
            self.editor.destroy()
            self.editor = None
        self.annotate = False
        if self._render_job is not None:
            try:
                self.root.after_cancel(self._render_job)
            except tk.TclError:
                pass
        self._render_job = None
        self._pending = None
        self._drawn = None
        self._cursor = None
        self._snap_at_press = None
        self.win_rects = []
        if self._esc_active:
            self.hotkeys.release_esc()
            self._esc_active = False
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass            # already gone (e.g. destroyed by Alt+F4)
            self.top = None
        self.cv = None
        self.ph_dim = self.ph_sel = self.ph_loupe = None
        self.img = None
        self.mon = None
        self.start = None
        self.active = False

    # ---- toast ----

    def _kill_toast(self, widget=None):
        if self.toast is None:
            return False
        if widget is not None and widget is not self.toast:
            return False        # a stale timer belonging to an already-replaced toast
        if self._toast_job is not None:
            try:
                self.root.after_cancel(self._toast_job)
            except tk.TclError:
                pass
            self._toast_job = None
        try:
            self.toast.destroy()
        except tk.TclError:
            pass
        self.toast = None
        return True

    def _toast(self, msg, path=None, ms=None):
        self._kill_toast()  # close any overlapping old toast
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        text = msg
        if path is not None:
            text += "\nclick: open  •  right-click: show in folder"
        tk.Label(t, text=text, bg="#1f1f1f", fg="#ffffff", justify="left",
                 font=("Segoe UI", 10), padx=14, pady=9,
                 cursor="hand2" if path is not None else "").pack()
        if path is not None:
            # A binding on the Toplevel also fires for clicks on the packed
            # Label, because Tk puts the toplevel in each child's bindtags.
            t.bind("<Button-1>", lambda _e, p=path: self._open(p, False))
            t.bind("<Button-3>", lambda _e, p=path: self._open(p, True))
        t.update_idletasks()
        area = _work_area()     # real work area: honours a taskbar on any edge
        if area:
            _al, _at, right, bottom = area
        else:
            right, bottom = t.winfo_screenwidth(), t.winfo_screenheight() - 48
        t.geometry(f"+{right - t.winfo_width() - 24}+{bottom - t.winfo_height() - 16}")
        self.toast = t
        # Scheduled on the long-lived root, identity-bound and cancelled in
        # _kill_toast: a timer left armed on a destroyed widget fires as
        # "invalid command name" into Tk's bgerror dialog, which
        # report_callback_exception does not intercept.
        self._toast_job = self.root.after(TOAST_MS if ms is None else ms,
                                          lambda w=t: self._kill_toast(w))

    def _open(self, path, reveal):
        self._kill_toast()
        try:
            if reveal:
                # One pre-built command line, NOT a list: the list form makes
                # subprocess quote "/select,C:\...\a b.png" as a single token and
                # explorer then ignores it and opens Documents instead.
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                os.startfile(path)
        except Exception:
            _log_error()


def main():
    _single_instance_or_exit()
    if _DPI_LEVEL != "per-monitor-v2":
        # Logged only when degraded, so "the capture looks blurry" has something
        # to read and a healthy system still writes nothing.
        _log_msg(f"DPI awareness: {_DPI_LEVEL} (Per-Monitor v2 unavailable) — "
                 "captures can look scaled on a HiDPI display.")
    # If the file was moved, silently update the startup shortcut to the current path
    _sync_startup_path()
    # --startup "remove completely" can close us by signaling this event
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

    # The capture/quit hotkeys live on the RegisterHotKey thread. As WM_HOTKEY
    # arrives it only drops events onto the 'events' queue without touching tkinter.
    hotkeys = HotkeyManager(events)
    hotkeys.start()
    app = PyShot(root, hotkeys)

    def dispatch(kind, payload):
        if kind == "capture":
            app.start_capture()
        elif kind == "cancel":
            if app.active:
                app.escape()
        elif kind == "hotkey_failed":
            names = " and ".join(f"{what} ({pretty_hotkey(spec)})" for what, spec in payload)
            app._toast(f"⚠ Already used by another app: the {names} hotkey"
                       f"{'s' if len(payload) > 1 else ''} — pick another one in "
                       "pyshot.py, details in %TEMP%\\pyshot.log", ms=9000)

    def pump():
        """False when PyShot should stop."""
        # quit signal from --startup (0 = WAIT_OBJECT_0: signaled)
        if quit_event and _K32.WaitForSingleObject(quit_event, 0) == 0:
            return False
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                return True
            if kind == "quit":
                return False
            try:
                dispatch(kind, payload)
            except Exception:
                _log_error()    # one bad event must not drop the rest

    def poll():
        # Anything escaping this body would stop the re-arm below for good,
        # leaving a live process that ignores every hotkey — so nothing may escape.
        keep = True
        try:
            keep = pump()
        except Exception:
            _log_error()
        if not keep:
            hotkeys.stop()
            root.destroy()
            return
        root.after(POLL_MS, poll)

    root.after(POLL_MS, poll)
    root.mainloop()


USAGE = f"""PyShot {__version__} — background screenshot tool

  {_launch_cmd(background=True)}
      run in the background
  {_launch_cmd()} --startup
      add to / remove from Windows startup
  {_launch_cmd()} --help
      this message

{pretty_hotkey(HOTKEY_CAPTURE)} captures, {pretty_hotkey(HOTKEY_QUIT)} quits.
Errors are logged to %TEMP%\\pyshot.log"""


if __name__ == "__main__":
    try:
        _arg = sys.argv[1] if len(sys.argv) > 1 else ""
        if _arg in ("--startup", "--setup", "--debug"):   # --debug: the old name
            _startup_dialog()
        elif _arg in ("-h", "--help", "/?"):
            _tell(USAGE)
        elif _arg:
            # Without this, a typo silently started an invisible daemon — or
            # exited 0 with no output because the mutex was already held.
            _tell(f"unknown option: {_arg}\n\n{USAGE}", error=True)
            sys.exit(2)
        else:
            main()
    except SystemExit:
        raise
    except Exception:
        _log_error()
