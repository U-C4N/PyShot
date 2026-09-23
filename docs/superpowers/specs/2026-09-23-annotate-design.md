# Annotation mode (`T`) — design

- Date: 2026-09-23
- Status: awaiting review
- Target version: 2.1.0

## Goal

Let the user draw on a capture before it is delivered, without slowing down the
default capture. Pressing `T` in the capture overlay arms *annotation mode*;
releasing the selection then opens a toolbar instead of saving. Every mark is
rendered by Pillow, so after each stroke the overlay shows exactly the pixels that
will be saved and copied.

**Success:** without `T`, captures behave exactly as in 2.0.0 (the only visible
difference is the hint mentioning `T`). With `T`, the user can draw with a pen,
highlight, draw arrows and boxes, write text and pixelate inside the selection,
undo, and deliver with `Enter`. The PNG and the clipboard then hold the same
pixels the overlay showed.

**Constraints:** still a single file (`pyshot.py`); no new dependencies (Pillow's
`ImageDraw`/`ImageFont` and the stdlib are enough); Python 3.9+; Windows only.

## User-facing behaviour

### Select phase (today's overlay)

- `T` toggles annotation mode. Only a bare `t` counts; Ctrl/Alt+T are ignored,
  as they are for `C`. Lock keys are not modifiers: `T` and `C` also work with
  NumLock or CapsLock on (Tk on Windows reports NumLock as Mod1, `0x0008`).
- `T` works before or during the drag. The during-the-drag case matters: Windows
  can refuse the overlay keyboard focus when it opens, but a mouse press on the
  overlay activates it.
- While armed, the selection outline and the window-snap highlight turn orange
  (`#ff9f0a`) instead of blue, the size label reads `640 × 480 px · annotate`, and
  the top hint says annotation is on.
- Releasing a drag while armed, or clicking a window while armed, enters the edit
  phase with that box. When unarmed, release captures immediately, as today.
- `ANNOTATE = False` removes the key and its hint.

### Edit phase

- The region stays bright and the rest stays dimmed. Loupe, crosshair guides and
  window snapping are gone. A toolbar appears beside the region. The orange
  outline and the size label stay on top of the region image; the toolbar
  stays on top of everything, so its buttons are visible even when it has to
  sit inside the region.
- Default tool: Pen. Default colour: red.
- Marks are clipped to the region. Presses outside the region and outside the
  toolbar do nothing.
- Marks apply in order, so a Blur drawn over an earlier mark pixelates that mark
  too.

| Input | Action |
|---|---|
| Drag inside the region | Draw with the current tool |
| `Shift` held while dragging a Box | Solid filled box: a true redaction |
| Mouse wheel | Size of the current tool (stroke width, font px, pixel block) |
| `Enter` / ✓ | Finish: deliver through `CAPTURE_MODE` like a normal capture, toast included |
| `Ctrl+Z` / ↶ | Undo the last mark. While typing, discard the text being typed instead |
| `Esc` / ✕ | Cancel the capture. While typing, `Esc` only ends the text and keeps it |
| Right-click | Nothing. It cancels in the select phase; here it would throw the drawing away |
| `Alt+F4` | Cancel, as today |

### Tools

| Tool | Gesture | Rendered as | Default size at 96 DPI | Range |
|---|---|---|---|---|
| Pen | freehand drag | round-capped polyline; a click leaves a dot | 4 px | 1–30 |
| Marker | freehand drag | the same stroke at ~40 % opacity; self-overlaps do not darken | 18 px | 6–60 |
| Arrow | drag from tail to tip | line plus a filled triangular head | 4 px | 1–30 |
| Box | drag corner to corner | outline centred on the dragged edge; `Shift`: filled | 3 px | 1–30 |
| Text | click, then type | Segoe UI; `Enter` ends, `Shift+Enter` adds a line, `Backspace` deletes | 20 px | 10–96 |
| Blur | drag a rectangle | pixelated in square blocks | 12 px blocks | 6–48 |

- Sizes and ranges are in 96-DPI pixels and are multiplied by the system DPI
  scale (`winfo_fpixels('1i') / 96`), as is the toolbar.
- One wheel notch changes the size by 1 px (2 px for Marker, Text and Blur). A
  notch is a delta of 120, however many events it arrives in, since precision
  touchpads send many small deltas.
- For Box, the `Shift` state at release decides whether the box is filled; the
  preview follows `Shift` live.
- Colours: red `#ff3b30`, yellow `#ffcc00`, green `#34c759`, blue `#0a84ff`,
  black and white. The current colour applies to every tool except Blur.
- Pixelation hides text from a casual reader, but it is not a guaranteed
  redaction: published attacks recover text from small-block pixelation. The
  README says so and points to `Shift` + Box for secrets.

### Toolbar

Two columns, drawn on the overlay canvas itself. There is no extra window, so
focus and z-order stay as they are today. Background `#202020`:

    [ pen   ][ marker ]
    [ arrow ][ box    ]
    [ text  ][ blur   ]
    -------------------
    [ red   ][ yellow ]
    [ green ][ blue   ]
    [ black ][ white  ]
    -------------------
    [     size 4      ]
    [ undo  ][ cancel ]
    [      done       ]

- Icons are canvas primitives plus Segoe UI Symbol glyphs (↶ ✓ ✕), so there
  are no image files.
- The active tool gets an orange background; the active colour a white ring.
- Placement: right of the region. If it does not fit there, left of it; if it
  fits on neither side, inside the region's right edge. It is aligned with the
  region's top and clamped to the overlay.
- A one-line key help (`Enter`, `Ctrl+Z`, wheel, `Esc`) replaces the top hint. It
  moves to the bottom when the region covers the top of the screen and is hidden
  when the region covers both.
- UI strings stay English, like the rest of the app.

## Architecture

All new code goes into `pyshot.py` under a new "Annotation" section, split into
a pure layer and a Tk layer.

### Pure layer (no Tk, no Win32; tested headlessly)

- `Mark`: frozen dataclass with these fields:
  - `kind`: `pen`, `marker`, `arrow`, `box`, `fill`, `text` or `blur`
  - `color`: `#rrggbb`, unused by `blur`
  - `size`: int
  - `pts`: tuple of region-relative `(x, y)` points
  - `text`: str, default `""`
- `apply_mark(img, mark) -> Image`: returns a new image with one mark drawn. It
  never mutates its input.
- `render_marks(base, marks) -> Image`: `apply_mark` folded over `marks`.
  Because rendering is a fold, applying one new mark to the current render equals
  re-rendering everything. A commit therefore costs one mark, and undo is a
  replay.
- `arrow_head(x0, y0, x1, y1, size)`: the tip, left and right points of the
  head, or `None` for a zero-length arrow.
- `pixelate(img, box, block)`: pixelates the sub-box, clamped to the image. An
  empty box is a no-op.
- `_font(size)`: tries Segoe UI (`%WINDIR%\Fonts\segoeui.ttf`), then Arial, then
  Pillow's built-in font. Cached per size; never raises.

### Tk layer: `Annotator`

`PyShot` creates the `Annotator` on entering the edit phase and destroys it in
`_close_overlay`. The constructor gets:

- the canvas
- the region box
- the region's base image (`img.crop(box)`)
- two callbacks, `on_done(image)` and `on_cancel()`

The annotator owns every canvas item it creates (tag `annot`): the region image,
the toolbar, the key help, the live preview and the text being typed. Its state
is the mark list, the current render and its `PhotoImage`, the tool, the colour,
the per-tool sizes, the preview item and its points, and the text-entry state.

Data flow for one stroke:

1. Press inside the region: create a Tk preview item in the current colour and
   size. It is a line, an arrow, a rectangle, or a dashed rectangle for Blur.
2. Motion: update the preview's coordinates. This is pure Tk work, with no
   Pillow calls.
3. Release: build a `Mark` from region-relative, clamped coordinates. The result
   of `apply_mark(current, mark)` becomes the new render. Delete the preview item
   and refresh the region image once.

- **Text:** a click shows what is typed in a canvas text item, with a caret.
  `Enter`, `Esc`, any other click (outside the region too), a tool change or Done
  commits it as a `text` mark, the same way as a stroke. Empty text commits
  nothing.
- **Undo:** pop the last mark and replay `render_marks(base, marks)`.
- **Done:** commit any pending text, then call `on_done(current_render)`.

The live preview is drawn by Tk and the committed result by Pillow, so the two
can differ while a drag is in progress. The most visible case is the Marker,
which is opaque while dragging and turns translucent on release. After every
release, the screen and the output agree.

The annotator uses no `after()` timers. It has nothing to animate, so it cannot
leave a timer armed on a destroyed widget, which is the 2.0.0 toast bug.

### Changes to `PyShot`

- **`T` handling:** a `self.annotate` flag, reset on every capture. The `T`
  handler (select phase only) flips it, recolours the outline and redraws the
  size label.
- **`_on_release`:** when armed, call `_begin_edit(box)` instead of closing and
  delivering.
- **`_begin_edit(box)`:**
  - Cancel any pending select-phase render and hide the loupe, the guides and
    the hint.
  - Create the `Annotator` and rebind to it: the canvas mouse events, the wheel,
    `Return`, `Control-z` and `KeyPress`.
  - Remove the `<KeyPress-t>` and `<KeyPress-c>` bindings. Tk prefers them over
    `<KeyPress>`, so typing "t" or "c" into a text mark would otherwise toggle the
    mode, or copy a colour and close the overlay.
  - Rebind right-click to nothing.
  - Release the global Esc hotkey (see `escape()` below).
  - When a click on a window opened the edit phase, ignore presses for the
    system double-click time, so the second click of a double-click does not
    leave a pen dot.
- **`escape()`:** the single Esc entry point. While selecting, both the Tk
  binding and the global Esc hotkey call it; the hotkey swallows the key before
  Tk sees it. When the edit phase begins, the global hotkey is released: the
  press that ended the selection gave the overlay focus, so the Tk binding covers
  Esc, and Esc typed into another app (on another monitor, say) reaches that app
  instead of cancelling the drawing. While the annotator is typing, `escape()`
  ends the text; otherwise it cancels.
- **`_finish_edit(image)`:** `_close_overlay()`, then `_deliver(image)`. This is
  the same delivery path, toast and error handling as a normal capture.
- **Dispatch:** `main()`'s dispatcher sends `cancel` events to `app.escape()`.

## Error handling

- A mark that fails to render is logged and dropped. The drawing stays usable.
- There is no separate final render that could fail. The delivered image is the
  current render, and a mark enters the list only after it has drawn
  successfully, so a capture is never thrown away. This is the same rule as the
  2.0.0 save-error fix.
- If undo's replay fails, the popped mark is put back, so the list and the screen
  stay in step.
- Font loading falls back as described above and never raises.
- Everything else inherits the existing safety nets. Tk callback errors go to the
  log, and `_close_overlay` tears down the annotator along with the overlay.

## Testing

### Automated

A new `tests/test_annotate.py`, run by the existing CI, covers:

- `render_marks(base, [])` equals the base and returns a new object.
- A chain of incremental `apply_mark` calls equals `render_marks` for a mixed
  list of marks.
- Pen: pixels on the path take the colour, and a one-point stroke draws a dot.
- Marker: a pixel under the stroke is the documented blend of base and colour. A
  self-crossing stroke has the same value at the crossing, so there is no
  darkening.
- Arrow:
  - the `arrow_head` tip equals the end point
  - the base points are symmetric
  - the head length follows the size
  - a zero-length arrow returns `None` and renders nothing
- Box: the edge is coloured and the interior is untouched. `fill` colours the
  interior. Drag direction does not matter.
- Blur:
  - every block inside the box is uniform
  - pixels outside the box are untouched
  - a box hanging off the image is clamped
  - an empty box is a no-op
- Text: a Turkish string (`"Çığ öşü İ"`) renders without error and changes
  pixels. Empty text changes nothing.
- Coordinates outside the region are clipped, never wrapped or raised.
- `_font` returns a usable font when the Segoe UI path does not exist.

The Tk layer is tested too, on a hidden Tk root with events passed straight to
the handlers:

- `tests/test_annotator.py` covers the `Annotator` on a real canvas: toolbar
  placement, each gesture, clamping, text entry (including "t", "c" and Turkish
  letters), undo, the wheel, Done, cancel and teardown.
- `tests/test_edit_phase.py` builds the real overlay with only the calls that
  would show it replaced. It covers `T` (also mid-drag), release with and
  without `T`, the removed `t`/`c` bindings, the Esc routing and delivery.

### Manual checklist

These need a real screen, keyboard focus or the packaged exe, so they are
checked by hand:

- `T` before a drag and during a drag. `T` twice turns it off. A window click
  while armed.
- Each tool, wheel sizing, `Shift`+Box, undo, `Enter`, ✓, `Esc` and ✕.
  Right-click is ignored.
- Typing "t" and "c" into a text mark. Turkish letters, and AltGr characters
  (`@`, `€`).
- `T` and `C` with NumLock on.
- While drawing, Esc pressed in an app on another monitor reaches that app and
  leaves the drawing alone.
- A double-click on a window while armed opens the editor without a stray dot.
- `CAPTURE_MODE` set to `both`, `clipboard` and `file`.
- A monitor left of or above the primary.
- 150 % scaling.
- `Alt+F4` while editing.
- The capture hotkey is ignored while editing.
- `PyShot.exe` built from `pyshot.spec` renders text, which shows that FreeType
  is bundled.

## Docs and release

- Version 2.1.0: `__version__`, the README badge and a "What's new in 2.1.0"
  section. The existing tests enforce all three.
- README updates:
  - shortcuts table: `T` and the edit keys
  - configuration table: `ANNOTATE`
  - comparison table: annotation ✓
  - limitations: the "no annotation step" item goes; the selection still cannot
    be resized
  - the pixelation caveat

## Out of scope

- Resizing or moving the selection after release
- Selecting, moving or editing a mark after it is placed
- Redo
- A custom colour picker
- Tool shortcut keys
- Separate copy and save actions (`Enter` follows `CAPTURE_MODE`)
- Per-monitor DPI scaling of the toolbar (it uses the system DPI, like today's
  labels)
