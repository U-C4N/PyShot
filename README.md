<h1 align="center">PyShot</h1>

<p align="center">
  <b>Press a key. Drag a box. Paste it anywhere.</b><br>
  Screenshots, native MP4 recording and local MCP tools for Windows. Runs in the background, never uploads captures.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-3.0.0-blue.svg" alt="Version 3.0.0">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6.svg" alt="Windows">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT license">
</p>

## Quick start

Extract the complete `PyShot-3.0.0-win-x64.zip` bundle into a permanent folder and run `PyShot.exe`. Keep all files and the `_internal` folder together. The bundle includes Python and the native recorder; users do not install Python, Rust or FFmpeg. When upgrading from 2.x, close the old process with `Ctrl+Shift+Q` before launching 3.0.

From source (screenshot support; build the native helper below for video):

```powershell
git clone https://github.com/U-C4N/PyShot.git
cd PyShot
pip install -r requirements.txt
pythonw pyshot.py
```

This installs the locked dependencies and starts PyShot silently in the background (the **w** in `pythonw` means no console window). Press **`Ctrl+Shift+H`**, drag a box, then **`Ctrl+V`** it anywhere. Every shot is also saved as a lossless PNG in `Desktop\Screenshots\`. **`Ctrl+Shift+R`** selects a video target or stops the current recording. **`Ctrl+Shift+Q`** finalizes any recording and quits.

> Windows 10 1903+ or Windows 11, x64. Source usage needs Python 3.11+. Packaging uses Python 3.13. Windows N editions need Microsoft's Media Feature Pack.

## What's new in 3.0.0

- **Native video:** Windows Graphics Capture and Media Foundation write H.264/AAC MP4 at 30 FPS. Select a region, monitor or window; audio defaults to off.
- **Local MCP:** nine tools for Codex CLI and Claude Code, including screenshots, video status and extracted video frames. Clients share the same PyShot process and recording.
- **Windows fixes:** access-denied mutexes count as running, DPI API failures no longer claim success, and failed quit signals are reported correctly.
- **Existing annotation tools remain:** pen, marker, arrow, box, text, pixelate, undo and clipboard delivery.

## Record video

Press `Ctrl+Shift+R`, select **region**, **monitor** or **window**, then choose **none**, **microphone**, **system** or **both** for audio. GUI recording uses Windows' default audio endpoints. Region selection stays within the selected monitor and uses physical pixels. Window recordings follow the selected HWND as it moves; resizing fits the content into the initial canvas without stretching. Odd pixel dimensions are padded to even dimensions.

Press `Ctrl+Shift+R` again to stop. Videos are saved under `Desktop\Screenshots\Videos` (including redirected/OneDrive desktops). The active file ends in `.partial.mp4`; successful finalization renames it to `.mp4`. A closed target, locked desktop or lost audio device interrupts recording, with a reason returned alongside any finalized output. A failed finalization retains the partial file and reports an error; it is not guaranteed playable after a crash or power loss.

Only one recording runs per Windows user/session. Both GUI hotkeys and MCP clients see and can stop it. The video timeline repeats the latest image when the screen is static. No pause/resume, cloud connection, or mouse/keyboard automation is provided.

## Connect Codex CLI and Claude Code

Run once from the extracted bundle folder:

```powershell
.\pyshot-mcp.exe setup --clients codex,claude
```

From source:

```powershell
python pyshot_mcp.py setup --clients codex,claude
```

Setup calls the installed clients' official `mcp add` commands, reuses an identical registration, and preserves their permission settings. Restart the client after adding PyShot. A customized or unrelated registration named `pyshot` is reported instead of overwritten. Keep the bundle at its registered path, or run setup again after moving it.

Example request: **“Use PyShot to record my primary monitor without audio, stop after 10 seconds, then show frames at 0, 4 and 8 seconds.”** The agent's permission policy still governs tool use. This stdio server is for local MCP clients; browser ChatGPT connections are outside this release's scope.

| Tool | Result |
|---|---|
| `get_capabilities` | Version and available capture/recording features |
| `list_displays` | Monitor IDs and physical coordinates |
| `list_windows` | Capturable HWND IDs and titles |
| `list_audio_devices` | Available microphone and system endpoints |
| `capture_screenshot` | Inline preview plus original PNG path and dimensions |
| `recording_start` | Shared recording ID; optional audio and duration limit |
| `recording_status` | State, duration and interruption/error details |
| `recording_stop` | Finalized MP4 path and metadata; safe to repeat for the same ID |
| `recording_frames` | Up to eight images at specified times from a completed video |

MCP captures do not open selection dialogs. The broker starts automatically on the first tool call. Its named pipe accepts only the current Windows user, rejects remote clients, and is isolated by session. Closing the originating MCP process finalizes its recording. MP4 is returned as a path and metadata; use `recording_frames` for visual inspection. [API examples and architecture](docs/architecture.md).

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
| `Ctrl+Shift+R` | anywhere | Select a video target / stop the active recording |
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
| `HOTKEY_RECORD` | `"ctrl+shift+r"` | Same syntax |
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
<summary><b>Build the standalone Windows bundle</b></summary>

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
.\scripts\build.ps1 -Python .\.venv\Scripts\python.exe
```

The build machine needs Rust with the MSVC toolchain and Visual Studio C++ Build Tools/Windows SDK. This builds `dist\PyShot-3.0.0-win-x64.zip` with a windowless GUI, a console MCP adapter and a native recorder. `PyShot.exe --startup` and `PyShot.exe --help` work like the script. Settings are baked in at build time. `Cargo.lock`, `uv.lock` and hash-locked Python requirements make dependencies reproducible.

To build only the recorder for source usage:

```powershell
cargo build --release --locked --manifest-path native/pyshot-recorder/Cargo.toml
```

</details>

<details>
<summary><b>Troubleshooting and limitations</b></summary>

- **Launched, but no window?** That's by design: PyShot waits in the background for `Ctrl+Shift+H`. Launch it again and a message box confirms it is already running.
- **Hotkey does nothing:** another app owns that combo (PyShot says so in a toast at startup), or the key name wasn't recognised and PyShot fell back to `Ctrl+Shift+H`. Check `%TEMP%\pyshot.log`.
- **"clipboard copy failed!":** another app was holding the clipboard; with the default `CAPTURE_MODE` the PNG is still saved.
- **Anything else:** a missing package gets a message box; other errors land in `%TEMP%\pyshot.log`.
- **Limits:** Windows only. Protected/DRM content may be black. Region video stays inside one monitor. A screenshot selection can't be resized after release, and marks can't be moved, only undone. All three hotkeys are reserved system-wide while PyShot runs; change the constants if they clash.

</details>

<details>
<summary><b>Development</b></summary>

```powershell
pip install --require-hashes -r requirements-dev.lock
pytest -q
ruff check .
cargo test --locked --manifest-path native/pyshot-recorder/Cargo.toml
```

CI runs Python/native tests, builds the bundle and validates its stdio protocol on `windows-latest`. Hardware acceptance checks and reproducible commands are tracked in [release validation](docs/validation.md).

</details>

---

<p align="center"><a href="LICENSE">MIT License</a> · Made by <a href="https://github.com/U-C4N">U-C4N</a> · Not affiliated with Lightshot</p>
