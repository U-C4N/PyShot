"""Physical-pixel targets shared by the GUI and MCP; no Tk dependency."""
import ctypes
from ctypes import wintypes

import mss
import win32api
import win32gui
from PIL import Image

from .errors import PyShotError
from .output import shots_dir, unique_path

_dwm = ctypes.WinDLL("dwmapi")
_dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_dwm.DwmGetWindowAttribute.restype = ctypes.c_long


def list_displays():
    displays = []
    for handle, _, rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        x, y, right, bottom = rect
        displays.append({"monitor_id": info["Device"], "hmonitor": int(handle),
                         "x": x, "y": y, "width": right-x, "height": bottom-y,
                         "primary": bool(info["Flags"] & 1)})
    return displays


def window_rect(hwnd):
    rect = wintypes.RECT()
    if _dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
        return rect.left, rect.top, rect.right, rect.bottom
    return win32gui.GetWindowRect(hwnd)


def list_windows():
    windows = []
    def visit(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return
            if win32gui.GetClassName(hwnd) in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return
            if win32gui.GetWindowLong(hwnd, -20) & 0x20:
                return
            cloaked = wintypes.DWORD()
            if (_dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value):
                return
            x, y, right, bottom = window_rect(hwnd)
            if right-x > 8 and bottom-y > 8:
                windows.append({"window_id": str(hwnd), "title": win32gui.GetWindowText(hwnd),
                                "x": x, "y": y, "width": right-x, "height": bottom-y})
        except Exception:
            pass  # A window may disappear during enumeration.
    win32gui.EnumWindows(visit, None)
    return windows


def resolve_target(target):
    if not isinstance(target, dict):
        raise PyShotError("invalid_target", "target must be an object")
    kind = target.get("kind", "monitor")
    displays = list_displays()
    if kind == "monitor":
        ident = target.get("monitor_id", "primary")
        match = next((m for m in displays if m["monitor_id"] == ident
                      or (ident == "primary" and m["primary"])), None)
        if not match:
            raise PyShotError("target_lost", "Monitor is unavailable", {"monitor_id": ident})
        return {"kind": kind, **match}
    if kind == "window":
        match = next((w for w in list_windows() if w["window_id"] == str(target.get("window_id"))), None)
        if not match:
            raise PyShotError("target_lost", "Window is unavailable or minimized")
        return {"kind": kind, **match, "hwnd": int(match["window_id"])}
    if kind == "region":
        region = target.get("region", {})
        if any(type(region.get(k)) is not int for k in ("x", "y", "width", "height")):
            raise PyShotError("invalid_target", "Region requires integer x, y, width and height")
        x, y, w, h = (region[k] for k in ("x", "y", "width", "height"))
        if w < 2 or h < 2 or w*h > 67_108_864:
            raise PyShotError("invalid_target", "Region size is invalid")
        # WGC captures one display. Explicitly reject cross-display regions.
        match = next((m for m in displays if x >= m["x"] and y >= m["y"]
                      and x+w <= m["x"]+m["width"] and y+h <= m["y"]+m["height"]), None)
        if not match:
            raise PyShotError("invalid_target", "Select a region within one display")
        return {"kind": kind, **region, "hmonitor": match["hmonitor"],
                "monitor_x": match["x"], "monitor_y": match["y"]}
    raise PyShotError("invalid_target", "Target kind must be monitor, region or window")


def grab(sct, rect):
    shot = sct.grab({k: rect[k] for k in ("left", "top", "width", "height")})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture_screenshot(target):
    resolved = resolve_target(target)
    path = unique_path(shots_dir(), ".png")
    if resolved["kind"] == "window":
        # Capture the HWND surface even when another window overlaps it.
        import json
        from .recording import helper_query
        helper_query("screenshot", "--target", json.dumps(resolved), "--output", str(path))
        with Image.open(path) as source:
            image = source.convert("RGB")
    else:
        with (getattr(mss, "MSS", None) or mss.mss)() as sct:
            image = grab(sct, {"left": resolved["x"], "top": resolved["y"],
                               "width": resolved["width"], "height": resolved["height"]})
        image.save(path, "PNG")
    preview = image.copy()
    preview.thumbnail((1600, 1200))
    preview_path = path.with_name(path.stem + ".preview.png")
    preview.save(preview_path, "PNG")
    return {"path": str(path), "preview_path": str(preview_path), "width": image.width,
            "height": image.height, "preview_width": preview.width, "preview_height": preview.height,
            "target": resolved}
