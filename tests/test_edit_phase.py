"""The edit phase wired into the real overlay: T, release, keys, Esc, delivery.

The overlay is built for real on the hidden Tk root. Only the calls that would
put it on screen (deiconify, SetWindowPos with SWP_SHOWWINDOW, SetForegroundWindow)
and the system-wide Esc registration are replaced, so nothing appears while the
tests run.
"""
import tkinter as tk
from types import SimpleNamespace

import pytest
from PIL import Image

import pyshot

GREY = (128, 128, 128)


def ev(x=0, y=0, state=0, keysym="", char=""):
    return SimpleNamespace(x=x, y=y, state=state, delta=0, keysym=keysym, char=char)


class FakeHotkeys:
    def __init__(self):
        self.released = 0

    def capture_esc(self):
        pass

    def release_esc(self):
        self.released += 1


def open_overlay(shot):
    shot.active = True
    shot.img = Image.new("RGB", (400, 300), GREY)
    shot.mon = {"left": 0, "top": 0, "width": 400, "height": 300, "work": None}
    shot._build_overlay()


@pytest.fixture
def quiet(monkeypatch):
    """Keep the overlay off the screen, however it is built."""
    monkeypatch.setattr(tk.Toplevel, "deiconify", lambda self: None)
    monkeypatch.setattr(pyshot._U32, "SetWindowPos", lambda *args: 1)
    monkeypatch.setattr(pyshot._U32, "SetForegroundWindow", lambda *args: 1)
    # Tk's focus_force and lift call SetForegroundWindow themselves; unpatched,
    # a local test run could pull focus away from the terminal.
    monkeypatch.setattr(tk.Toplevel, "focus_force", lambda self: None)
    monkeypatch.setattr(tk.Toplevel, "lift", lambda self, *args: None)


@pytest.fixture
def app(tk_root, quiet, monkeypatch):
    shot = pyshot.PyShot(tk_root, FakeHotkeys())
    shot.delivered = []
    monkeypatch.setattr(shot, "_deliver", shot.delivered.append)
    open_overlay(shot)
    yield shot
    shot._close_overlay()


def select(app, a, b):
    app._on_press(ev(*a))
    app._on_move(ev(*b))
    app._on_release(ev(*b))


def test_the_hint_advertises_t(app):
    assert "T: annotate" in app.cv.itemcget(app.hint_id, "text")


def test_annotate_off_leaves_t_unbound_and_unadvertised(tk_root, quiet, monkeypatch):
    monkeypatch.setattr(pyshot, "ANNOTATE", False)
    shot = pyshot.PyShot(tk_root, FakeHotkeys())
    open_overlay(shot)
    try:
        assert shot.top.bind("<KeyPress-t>") == ""
        assert "T: annotate" not in shot.cv.itemcget(shot.hint_id, "text")
    finally:
        shot._close_overlay()


def test_t_arms_annotation_mode_and_turns_the_outline_orange(app):
    app._toggle_annotate(ev())
    assert app.annotate
    assert app.cv.itemcget(app.rect_id, "outline") == pyshot.ANNOT_ACCENT
    app._toggle_annotate(ev())
    assert not app.annotate
    assert app.cv.itemcget(app.rect_id, "outline") == "#3aa3ff"


def test_ctrl_t_is_left_to_other_apps(app):
    app._toggle_annotate(ev(state=0x0004))
    assert not app.annotate


def test_without_t_a_release_delivers_at_once(app):
    select(app, (10, 10), (110, 60))
    (image,) = app.delivered
    assert image.size == (101, 51)
    assert app.top is None


def test_an_armed_release_opens_the_annotator_instead_of_saving(app):
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    assert isinstance(app.editor, pyshot.Annotator)
    assert app.editor.box == (10, 10, 111, 61)
    assert app.delivered == [] and app.top is not None


def test_t_pressed_during_the_drag_still_counts(app):
    app._on_press(ev(10, 10))
    app._on_move(ev(110, 60))
    app._toggle_annotate(ev())
    app._on_release(ev(110, 60))
    assert app.editor is not None and app.delivered == []


def test_an_armed_click_on_a_window_edits_that_window(app):
    app.win_rects = [(50, 40, 250, 180)]
    app._toggle_annotate(ev())
    app._on_press(ev(100, 100))
    app._on_release(ev(100, 100))                   # a click, not a drag
    assert app.editor is not None
    assert app.editor.box == (50, 40, 250, 180)


def test_the_capture_hotkey_during_editing_keeps_the_drawing(app):
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    app.editor.on_press(ev(20, 30))
    app.editor.on_release(ev(60, 30))
    editor = app.editor
    app.start_capture()                             # Ctrl+Shift+H pressed again
    assert app.editor is editor and len(editor.marks) == 1


def test_t_and_c_are_unbound_in_the_edit_phase_so_they_can_be_typed(app):
    # Tk runs only the most specific binding: a leftover <KeyPress-t> would
    # swallow every "t" typed into a text mark.
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    for seq in ("<KeyPress-t>", "<KeyPress-T>", "<KeyPress-c>", "<KeyPress-C>"):
        assert app.top.bind(seq) == ""
    assert app.top.bind("<KeyPress>") != ""
    assert app.top.bind("<Return>") != ""


def test_esc_while_typing_only_ends_the_text(app):
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    app.editor.tool = "text"
    app.editor.on_press(ev(20, 20))
    app.editor.on_key(ev(keysym="h", char="h"))
    app.escape()
    assert app.top is not None                          # still editing
    assert [m.text for m in app.editor.marks] == ["h"]
    app.escape()
    assert app.top is None and app.delivered == []      # the second Esc cancels


def test_finishing_delivers_the_annotated_region_and_closes(app):
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    app.editor.on_press(ev(20, 30))
    app.editor.on_drag(ev(100, 30))
    app.editor.on_release(ev(100, 30))
    app.editor.finish()
    assert app.top is None and app.editor is None and not app.annotate
    (image,) = app.delivered
    assert image.size == (101, 51)
    assert image.getpixel((50, 20)) == (255, 59, 48)


def test_every_capture_starts_unarmed(app):
    app._toggle_annotate(ev())
    app._close_overlay()
    open_overlay(app)
    assert not app.annotate


# --- lock keys, the Esc hand-back, double-clicks ----------------------------------

NUMLOCK, ALT, ALTGR = 0x0008, 0x20000, 0x20004     # Tk event.state bits on Windows


def test_numlock_does_not_stop_t(app):
    app._toggle_annotate(ev(state=NUMLOCK))
    assert app.annotate


@pytest.mark.parametrize("state", [ALT, ALTGR])
def test_alt_t_and_altgr_t_are_left_to_other_apps(app, state):
    app._toggle_annotate(ev(state=state))
    assert not app.annotate


def test_numlock_does_not_stop_c_copying_the_colour(app, monkeypatch):
    copied = []
    monkeypatch.setattr(pyshot, "_copy_text", lambda text: copied.append(text) or True)
    monkeypatch.setattr(app, "_toast", lambda *args, **kwargs: None)
    app._cursor = (5, 5)
    app._copy_colour(ev(state=NUMLOCK))
    assert copied == ["#808080"] and app.top is None


def test_drawing_hands_the_global_esc_back(app):
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    assert app.hotkeys.released == 1 and not app._esc_active
    app._close_overlay()
    assert app.hotkeys.released == 1                # never released twice


def test_a_double_click_on_a_window_leaves_no_dot(app):
    app.win_rects = [(50, 40, 250, 180)]
    app._toggle_annotate(ev())
    app._on_press(ev(100, 100))
    app._on_release(ev(100, 100))                   # the first click opens the editor
    app._edit_press(ev(100, 100))                   # the second click of the double-click
    app.editor.on_release(ev(100, 100))
    assert app.editor.marks == []


def test_after_the_double_click_time_a_press_draws(app):
    app.win_rects = [(50, 40, 250, 180)]
    app._toggle_annotate(ev())
    app._on_press(ev(100, 100))
    app._on_release(ev(100, 100))
    app._ignore_press_until = 0.0                   # as if the double-click time had passed
    app._edit_press(ev(100, 100))
    app.editor.on_release(ev(100, 100))
    assert len(app.editor.marks) == 1


def test_after_a_drag_the_first_press_draws_at_once(app):
    app._toggle_annotate(ev())
    select(app, (10, 10), (110, 60))
    app._edit_press(ev(50, 30))
    app.editor.on_release(ev(50, 30))
    assert len(app.editor.marks) == 1
