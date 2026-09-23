<h1 align="center">PyShot</h1>

<p align="center">
  <b>Press a key. Drag a box. Paste it anywhere.</b><br>
  A Lightshot-style screenshot tool for Windows: one Python file, runs in the background, never goes online.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.1.0-blue.svg" alt="Version 2.1.0">
  <img src="https://img.shields.io/badge/python-3.9%2B-3776AB.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6.svg" alt="Windows">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT license">
</p>

## Quick start

```powershell
git clone https://github.com/U-C4N/PyShot.git
cd PyShot
pip install -r requirements.txt
pythonw pyshot.py
```

This installs `mss`, `Pillow` and `pywin32`, then starts PyShot silently in the background (the **w** in `pythonw` means no console window). Press **`Ctrl+Shift+H`**, drag a box, then **`Ctrl+V`** it anywhere. Every shot is also saved as a lossless PNG in `Desktop\Screenshots\`. **`Ctrl+Shift+Q`** quits.

> Needs Windows 8.1+ (best on Windows 10 1703+) and Python 3.9+.

## What's new in 2.1.0

- **Annotation mode:** press `T` in the overlay to draw on a capture before it is saved.
- **Solid box:** `Shift` + Box paints a solid block, the safe way to hide a password.
- **Fixed:** `C` (copy colour) now works with NumLock on.

## Features

| Feature | What it does |
|---|---|
| **Freeze & select** | The monitor under your mouse freezes and dims; drag with a live `W×H` label. |
| **Window snap** | Click a window to capture its exact frame, with no border slivers or shadow. |
| **Loupe** | Magnifier with an `x,y  #rrggbb` readout; `C` copies that colour instead of capturing. |
| **Annotate** | Press `T` to draw on the shot before it is saved ([see below](#draw-before-you-save)). |
| **Lossless PNG** | `Desktop\Screenshots\YYYY-MM-DD_HH-MM-SS.png`, even when your Desktop is on OneDrive. |
| **Pastes everywhere** | The clipboard gets `CF_DIB` (Outlook, Word, Gmail) *and* `PNG` (Discord, Slack). |
| **Clickable toast** | Click it to open the shot, right-click to show it in Explorer. |
| **No keyboard hook** | Uses `RegisterHotKey`, so other apps' `Ctrl+click` and `Ctrl+scroll` keep working. |
| **Sharp on scaled displays** | Per-Monitor-v2 DPI aware: native resolution at 125% or 150%. |
| **Private** | No cloud, no account, no telemetry. Errors go to `%TEMP%\pyshot.log`. |

## Draw before you save

Press **`T`** in the overlay, before or during the drag (press it again to turn it off): the hint reads *Annotate ON* and the selection outline turns orange. Release (or click a window) and a toolbar appears beside the region:

- **Tools:** pen · marker · arrow · box · text · pixelate, plus 6 colours. The mouse wheel sets the size. For text, click, then type.
- **`Shift` + Box** paints a solid block. Use it for passwords: pixelation is not a guaranteed redaction.
- **`Ctrl+Z`** or ↶ undo · **`Enter`** or ✓ done · **`Esc`** or ✕ cancel · **`Shift+Enter`** new line in text

What you see is what you save: Pillow renders each mark into the exact pixels that get saved and copied. While typing, `Enter` or `Esc` only ends the text (press `Enter` again to finish) and `Ctrl+Z` discards it. Right-click does nothing while you draw, so a stray right-click can't lose your work. Skip `T` and nothing changes.

## Shortcuts

| Key | Where | Action |
|---|---|---|
| `Ctrl+Shift+H` | anywhere | Freeze the screen and start a capture |
| `Ctrl+Shift+Q` | anywhere | Quit PyShot |
| Drag / click | overlay | Capture a region / the window under the cursor |
| `C` | overlay | Copy the pixel colour under the cursor instead of capturing (needs the loupe) |
| `T` | overlay | Toggle annotation mode |
| `Esc` / right-click | overlay (selecting) | Cancel. While drawing, `Esc` cancels and right-click does nothing |

## Configure

Edit the constants at the top of `pyshot.py`, then quit with `Ctrl+Shift+Q` and start it again:

| Constant | Default | Options |
|---|---|---|
| `HOTKEY_CAPTURE` | `"ctrl+shift+h"` | `ctrl`/`shift`/`alt`/`win` + a letter, digit, `f1`–`f24` or named key, e.g. `"printscreen"`, `"ctrl+alt+f9"` |
| `HOTKEY_QUIT` | `"ctrl+shift+q"` | Same syntax |
| `CAPTURE_TARGET` | `"cursor"` | `"cursor"` (monitor under the mouse), `"primary"`, `"all"` |
| `CAPTURE_MODE` | `"both"` | `"both"`, `"clipboard"` (no file), `"file"` (no clipboard) |
| `WINDOW_SNAP`, `LOUPE`, `ANNOTATE` | `True` | `False` turns off window snap, the loupe (and `C`), or `T` |

A hotkey that can't be parsed falls back to the default, and the reason goes to `%TEMP%\pyshot.log`.

## Start with Windows

```powershell
python pyshot.py --startup
```

A dialog adds PyShot to startup. If it is already there, it offers **Remove completely**, which also closes the running copy. `--help` prints usage.

<details>
<summary><b>Build a standalone .exe</b></summary>

```powershell
pip install pyinstaller
pyinstaller pyshot.spec
```

This builds `dist\PyShot.exe`, which runs with no console window. `PyShot.exe --startup` and `PyShot.exe --help` work like the script. Settings are baked in at build time, so rebuild after editing them. There is no prebuilt download; build it yourself.

</details>

<details>
<summary><b>Troubleshooting and limitations</b></summary>

- **Launched, but no window?** That's by design: PyShot waits in the background for `Ctrl+Shift+H`. Launch it again and a message box confirms it is already running.
- **Hotkey does nothing:** another app owns that combo (PyShot says so in a toast at startup), or the key name wasn't recognised and PyShot fell back to `Ctrl+Shift+H`. Check `%TEMP%\pyshot.log`.
- **"clipboard copy failed!":** another app was holding the clipboard; with the default `CAPTURE_MODE` the PNG is still saved.
- **Anything else:** a missing package gets a message box; other errors land in `%TEMP%\pyshot.log`.
- **Limits:** Windows only. A selection can't be resized after release, and marks can't be moved, only undone. Both hotkeys are reserved system-wide while PyShot runs (VS Code's `Ctrl+Shift+H`, for example), so change `HOTKEY_CAPTURE` if that clashes.

</details>

<details>
<summary><b>Development</b></summary>

```powershell
pip install -r requirements.txt pytest ruff==0.15.5
pytest -q
ruff check .
```

CI runs lint and tests on `windows-latest`.

</details>

---

<p align="center"><a href="LICENSE">MIT License</a> · Made by <a href="https://github.com/U-C4N">U-C4N</a> · Not affiliated with Lightshot</p>
