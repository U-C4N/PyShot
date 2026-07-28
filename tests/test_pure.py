"""Tests for the pure helpers in pyshot.py.

Nothing here opens a window: importing pyshot only sets DPI awareness and
prepares ctypes prototypes — it never calls tk.Tk() — so these run headlessly
on a Windows CI runner.
"""
import re
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyshot  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    # parse_hotkey logs every fallback it takes. Without this, running the suite
    # appends bogus entries to the real %TEMP%\pyshot.log — the file the README's
    # Troubleshooting section tells users to read.
    monkeypatch.setattr(pyshot, "LOG_FILE", tmp_path / "pyshot.log")


# --- sel_box: two inclusive cursor positions → one exclusive PIL crop box ---

def test_sel_box_reaches_the_last_column_and_row():
    # Without the +1 the right-most column and bottom row could never be
    # selected, because the cursor cannot go past the last pixel.
    assert pyshot.sel_box((10, 10), 1919, 1079, 1920, 1080) == (10, 10, 1920, 1080)


def test_sel_box_single_pixel_at_origin():
    assert pyshot.sel_box((0, 0), 0, 0, 1920, 1080) == (0, 0, 1, 1)


def test_sel_box_normalises_a_drag_up_and_to_the_left():
    assert pyshot.sel_box((100, 80), 40, 20, 1920, 1080) == (40, 20, 101, 81)


def test_sel_box_clamps_both_ends_into_the_image():
    assert pyshot.sel_box((-50, -50), 5000, 5000, 800, 600) == (0, 0, 800, 600)


# --- next_free_path: a second shot inside the same second -------------------

def test_next_free_path_appends_a_sequence_number(tmp_path):
    stamp = "2026-07-28_12-00-00"
    assert pyshot.next_free_path(tmp_path, stamp) == tmp_path / f"{stamp}.png"
    (tmp_path / f"{stamp}.png").touch()
    assert pyshot.next_free_path(tmp_path, stamp) == tmp_path / f"{stamp}_2.png"
    (tmp_path / f"{stamp}_2.png").touch()
    assert pyshot.next_free_path(tmp_path, stamp) == tmp_path / f"{stamp}_3.png"


# --- to_dib: a BMP minus its 14-byte BITMAPFILEHEADER ----------------------

def test_to_dib_starts_at_the_bitmapinfoheader():
    dib = pyshot.to_dib(Image.new("RGB", (2, 2), "red"))
    assert int.from_bytes(dib[:4], "little") == 40      # sizeof(BITMAPINFOHEADER)
    # 2 px of 24-bit colour is 6 bytes, padded to a 4-byte boundary → 8 per row
    assert len(dib) == 40 + 2 * 8


def test_to_dib_flattens_alpha_to_24_bit():
    # An RGBA DIB pastes as solid black into some targets, so RGBA must go.
    dib = pyshot.to_dib(Image.new("RGBA", (2, 2), (255, 0, 0, 128)))
    assert int.from_bytes(dib[14:16], "little") == 24   # biBitCount


# --- parse_hotkey: the configurable shortcut ------------------------------

def test_parse_hotkey_matches_the_documented_default():
    assert pyshot.parse_hotkey("ctrl+shift+h") == (
        pyshot.MOD_CONTROL | pyshot.MOD_SHIFT | pyshot.MOD_NOREPEAT, 0x48)


def test_parse_hotkey_accepts_a_bare_named_key():
    assert pyshot.parse_hotkey("printscreen") == (pyshot.MOD_NOREPEAT, 0x2C)


def test_parse_hotkey_accepts_function_keys_and_alt():
    assert pyshot.parse_hotkey("ctrl+alt+f5") == (
        pyshot.MOD_CONTROL | pyshot.MOD_ALT | pyshot.MOD_NOREPEAT, 0x74)


def test_parse_hotkey_accepts_digits_and_the_windows_key():
    assert pyshot.parse_hotkey("win+1") == (pyshot.MOD_WIN | pyshot.MOD_NOREPEAT, 0x31)


@pytest.mark.parametrize("spec", ["", "ctrl+nope+h", "hyper+h", "ctrl+shift", None])
def test_parse_hotkey_falls_back_instead_of_raising(spec):
    # A typo must never leave PyShot with no working capture hotkey at all.
    assert pyshot.parse_hotkey(spec) == (
        pyshot.MOD_CONTROL | pyshot.MOD_SHIFT | pyshot.MOD_NOREPEAT, 0x48)


def test_pretty_hotkey_is_readable():
    assert pyshot.pretty_hotkey("ctrl+shift+h") == "Ctrl+Shift+H"


# --- the shipped configuration itself, not just the parser ------------------

@pytest.mark.parametrize("spec", [pyshot.HOTKEY_CAPTURE, pyshot.HOTKEY_QUIT])
def test_shipped_hotkeys_parse_without_falling_back(spec):
    # A typo in the shipped constants would otherwise pass CI green: parse_hotkey
    # swallows it and silently returns the built-in default.
    last = spec.split("+")[-1].strip().lower()
    expected = ord(last.upper()) if len(last) == 1 else pyshot._NAMED_KEYS[last]
    assert pyshot.parse_hotkey(spec)[1] == expected


def test_capture_and_quit_hotkeys_are_different_combinations():
    assert pyshot.parse_hotkey(pyshot.HOTKEY_CAPTURE) != pyshot.parse_hotkey(pyshot.HOTKEY_QUIT)


# --- version: the module and the README badge must not drift apart ----------

def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pyshot.__version__)


def test_readme_badge_matches_the_module_version():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"version-{pyshot.__version__}-" in readme
    assert f"## What's new in {pyshot.__version__}" in readme


def test_each_hotkey_has_its_own_fallback():
    # A shared fallback would make a typo in HOTKEY_QUIT collide with the capture
    # hotkey, so quit would fail to register instead of degrading gracefully.
    assert pyshot._FALLBACK["capture"] != pyshot._FALLBACK["quit"]


# --- _window_at: which window is under the cursor -------------------------

def test_window_at_prefers_the_front_of_the_z_order():
    # _window_rects returns front-first, and what is under the cursor is
    # whatever is drawn there — not whichever window happens to be smallest.
    front, behind = (0, 0, 1920, 1080), (100, 100, 400, 400)
    assert pyshot._window_at([front, behind], 200, 200) == front
    assert pyshot._window_at([behind, front], 200, 200) == behind


def test_window_at_treats_the_right_and_bottom_edge_as_outside():
    rect = (0, 0, 10, 10)
    assert pyshot._window_at([rect], 9, 9) == rect
    assert pyshot._window_at([rect], 10, 10) is None


def test_window_at_returns_none_when_nothing_contains_the_point():
    assert pyshot._window_at([(0, 0, 10, 10)], 50, 50) is None
