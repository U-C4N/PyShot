"""Tests for the Annotator: the edit phase's toolbar, live preview and marks.

It draws on a real Tk canvas, but the canvas lives in a hidden root and the
events are plain objects handed straight to the handlers, so no window appears
and nothing depends on keyboard focus.
"""
import tkinter as tk
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

import pyshot
from pyshot import Annotator, Mark

GREY = (128, 128, 128)
RED = pyshot._PALETTE[0]
RED_RGB = (255, 59, 48)
SHIFT = pyshot._TK_SHIFT


def ev(x=0, y=0, state=0, delta=0, keysym="", char=""):
    return SimpleNamespace(x=x, y=y, state=state, delta=delta, keysym=keysym, char=char)


class Calls:
    """Records what the Annotator reports back to the overlay."""

    def __init__(self):
        self.done = []
        self.cancelled = 0

    def on_done(self, image):
        self.done.append(image)

    def on_cancel(self):
        self.cancelled += 1


@pytest.fixture
def canvas(tk_root):
    cv = tk.Canvas(tk_root, width=800, height=600)
    yield cv
    cv.destroy()


@pytest.fixture
def calls():
    return Calls()


def make(canvas, calls, box=(100, 100, 300, 250), scale=1.0):
    base = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), GREY)
    return Annotator(canvas, box, base, calls.on_done, calls.on_cancel, scale=scale)


def drag(ed, *pts, state=0):
    ed.on_press(ev(*pts[0], state=state))
    for p in pts[1:]:
        ed.on_drag(ev(*p, state=state))
    ed.on_release(ev(*pts[-1], state=state))


def press_button(ed, action):
    x0, y0, x1, y1, _action = next(b for b in ed.buttons if b[4] == action)
    ed.on_press(ev((x0 + x1) // 2, (y0 + y1) // 2))


def type_text(ed, text):
    for ch in text:
        ed.on_key(ev(keysym=ch, char=ch))


def start_typing(ed, at=(150, 150)):
    press_button(ed, "text")
    ed.on_press(ev(*at))


# --- toolbar placement ----------------------------------------------------------

def test_toolbar_sits_right_of_the_region_when_there_is_room(canvas, calls):
    ed = make(canvas, calls, box=(100, 100, 300, 250))
    assert ed.bar[0] > 300
    assert ed.bar[1] == 100


def test_toolbar_moves_left_when_the_right_side_is_full(canvas, calls):
    ed = make(canvas, calls, box=(600, 100, 790, 250))
    assert ed.bar[2] < 600


def test_toolbar_goes_inside_the_region_when_neither_side_fits(canvas, calls):
    ed = make(canvas, calls, box=(20, 20, 790, 590))
    x0, _y0, x1, _y1 = ed.bar
    assert 20 <= x0 and x1 <= 790


def test_toolbar_stays_on_the_canvas_for_a_region_near_the_bottom(canvas, calls):
    ed = make(canvas, calls, box=(100, 500, 300, 590))
    assert ed.bar[3] <= 600


def test_a_toolbar_taller_than_the_screen_is_pinned_to_the_top(canvas, calls):
    # 800x600 at 200 % scaling: the bar (~656 px) cannot fit. Its top rows stay
    # reachable; Enter, Esc and Ctrl+Z still cover the ones below the edge.
    ed = make(canvas, calls, box=(100, 300, 300, 400), scale=2.0)
    assert ed.bar[1] == 0
    assert ed.bar[3] - ed.bar[1] > 600


# --- key help line --------------------------------------------------------------

def help_items(canvas):
    return [i for i in canvas.find_withtag("annot")
            if canvas.type(i) == "text" and canvas.itemcget(i, "text") == pyshot._ANNOT_HELP]


def test_the_key_help_sits_at_the_top_when_the_region_leaves_room(canvas, calls):
    make(canvas, calls, box=(100, 100, 300, 250))
    (item,) = help_items(canvas)
    assert canvas.coords(item)[1] < 100


def test_the_key_help_moves_to_the_bottom_when_the_region_covers_the_top(canvas, calls):
    make(canvas, calls, box=(100, 10, 300, 250))
    (item,) = help_items(canvas)
    assert canvas.coords(item)[1] > 250


def test_the_key_help_is_left_out_when_the_region_covers_both_ends(canvas, calls):
    make(canvas, calls, box=(100, 10, 300, 590))
    assert help_items(canvas) == []


# --- drawing --------------------------------------------------------------------

def test_a_pen_drag_becomes_one_mark_in_region_coordinates(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (150, 150), (200, 150), (250, 150))
    assert ed.marks == [Mark("pen", RED, 4, ((50, 50), (100, 50), (150, 50)))]
    assert ed.render.getpixel((100, 50)) == RED_RGB


def test_the_preview_is_removed_once_the_mark_is_committed(canvas, calls):
    ed = make(canvas, calls)
    ed.on_press(ev(150, 150))
    preview = ed.preview
    assert canvas.type(preview) == "line"
    ed.on_release(ev(200, 150))
    assert ed.preview is None
    assert canvas.find_withtag(preview) == ()


def test_a_drag_leaving_the_region_is_clamped_to_its_edge(canvas, calls):
    ed = make(canvas, calls, box=(100, 100, 300, 250))
    drag(ed, (150, 150), (500, 150))
    assert ed.marks[0].pts[-1] == (199, 50)         # the last column of a 200 px region


def test_presses_outside_the_region_and_the_toolbar_do_nothing(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (20, 20), (60, 60))
    assert ed.marks == [] and ed.preview is None


def test_presses_on_the_toolbar_never_draw_even_inside_the_region(canvas, calls):
    ed = make(canvas, calls, box=(20, 20, 790, 590))    # the bar falls inside
    x0, _y0, _x1, y1 = ed.bar
    drag(ed, (x0 + 1, y1 - 2), (x0 + 1, y1 - 60))
    assert ed.marks == []


def test_choosing_a_tool_on_the_toolbar(canvas, calls):
    ed = make(canvas, calls)
    press_button(ed, "arrow")
    assert ed.tool == "arrow"
    drag(ed, (120, 200), (250, 120))
    assert ed.marks[0].kind == "arrow"


def test_a_click_with_the_arrow_tool_draws_nothing(canvas, calls):
    ed = make(canvas, calls)
    press_button(ed, "arrow")
    drag(ed, (150, 150), (151, 150))
    assert ed.marks == []


def test_a_click_with_the_pen_leaves_a_dot(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (150, 150))
    assert ed.marks[0].pts == ((50, 50),)


def test_shift_turns_a_box_into_a_solid_one(canvas, calls):
    ed = make(canvas, calls)
    press_button(ed, "box")
    drag(ed, (120, 120), (200, 200), state=SHIFT)
    assert ed.marks[0].kind == "fill"
    assert ed.render.getpixel((60, 60)) == RED_RGB


def test_choosing_a_colour_applies_it_to_the_next_mark(canvas, calls):
    ed = make(canvas, calls)
    press_button(ed, "color3")
    drag(ed, (150, 150), (250, 150))
    assert ed.marks[0].color == pyshot._PALETTE[3]


def test_the_wheel_resizes_the_current_tool_within_its_range(canvas, calls):
    ed = make(canvas, calls)
    ed.on_wheel(ev(delta=120))
    assert ed.sizes["pen"] == 5
    for _ in range(100):
        ed.on_wheel(ev(delta=120))
    assert ed.sizes["pen"] == 30
    for _ in range(100):
        ed.on_wheel(ev(delta=-120))
    assert ed.sizes["pen"] == 1


def test_small_wheel_deltas_add_up_to_one_notch(canvas, calls):
    # Precision touchpads send many small deltas; ten of 12 make one notch.
    ed = make(canvas, calls)
    for _ in range(9):
        ed.on_wheel(ev(delta=12))
    assert ed.sizes["pen"] == 4
    ed.on_wheel(ev(delta=12))
    assert ed.sizes["pen"] == 5


def test_sizes_follow_the_dpi_scale(canvas, calls):
    ed = make(canvas, calls, scale=1.5)
    assert ed.sizes["pen"] == 6 and ed.sizes["text"] == 30


# --- undo -----------------------------------------------------------------------

def test_undo_removes_the_last_mark_and_restores_the_image(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (150, 150), (250, 150))
    drag(ed, (150, 200), (250, 200))
    ed.undo()
    assert len(ed.marks) == 1
    assert ed.render.getpixel((100, 100)) == GREY       # the second stroke is gone
    assert ed.render.getpixel((100, 50)) == RED_RGB     # the first one stays
    ed.undo()
    assert ImageChops.difference(ed.render, ed.base).getbbox() is None


def test_undo_with_nothing_to_undo_is_harmless(canvas, calls):
    ed = make(canvas, calls)
    ed.undo()
    assert ed.marks == []


def test_the_undo_button_works_like_ctrl_z(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (150, 150), (250, 150))
    press_button(ed, "undo")
    assert ed.marks == []


# --- text -----------------------------------------------------------------------

def test_typing_a_text_mark_with_t_c_and_turkish_letters(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    assert ed.typing
    type_text(ed, "Çt c@ğ")
    ed.on_return(ev())
    assert not ed.typing
    assert ed.marks == [Mark("text", RED, 20, ((50, 50),), "Çt c@ğ")]


def test_backspace_and_shift_enter_while_typing(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    type_text(ed, "ab")
    ed.on_key(ev(keysym="BackSpace"))
    ed.on_return(ev(state=SHIFT))
    type_text(ed, "c")
    ed.end_text()
    assert ed.marks[0].text == "a\nc"


def test_control_characters_are_not_typed(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    ed.on_key(ev(keysym="a", char="\x01"))          # Ctrl+A
    ed.on_key(ev(keysym="Tab", char="\t"))
    type_text(ed, "x")
    ed.end_text()
    assert ed.marks[0].text == "x"


def test_blank_text_leaves_no_mark(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    type_text(ed, "   ")
    ed.end_text()
    assert ed.marks == [] and not ed.typing


def test_clicking_elsewhere_commits_the_text_being_typed(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    type_text(ed, "one")
    ed.on_press(ev(150, 200))                       # starts the next text
    assert [m.text for m in ed.marks] == ["one"]
    assert ed.typing


def test_changing_tool_or_colour_while_typing_keeps_the_text(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    type_text(ed, "keep me")
    press_button(ed, "color2")          # commits in the colour it was typed in
    assert ed.marks == [Mark("text", RED, 20, ((50, 50),), "keep me")]
    start_typing(ed, at=(150, 200))
    type_text(ed, "and me")
    press_button(ed, "pen")
    assert [m.text for m in ed.marks] == ["keep me", "and me"]
    assert ed.marks[1].color == pyshot._PALETTE[2]


def test_undo_while_typing_discards_only_the_unfinished_text(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (150, 150), (250, 150))
    start_typing(ed, at=(150, 200))
    type_text(ed, "oops")
    ed.undo()
    assert not ed.typing
    assert [m.kind for m in ed.marks] == ["pen"]


def test_the_wheel_resizes_the_text_being_typed(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    ed.on_wheel(ev(delta=120))
    type_text(ed, "big")
    ed.end_text()
    assert ed.marks[0].size == 22


# --- finishing ------------------------------------------------------------------

def test_enter_delivers_the_rendered_image(canvas, calls):
    ed = make(canvas, calls)
    drag(ed, (150, 150), (250, 150))
    ed.on_return(ev())
    (image,) = calls.done
    assert image.getpixel((100, 50)) == RED_RGB


def test_done_commits_the_text_still_being_typed(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    type_text(ed, "last")
    press_button(ed, "done")
    assert [m.text for m in ed.marks] == ["last"]
    assert len(calls.done) == 1


def test_the_cancel_button_cancels(canvas, calls):
    ed = make(canvas, calls)
    press_button(ed, "cancel")
    assert calls.cancelled == 1 and calls.done == []


def test_destroy_removes_everything_it_drew(canvas, calls):
    ed = make(canvas, calls)
    start_typing(ed)
    ed.destroy()
    assert canvas.find_withtag("annot") == ()


def test_a_mark_that_fails_to_render_is_dropped(canvas, calls, monkeypatch):
    ed = make(canvas, calls)

    def fail(img, mark):
        raise RuntimeError("cannot draw this")

    monkeypatch.setattr(pyshot, "apply_mark", fail)
    drag(ed, (150, 150), (250, 150))
    assert ed.marks == []
    assert ImageChops.difference(ed.render, ed.base).getbbox() is None
