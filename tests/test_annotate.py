"""Tests for the annotation pure layer: a mark is data and Pillow draws it.

Nothing here opens a window, so these run headlessly, like test_pure.py.
"""
import pytest
from PIL import Image, ImageChops

import pyshot
from pyshot import Mark

RED = "#ff3b30"
RED_RGB = (255, 59, 48)
GREY = (128, 128, 128)


@pytest.fixture
def base():
    return Image.new("RGB", (120, 80), GREY)


def changed(a, b):
    """True when the two images differ anywhere."""
    return ImageChops.difference(a, b).getbbox() is not None


def noise(size):
    """An image with no two neighbouring pixels alike, so pixelation shows."""
    img = Image.new("RGB", size)
    img.putdata([((x * 37) % 256, (y * 91) % 256, ((x + y) * 13) % 256)
                 for y in range(size[1]) for x in range(size[0])])
    return img


# --- render_marks / apply_mark ----------------------------------------------

def test_no_marks_renders_an_equal_but_separate_image(base):
    out = pyshot.render_marks(base, [])
    assert out is not base
    assert not changed(out, base)


def test_apply_mark_never_modifies_its_input(base):
    before = base.copy()
    pyshot.apply_mark(base, Mark("pen", RED, 4, ((10, 10), (100, 10))))
    assert not changed(base, before)


def test_chained_apply_mark_equals_render_marks(base):
    # The Annotator commits one mark at a time and undo replays the list; the
    # two only agree because rendering is a fold.
    marks = [Mark("pen", RED, 4, ((5, 5), (60, 40), (100, 20))),
             Mark("marker", "#ffcc00", 12, ((10, 60), (110, 60))),
             Mark("box", "#0a84ff", 3, ((20, 20), (70, 50))),
             Mark("blur", RED, 6, ((0, 0), (40, 40))),
             Mark("arrow", "#34c759", 3, ((100, 70), (60, 30))),
             Mark("text", "#000000", 14, ((30, 30),), "Hi")]
    chained = base
    for mark in marks:
        chained = pyshot.apply_mark(chained, mark)
    assert not changed(chained, pyshot.render_marks(base, marks))


# --- pen ------------------------------------------------------------------------

def test_pen_colours_the_pixels_on_its_path(base):
    out = pyshot.apply_mark(base, Mark("pen", RED, 4, ((10, 40), (110, 40))))
    assert out.getpixel((60, 40)) == RED_RGB
    assert out.getpixel((60, 10)) == GREY


@pytest.mark.parametrize("width", [1, 2, 6])
def test_a_one_point_pen_stroke_leaves_a_dot_as_wide_as_the_pen(base, width):
    # Width 1 is the edge case: its disc has radius 0, which Pillow's ellipse
    # would silently not draw at all.
    out = pyshot.apply_mark(base, Mark("pen", RED, width, ((30, 30),)))
    assert out.getpixel((30, 30)) == RED_RGB
    dot = ImageChops.difference(out, base).getbbox()
    assert dot[2] - dot[0] == width
    assert out.getpixel((40, 30)) == GREY


# --- marker -------------------------------------------------------------------

def test_marker_blends_its_colour_over_what_is_underneath(base):
    out = pyshot.apply_mark(base, Mark("marker", RED, 10, ((10, 40), (110, 40))))
    a = pyshot._MARKER_ALPHA / 255
    expected = tuple(round(g + (r - g) * a) for g, r in zip(GREY, RED_RGB))
    assert all(abs(p - e) <= 1 for p, e in zip(out.getpixel((60, 40)), expected))


def test_marker_only_works_on_its_own_neighbourhood(monkeypatch):
    # Undo replays every mark. A full-size mask per Marker made 20 strokes on a
    # 4K region take ~0.6 s to replay; the mask must stay the stroke's size.
    sizes = []
    real_new = Image.new

    def spy(mode, size, *args, **kwargs):
        sizes.append(size)
        return real_new(mode, size, *args, **kwargs)

    big = real_new("RGB", (3840, 2160), GREY)
    monkeypatch.setattr(pyshot.Image, "new", spy)
    pyshot.apply_mark(big, Mark("marker", RED, 10, ((100, 100), (200, 100))))
    assert sizes and all(w * h < 200 * 200 for w, h in sizes)


def test_marker_does_not_darken_where_a_stroke_crosses_itself(base):
    # An X drawn in one stroke: both diagonals pass through (60, 40), and
    # (40, 20) lies on only one of them.
    stroke = ((20, 0), (100, 80), (100, 0), (20, 80))
    out = pyshot.apply_mark(base, Mark("marker", RED, 10, stroke))
    assert out.getpixel((60, 40)) == out.getpixel((40, 20))


# --- arrow --------------------------------------------------------------------

def test_arrow_head_tip_is_the_end_point():
    tip, _left, _right = pyshot.arrow_head(0, 0, 100, 0, 4)
    assert tip == (100, 0)


def test_arrow_head_base_is_symmetric_and_sized_by_the_stroke():
    head, half = pyshot._arrow_dims(4)
    _tip, left, right = pyshot.arrow_head(0, 0, 100, 0, 4)
    assert left[0] == pytest.approx(100 - head) and right[0] == pytest.approx(100 - head)
    assert left[1] == pytest.approx(half) and right[1] == pytest.approx(-half)


def test_a_thicker_arrow_gets_a_bigger_head():
    thin = pyshot.arrow_head(0, 0, 200, 0, 2)
    thick = pyshot.arrow_head(0, 0, 200, 0, 8)
    assert thick[1][0] < thin[1][0]         # its base sits further back from the tip


def test_a_zero_length_arrow_has_no_head_and_draws_nothing(base):
    assert pyshot.arrow_head(5, 5, 5, 5, 4) is None
    out = pyshot.apply_mark(base, Mark("arrow", RED, 4, ((50, 50), (50, 50))))
    assert not changed(out, base)


def test_arrow_draws_its_shaft_and_its_head(base):
    out = pyshot.apply_mark(base, Mark("arrow", RED, 3, ((10, 40), (110, 40))))
    assert out.getpixel((30, 40)) == RED_RGB        # shaft
    assert out.getpixel((105, 40)) == RED_RGB       # head, just behind the tip


# --- box / fill -----------------------------------------------------------------

def test_box_colours_its_edge_and_leaves_the_inside_alone(base):
    out = pyshot.apply_mark(base, Mark("box", RED, 3, ((20, 20), (80, 60))))
    assert out.getpixel((20, 40)) == RED_RGB        # on the left edge
    assert out.getpixel((19, 40)) == RED_RGB        # centred: part of the line is outside
    assert out.getpixel((50, 40)) == GREY           # inside


def test_box_does_not_care_which_way_it_was_dragged(base):
    a = pyshot.apply_mark(base, Mark("box", RED, 3, ((20, 20), (80, 60))))
    b = pyshot.apply_mark(base, Mark("box", RED, 3, ((80, 60), (20, 20))))
    assert not changed(a, b)


def test_fill_colours_the_whole_box(base):
    out = pyshot.apply_mark(base, Mark("fill", RED, 3, ((20, 20), (80, 60))))
    assert out.getpixel((50, 40)) == RED_RGB
    assert out.getpixel((90, 40)) == GREY


# --- blur -----------------------------------------------------------------------

def test_blur_makes_every_block_one_colour():
    img = noise((64, 48))
    out = pyshot.apply_mark(img, Mark("blur", RED, 8, ((0, 0), (63, 47))))
    for by in range(0, 48, 8):
        for bx in range(0, 64, 8):
            block = {out.getpixel((bx + i, by + j)) for i in range(8) for j in range(8)}
            assert len(block) == 1


def test_blur_leaves_the_pixels_outside_its_box_alone():
    img = noise((64, 48))
    out = pyshot.apply_mark(img, Mark("blur", RED, 8, ((0, 0), (31, 23))))
    assert out.crop((32, 0, 64, 48)).tobytes() == img.crop((32, 0, 64, 48)).tobytes()
    assert out.crop((0, 24, 64, 48)).tobytes() == img.crop((0, 24, 64, 48)).tobytes()


def test_pixelate_clamps_a_box_hanging_off_the_image():
    img = noise((40, 40))
    pyshot.pixelate(img, (-20, -20, 1000, 1000), 10)
    assert len({img.getpixel((i, j)) for i in range(10) for j in range(10)}) == 1


def test_pixelate_with_an_empty_box_is_a_no_op():
    img = noise((40, 40))
    before = img.copy()
    pyshot.pixelate(img, (10, 10, 10, 30), 4)
    assert not changed(img, before)


# --- text -----------------------------------------------------------------------

def test_turkish_text_renders(base):
    out = pyshot.apply_mark(base, Mark("text", "#000000", 20, ((5, 5),), "Çığ öşü İ"))
    assert changed(out, base)


def test_empty_text_changes_nothing(base):
    out = pyshot.apply_mark(base, Mark("text", "#000000", 20, ((5, 5),), ""))
    assert not changed(out, base)


def test_font_falls_back_when_segoe_ui_is_missing(monkeypatch, tmp_path, base):
    monkeypatch.setattr(pyshot, "_FONT_FILES", (tmp_path / "missing.ttf",))
    pyshot._font.cache_clear()
    try:
        out = pyshot.apply_mark(base, Mark("text", "#000000", 20, ((5, 5),), "fallback"))
        assert changed(out, base)
    finally:
        pyshot._font.cache_clear()      # the next test must see the real font again


# --- coordinates ----------------------------------------------------------------

@pytest.mark.parametrize("kind", ["pen", "marker", "arrow", "box", "fill", "blur", "text"])
def test_coordinates_far_outside_the_image_do_not_raise(base, kind):
    mark = Mark(kind, RED, 6, ((-500, -500), (5000, 5000)), "x")
    assert pyshot.apply_mark(base, mark).size == base.size


def test_a_stroke_leaving_the_image_is_cut_at_the_edge(base):
    out = pyshot.apply_mark(base, Mark("pen", RED, 4, ((60, 40), (500, 40))))
    assert out.getpixel((119, 40)) == RED_RGB
    assert out.getpixel((0, 40)) == GREY            # nothing wrapped round to the left
