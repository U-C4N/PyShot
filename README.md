<h1 align="center">PyShot 📸</h1>

<p align="center"><strong>A Lightshot-style background screenshot tool for Windows — freeze the screen, select a region, and get a lossless PNG on your Desktop and clipboard. One key, fully offline, a single file.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.1.0-blue.svg" alt="Version">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6.svg" alt="Platform">
  <img src="https://img.shields.io/badge/dependencies-3-orange.svg" alt="Dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</p>

PyShot runs silently in the background. Press **`Ctrl+Shift+H`** anywhere, drag to select a region, and it saves a **lossless PNG** to your Desktop *and* copies it to the clipboard — ready to paste into email, Discord, or Word with `Ctrl+V`. It's a single Python file (`pyshot.py`) with no installer, no cloud upload, and no telemetry.

- **One-key capture** — press **`Ctrl+Shift+H`** anywhere; the screen freezes and dims, and you drag to select the area you want (with a live `width × height` label).
- **Lossless PNG, always** — saved to `Desktop\Screenshots\`, never re-compressed to JPEG.
- **Dual-format clipboard** — `CF_DIB` for classic targets (Outlook / Gmail / Word) and a registered `PNG` format for modern apps (Discord / Slack) are placed on the clipboard at once.
- **Click a window to grab it** — the window under the cursor is highlighted; a single click captures it at its exact frame, with no border slivers or drop shadow.
- **Magnifier and colour readout** — a loupe with crosshair guides shows the pixel under the cursor and its `#rrggbb`; press `C` to copy that colour instead of taking a shot.
- **Draw on it before it is saved** — press `T` while selecting and releasing opens a toolbar instead of saving: pen, marker, arrow, box, text and pixelate, six colours, mouse-wheel sizing and undo. `Enter` delivers it like any other capture; without `T`, nothing changes.
- **Captures the monitor you are on** — the overlay opens on the screen the mouse is on, not always the primary one. Optional whole-virtual-desktop mode.
- **Your shortcut, your rules** — `HOTKEY_CAPTURE = "printscreen"` (or `"ctrl+alt+f9"`, `"ctrl+shift+f12"`, …) really does rebind it; no virtual key codes to look up.
- **Runs once, in the background, on every boot** — single-instance via a named mutex; optional auto-start on Windows login.
- **Won't break other apps' Ctrl shortcuts** — uses the native `RegisterHotKey` API, not a global keyboard hook, so `Ctrl+click`, `Ctrl+scroll-zoom`, and `Ctrl+drag` keep working everywhere.
- **DPI aware** — sharp, correct-resolution captures even at 125% / 150% scaling (Per-Monitor v2).
- **Fully offline** — no cloud, no accounts, no telemetry; any error is logged locally to `%TEMP%\pyshot.log`.

> **Modeled on Lightshot, minus the cloud** — everything stays on your machine; nothing is ever uploaded.

## What's new in 2.1.0

**New**

- Annotation mode: press `T` while the overlay is open — before the drag or during it — and releasing the selection opens a toolbar instead of saving. Pen, marker, arrow, box, text and pixelate, six colours, mouse-wheel sizing, and undo; `Enter` delivers the result exactly like a normal capture.
- `Shift` + Box paints a solid block: the safe way to hide a password, because pixelation is not a guaranteed redaction.
- What you see is what you save: every mark is drawn by Pillow the moment you release the mouse, and the saved PNG and the clipboard get those same pixels.

**Changed**

- While you are drawing, right-click no longer cancels (it would throw the drawing away), and `Esc` while typing a text mark only ends the text.

**Fixed**

- `C` (copy the pixel colour) did nothing while NumLock was on: Tk reports NumLock as a modifier, which 2.0.0 mistook for Alt. `C` and the new `T` now work either way.

## What's new in 2.0.0

**New**

- Capture follows the mouse: the overlay opens on the monitor you are actually working on (`CAPTURE_TARGET = "cursor" | "primary" | "all"`).
- Click a window to capture it, at its true DWM frame — no border slivers, no drop shadow.
- Magnifier with crosshair guides and a live `x,y  #rrggbb` readout; `C` copies that colour instead of taking a shot.
- `HOTKEY_CAPTURE` / `HOTKEY_QUIT` are parsed for real, so `"printscreen"` or `"ctrl+alt+f9"` just works — previously they were labels that did nothing.
- `CAPTURE_MODE` picks the sinks: `both`, `clipboard` (nothing accumulates on your Desktop) or `file`.
- The toast is clickable: open the shot, or right-click to reveal it in Explorer.
- A missing dependency, a hotkey another app already owns, and a second launch all say so on screen instead of failing silently under `pythonw`.
- Tests (`pytest`), CI on `windows-latest`, `requirements.txt`, and `pyshot.spec` for a single-file `PyShot.exe`.

**Fixed**

- A save error no longer throws the capture away — the clipboard copy still happens.
- A replaced toast no longer leaves an armed timer behind, which surfaced as a modal Tcl error dialog in a tool whose whole premise is being silent. The selection renderer had the same latent flaw.
- Alt+F4 on the overlay no longer wedges PyShot into ignoring every later hotkey, and nothing escaping the event loop can stop it re-arming.
- `Esc` registration, `--startup` reporting, `explorer /select` on paths with spaces, and the startup shortcut's self-repair all behave as documented.

**Changed**

- The capture target defaults to the monitor under the cursor rather than always the primary one.
- A click without a drag grabs the window under it; previously it always cancelled.
- `--debug` is now `--startup` (the old name still works), `--help` exists, and an unknown option is reported instead of silently starting a background copy.

## Installation

```bash
pip install -r requirements.txt
```

| Package | Purpose |
|---|---|
| [`mss`](https://pypi.org/project/mss/) | Raw, fast, lossless screen capture |
| [`Pillow`](https://pypi.org/project/Pillow/) | Image processing, cropping, PNG saving |
| [`pywin32`](https://pypi.org/project/pywin32/) | Clipboard, window enumeration, startup shortcut |

Requires **Windows 8.1+** (best on Windows 10 1703+, which has Per-Monitor-v2 DPI) and **Python 3.9+**. If a package is missing, PyShot says so in a message box instead of failing silently.

Prefer not to install Python at all? Build a single self-contained executable:

```bash
pip install pyinstaller
pyinstaller pyshot.spec      # → dist\PyShot.exe
```

Double-click `PyShot.exe` to run it in the background; `PyShot.exe --startup` manages auto-start and `PyShot.exe --help` prints usage, exactly like the script forms below.

## Quick start

Run in the background without a console window (recommended):

```bash
pythonw pyshot.py
```

> `pythonw` (note the trailing **w**) runs with no black console window. Use plain `python pyshot.py` if you want to see output while debugging.

Then just press **`Ctrl+Shift+H`** whenever you want a screenshot.

## Shortcuts

| Shortcut | Action |
|---|---|
| **`Ctrl+Shift+H`** | **Freeze the screen and open the selection overlay** |
| *Drag* | Select a region and capture it |
| *Click a window* | Capture that window's exact frame |
| `C` *(overlay open)* | Copy the `#rrggbb` under the cursor instead of capturing |
| `T` *(overlay open)* | Annotation mode: releasing the selection opens the toolbar instead of saving (`T` again turns it off) |
| `Esc` *(overlay open)* | Cancel the capture |
| Right-click *(overlay open)* | Cancel the capture |
| `Ctrl+Shift+Q` | Quit the program completely |

A drag smaller than 3 pixels counts as a click, not a selection: it grabs the window under the cursor, or cancels when there is none. `Esc` works even if Windows refuses to give the overlay keyboard focus (it is registered as a system-wide hotkey while you select; once you are drawing, the overlay has focus and Esc pressed in any other app is left to that app); `C` and `T` have no such safety net, so they depend on focus. `T` also works mid-drag, and pressing the mouse on the overlay is what gives it focus.

### Annotation mode

With `T` on, the outline turns orange. After you release — or click a window — the region stays on screen with a toolbar beside it; marks are clipped to the region.

| Input | Action |
|---|---|
| *Drag inside the region* | Draw with the current tool: pen, marker, arrow, box, text (click, then type) or pixelate |
| `Shift` + *drag* with Box | Solid box — use this, not pixelate, for passwords |
| *Mouse wheel* | Size of the current tool (stroke, text size, pixel block) |
| `Enter` / ✓ | Done: save and/or copy, like a normal capture |
| `Ctrl+Z` / ↶ | Undo the last mark (while typing: drop the unfinished text) |
| `Esc` / ✕ | Cancel the capture (while typing, `Esc` only ends the text) |

Right-click does nothing while you are drawing, so a stray click cannot throw the drawing away. `Shift+Enter` starts a new line in a text mark.

## Output

Every capture goes to two places:

```
Desktop\Screenshots\YYYY-MM-DD_HH-MM-SS.png
```

1. **As a file** — e.g. `Desktop\Screenshots\2026-06-16_14-30-05.png`. A second shot within the same second gets `_2`, `_3`, … appended. The correct folder is found even if your Desktop lives on OneDrive.
2. **To the clipboard** — paste directly with `Ctrl+V` into an email, chat, or document.

A small toast then appears in the bottom-right corner (above the taskbar, wherever it is docked) for ~2.5 s, naming the file and reporting whether the clipboard write succeeded. **When a file was written, click the toast to open the shot and right-click it to reveal it in Explorer.** The two sinks are independent: if the folder is read-only, full, or an offline OneDrive path, the clipboard copy still happens and the capture is not lost — and PyShot retries a locked clipboard 10 times over ~1 s before reporting failure.

Set `CAPTURE_MODE = "clipboard"` to stop writing files altogether (nothing accumulates on your Desktop), or `"file"` so captures never touch the clipboard.

## Startup (auto-run) management

```bash
python pyshot.py --startup
```

This opens a dialog and acts based on the current state:

- **Not registered** → asks *"Add to startup?"* — confirm and PyShot launches automatically in the background **every time the computer boots**.
- **Already registered** → reports whether the shortcut still points at *this* copy, repairs it if not, and offers *"Remove completely"*, which removes the startup entry **and** closes the currently running copy.

It only manages the startup shortcut; it never deletes the `pyshot.py` file itself. `--setup` and the older `--debug` are accepted as aliases; `--help` prints usage, and an unrecognised option is reported instead of silently starting a background copy.

## Configuration

Tweak the constants near the top of `pyshot.py`:

| Constant | Default | Description |
|---|---|---|
| `HOTKEY_CAPTURE` | `ctrl+shift+h` | Capture shortcut. `ctrl` / `shift` / `alt` / `win` plus a letter, digit, `f1`–`f24`, `printscreen`, `insert`, `home`, `space`, … A spec that cannot be parsed falls back to the default and says why in the log. |
| `HOTKEY_QUIT` | `ctrl+shift+q` | Quit shortcut, same syntax |
| `CAPTURE_TARGET` | `cursor` | Which screen freezes: `cursor` (the one the mouse is on), `primary`, or `all` (the whole virtual desktop) |
| `CAPTURE_MODE` | `both` | `both`, `clipboard` (no file written), or `file` (captures don't touch the clipboard; the `C` colour shortcut still copies) |
| `WINDOW_SNAP` | `True` | Highlight the window under the cursor and let a click capture it |
| `LOUPE` | `True` | Magnifier, crosshair guides and the coordinate/colour readout |
| `ANNOTATE` | `True` | The `T` key: annotate a capture before it is saved |
| `TELL_IF_RUNNING` | `True` | A second launch answers "already running" instead of exiting mutely |
| `DIM` | `0.35` | Dim level outside the selection (0 = black, 1 = no dimming) |
| `MIN_SEL` | `3` | Drags smaller than this (px) count as a click |
| `RENDER_MS` | `15` | Refresh interval of the selection drawing (~60 fps) |
| `POLL_MS` | `40` | How often the tkinter loop drains the hotkey queue |
| `TOAST_MS` | `2500` | How long the confirmation toast stays on screen |
| `LOUPE_SRC` / `LOUPE_ZOOM` / `LOUPE_PAD` | `17` / `8` / `20` | Loupe sampling window (px — keep it **odd**, so there is a true centre pixel to read the colour from), magnification, gap from the cursor |
| `LOG_FILE` | `%TEMP%\pyshot.log` | Where errors are appended |

## How it works

- **Native global hotkeys** — registers the parsed capture/quit combinations via Windows `RegisterHotKey` on a dedicated message-loop thread. It intercepts only those exact combos (plus bare `Esc`, and only while the overlay is on screen) and installs **no** system-wide keyboard hook — which is why it never disturbs other apps' Ctrl interactions. If a combo is already owned by another app, PyShot says so in a toast instead of just logging it.
- **Thread-safe overlay** — the hotkey thread only drops `(kind, payload)` events onto a queue; the Tkinter main loop drains it via `root.after` and does all UI work, because touching Tkinter from another thread would crash. Nothing may escape that loop, so every handler is wrapped: an escaped exception would stop the re-arm and leave a live process that ignores every hotkey.
- **Frozen Lightshot overlay** — grabs the target monitor with `mss` at native resolution, then places a borderless topmost window at that monitor's exact rectangle (Tk's `-fullscreen` always lands on the primary screen, so it is not used). The bitmap and the window are sized from the same `MONITORINFO`, so they can never disagree.
- **Window frames from DWM** — snapping reads `DWMWA_EXTENDED_FRAME_BOUNDS` rather than `GetWindowRect`, which over-reports by the invisible ~7 px resize border, and skips cloaked (suspended UWP) windows that report as visible but are not on screen. Windows are enumerated after the grab and before the overlay exists, so PyShot's own window is never a candidate.
- **Annotations are data, drawn by Pillow** — each stroke is kept as a small record and drawn onto the region by Pillow as soon as you release the mouse; the overlay shows that render, and the very same image is what gets saved and copied. Undo replays the remaining marks onto the clean capture.
- **Dual clipboard write** — strips the 14-byte `BITMAPFILEHEADER` to produce a valid `CF_DIB`, and also writes a registered `PNG` clipboard format; it retries if the clipboard is momentarily locked by another app.
- **Single instance** — a named mutex makes a second launch report "already running" and exit, so hotkeys never fire twice; a named event lets `--startup` signal the running copy to quit.
- **Self-repairing startup shortcut** — every normal launch checks the Startup `.lnk` and rewrites it if the script has moved, so auto-start stops pointing at a stale path. Note this can only heal once you run the moved copy: a shortcut pointing at a path with no file starts nothing at boot, so nothing is there to repair it.

## How it compares

| | PyShot | Lightshot |
|---|:---:|:---:|
| Freeze-and-select capture | ✓ | ✓ |
| Lossless PNG saved to disk | ✓ | ✓ |
| Magnifier + pixel colour readout | ✓ | ✓ |
| Annotation (arrow / box / blur) | ✓ | ✓ |
| Fully offline — nothing uploaded to a cloud | ✓ | — |
| Open source, single file you can read & edit | ✓ | — |
| No global keyboard hook (won't disturb `Ctrl+click`) | ✓ | — |
| Two clipboard formats at once (`CF_DIB` + `PNG`) | ✓ | — |
| No install, no account | ✓ | — |

## Troubleshooting

- **Hotkey doesn't work** → another app already owns it. PyShot shows a toast naming the combo at startup; pick another one with `HOTKEY_CAPTURE` (e.g. `"printscreen"`). Details in `%TEMP%\pyshot.log`.
- **Nothing happens at all when I double-click the file** → a dependency is missing (a message box says which) or a copy is already running (it says so too, unless you set `TELL_IF_RUNNING = False`).
- **Toast says "clipboard copy failed"** → another app was holding the clipboard lock; the PNG is still on your Desktop. Details in `%TEMP%\pyshot.log`.
- **Capture looks blurry / too small** → a DPI-scaling issue. PyShot requests Per-Monitor-v2 awareness and falls back through two older APIs; on very old Windows versions the last fallback is all that is available. When Per-Monitor v2 could not be reached, the level actually used is written to `%TEMP%\pyshot.log` at startup.
- **General errors** → all details are written to `%TEMP%\pyshot.log`.

## Development

```bash
pip install -r requirements.txt pytest ruff==0.15.5
pytest -q          # pure logic, annotation rendering, and the annotator on a hidden Tk root
ruff check .       # rule set is pinned in ruff.toml, linter version in ci.yml
```

The pure helpers (`sel_box`, `next_free_path`, `to_dib`, `parse_hotkey`, `_window_at`, and the annotation layer's `apply_mark`, `render_marks`, `arrow_head`, `pixelate`) are deliberately free of Win32 and tkinter so they can be tested headlessly. The annotator and the edit-phase wiring are tested on a hidden Tk root, with the calls that would show the overlay replaced; CI runs them on `windows-latest`, which is required rather than preferred because the module loads `user32`/`kernel32` and imports `win32clipboard` at import time.

## Limitations and disclaimer

- Windows only — relies on Win32 APIs (`RegisterHotKey`, clipboard, DWM, shell folders) and `pythonw`.
- The selection cannot be nudged or resized once you release the mouse, in annotation mode as well, and a mark cannot be moved after it is drawn — only undone.
- Pixelate hides text from a casual look but is not a guaranteed redaction: tools exist that recover text from small-block pixelation. For passwords and keys use `Shift` + Box, which paints a solid block.
- Global hotkeys are exclusive: if another app already owns your combination, registration fails (reported in a toast and the log, not a crash).
- On a mixed-DPI multi-monitor setup the overlay's hint and label text is sized from the system DPI, so it can look slightly small or large on a secondary screen. The capture itself is always at that monitor's native resolution.
- Not affiliated with or endorsed by Lightshot.

## License

MIT — see [LICENSE](LICENSE). Built by [U-C4N](https://github.com/U-C4N).
