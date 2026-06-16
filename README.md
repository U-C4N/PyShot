<h1 align="center">PyShot 📸</h1>

<p align="center"><strong>A Lightshot-style background screenshot tool for Windows — freeze the screen, select a region, and get a lossless PNG on your Desktop and clipboard. One key, fully offline, a single file.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.x-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6.svg" alt="Platform">
  <img src="https://img.shields.io/badge/dependencies-3-orange.svg" alt="Dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</p>

PyShot runs silently in the background. Press **`Ctrl+Shift+H`** anywhere, drag to select a region, and it saves a **lossless PNG** to your Desktop *and* copies it to the clipboard — ready to paste into email, Discord, or Word with `Ctrl+V`. It's a single Python file (`pyshot.py`) with no installer, no cloud upload, and no telemetry.

- **One-key capture** — press **`Ctrl+Shift+H`** anywhere; the screen freezes and dims, and you drag to select the area you want (with a live `width × height` label).
- **Lossless PNG, always** — saved to `Desktop\Screenshots\`, never re-compressed to JPEG.
- **Dual-format clipboard** — `CF_DIB` for classic targets (Outlook / Gmail / Word) and a registered `PNG` format for modern apps (Discord / Slack) are placed on the clipboard at once.
- **Runs once, in the background, on every boot** — single-instance via a named mutex; optional auto-start on Windows login.
- **Won't break other apps' Ctrl shortcuts** — uses the native `RegisterHotKey` API, not a global keyboard hook, so `Ctrl+click`, `Ctrl+scroll-zoom`, and `Ctrl+drag` keep working everywhere.
- **DPI aware** — sharp, correct-resolution captures even at 125% / 150% scaling (Per-Monitor v2).
- **Multi-monitor** — automatically captures the primary monitor.
- **Self-healing auto-start** — move the file and the startup shortcut repairs itself to the new path, so it never "dies."
- **Fully offline** — no cloud, no accounts, no telemetry; any error is logged locally to `%TEMP%\pyshot.log`.

> **Modeled on Lightshot, minus the cloud** — everything stays on your machine; nothing is ever uploaded.

## Installation

```bash
pip install mss Pillow pywin32
```

| Package | Purpose |
|---|---|
| [`mss`](https://pypi.org/project/mss/) | Raw, fast, lossless screen capture |
| [`Pillow`](https://pypi.org/project/Pillow/) | Image processing, cropping, PNG saving |
| [`pywin32`](https://pypi.org/project/pywin32/) | Clipboard and startup-shortcut handling |

Requires **Windows** (Vista+, best on Windows 10 1703+) and **Python 3.x**.

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
| **`Ctrl+Shift+H`** | **Freeze the screen, select an area with the mouse, and capture it** |
| `Ctrl+Shift+Q` | Quit the program completely |
| `Esc` *(overlay open)* | Cancel the capture |
| Right-click *(overlay open)* | Cancel the capture |

A selection smaller than 3 pixels is treated as an accidental click and cancelled.

## Output

Every capture goes to two places:

```
Desktop\Screenshots\YYYY-MM-DD_HH-MM-SS.png
```

1. **As a file** — e.g. `Desktop\Screenshots\2026-06-16_14-30-05.png`. A second shot within the same second gets `_2`, `_3`, … appended. The correct folder is found even if your Desktop lives on OneDrive.
2. **To the clipboard** — paste directly with `Ctrl+V` into an email, chat, or document.

## Startup (auto-run) management

```bash
python pyshot.py --debug
```

This opens a dialog and acts based on the current state:

- **Not registered** → asks *"Add to startup?"* — confirm and PyShot launches automatically in the background **every time the computer boots**.
- **Already registered** → offers *"Remove completely"* — removes the startup entry **and** closes the currently running copy.

It only manages the startup shortcut; it never deletes the `pyshot.py` file itself.

## Configuration

Tweak the constants near the top of `pyshot.py`:

| Constant | Default | Description |
|---|---|---|
| `HOTKEY_CAPTURE` | `ctrl+shift+h` | Capture shortcut (label; the real binding uses virtual key codes) |
| `HOTKEY_QUIT` | `ctrl+shift+q` | Quit shortcut |
| `DIM` | `0.35` | Dim level outside the selection (0 = black, 1 = no dimming) |
| `MIN_SEL` | `3` | Selections smaller than this (px) are treated as cancel |
| `RENDER_MS` | `15` | Refresh interval of the selection drawing (~60 fps) |

## How it works

- **Native global hotkeys** — registers `Ctrl+Shift+H` / `Ctrl+Shift+Q` via Windows `RegisterHotKey` on a dedicated message-loop thread. It intercepts only those exact combos and installs **no** system-wide keyboard hook — which is why it never disturbs other apps' Ctrl interactions.
- **Thread-safe overlay** — the hotkey thread only drops events onto a queue; the Tkinter main loop drains it via `root.after` and does all UI work, because touching Tkinter from another thread would crash.
- **Frozen Lightshot overlay** — grabs the screen with `mss` at native resolution, shows a dimmed full-screen copy, and overlays the bright, cropped selection plus a live size label, throttled to ~60 fps.
- **Dual clipboard write** — strips the 14-byte `BITMAPFILEHEADER` to produce a valid `CF_DIB`, and also writes a registered `PNG` clipboard format; it retries if the clipboard is momentarily locked by another app.
- **Single instance** — a named mutex makes a second launch exit silently, so hotkeys never fire twice; a named event lets `--debug` signal the running copy to quit.
- **Self-healing startup** — on every run it inspects the Startup shortcut and rewrites it if the script moved, so auto-start never points at a stale path.

## How it compares

| | PyShot | Lightshot |
|---|:---:|:---:|
| Freeze-and-select capture | ✓ | ✓ |
| Lossless PNG saved to disk | ✓ | ✓ |
| Fully offline — nothing uploaded to a cloud | ✓ | — |
| Open source, single file you can read & edit | ✓ | — |
| No global keyboard hook (won't disturb `Ctrl+click`) | ✓ | — |
| Two clipboard formats at once (`CF_DIB` + `PNG`) | ✓ | — |
| No install, no account | ✓ | — |

## Troubleshooting

- **Hotkey doesn't work** → `Ctrl+Shift+H` / `Ctrl+Shift+Q` may already be claimed by another app. Check `%TEMP%\pyshot.log`.
- **Capture looks blurry / too small** → a DPI-scaling issue; PyShot handles it automatically, but support is limited on very old Windows versions.
- **General errors** → all details are written to `%TEMP%\pyshot.log`.

## Limitations and disclaimer

- Windows only — relies on Win32 APIs (`RegisterHotKey`, clipboard, shell folders) and `pythonw`.
- Captures the **primary** monitor only.
- Global hotkeys are exclusive: if another app already owns `Ctrl+Shift+H` or `Ctrl+Shift+Q`, registration fails (logged, not crashed).
- Not affiliated with or endorsed by Lightshot.

## License

MIT — see [LICENSE](LICENSE). Built by [U-C4N](https://github.com/U-C4N).
