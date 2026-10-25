import ctypes
from datetime import datetime
from pathlib import Path
import uuid


def desktop_dir():
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:
        return Path(buf.value)
    return Path.home() / "Desktop"


def shots_dir():
    return desktop_dir() / "Screenshots"


def next_free_path(out_dir, stamp, ext=".png"):
    path = out_dir / f"{stamp}{ext}"
    seq = 2
    while path.exists():
        path = out_dir / f"{stamp}_{seq}{ext}"
        seq += 1
    return path


def unique_path(directory, extension):
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        + "_" + uuid.uuid4().hex[:12] + extension)
