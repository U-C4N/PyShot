"""Win32 helpers. DPI awareness must be set before creating any windows."""
import ctypes


def current_dpi_awareness(user32=None, shcore=None):
    user32 = user32 or ctypes.windll.user32
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        context = user32.GetThreadDpiAwarenessContext()
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if user32.AreDpiAwarenessContextsEqual(context, ctypes.c_void_p(-4)):
            return "per-monitor-v2"
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        awareness = user32.GetAwarenessFromDpiAwarenessContext(context)
        return {0: "none", 1: "system", 2: "per-monitor"}.get(awareness, "none")
    except (AttributeError, OSError):
        try:
            shcore = shcore or ctypes.windll.shcore
            awareness = ctypes.c_int()
            if shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) == 0:
                return {0: "none", 1: "system", 2: "per-monitor"}.get(awareness.value, "none")
        except (AttributeError, OSError):
            pass
    return "none"


def set_dpi_awareness(user32=None, shcore=None):
    user32 = user32 or ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    existing = current_dpi_awareness(user32, shcore)
    if existing != "none":
        return existing
    try:
        shcore = shcore or ctypes.windll.shcore
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return current_dpi_awareness(user32, shcore)
