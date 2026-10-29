# PyShot 3.0 release validation

Automated unit/protocol checks are distinct from hardware acceptance. Run the commands from the repository root on Windows x64.

```powershell
python -m pytest -q
python -m ruff check .
cargo test --locked --manifest-path native/pyshot-recorder/Cargo.toml
.\scripts\build.ps1
python scripts/smoke_mcp.py dist/PyShot-3.0.1-win-x64/pyshot-mcp.exe
```

Python regressions cover annotation/editing behavior, failed DPI HRESULT/BOOL calls, access-denied mutex detection, failed SetEvent, negative physical coordinates, real named-pipe ownership, single-recording conflicts, old-ID protection, repeated/concurrent stop, 20 controller cycles, MCP old/new protocol, image results and isolated idempotent CLI setup. The controller tests use a subprocess protocol fixture; they do not prove native media quality.

Native tests cover sizing, timestamp arithmetic and synthetic audio alignment. The native `self-test --output <absolute MP4 path>` writes synthetic video and audio, without recording a device or the desktop.

## Hardware acceptance checklist

- Region, monitor and HWND recording; negative monitor coordinates and mixed DPI.
- Move and resize the target window; inspect aspect ratio and fixed output dimensions.
- Static 60-second recording: inspect duration and stop latency.
- Silent, microphone, system and mixed audio; measure A/V offset below 100 ms over 30 minutes with a repeatable audiovisual stimulus.
- Target closure, desktop lock, endpoint disconnect, invalid/disk-full output and client EOF.
- Twenty native start/stop cycles, with readable MP4 output each time.
- Two real CLI clients share the same active ID; stopping an old ID leaves a new recording running.
- Extract exactly the shipped ZIP on a clean Windows 10/11 machine without Python, Rust or FFmpeg, and repeat screenshot/video/MCP smoke tests.

Hardware-specific results and any remaining release gates are recorded below as they are measured. A successful unit test is not a substitute for these checks.

## Measured on 2026-10-12

Host: Windows 11 x64, Python 3.13.15, one 2560×1600 display. Tests use owned synthetic windows except the explicit monitor capture.

| Check | Result |
|---|---|
| Python suite | 173 passed; Ruff passed |
| Rust suite | 13 passed |
| Native synthetic H.264/AAC + frame decoder | Passed; upright colors and exact display aperture |
| Real monitor capture | 2560×1600, 1.000 s, finalized MP4 |
| Odd region | 127×75 physical pixels padded to 128×76; color round-trip passed |
| HWND capture | Covered window screenshot returned the target; moving/resizing video retained the initial canvas |
| Static recording | 60.000 s, 1,799 encoded samples (overdue slots drop while the timeline keeps real time) |
| Stop latency | 36–40 ms immediate stop; worst of 20 consecutive restart cycles 151 ms |
| Target closure | Finalized with `target_closed` interruption |
| Microphone/system/both | Each real three-second capture finalized and decoded; audio/video **track duration** difference 7.98 ms |
| Packaged stdio | Initialize + all nine tools passed |
| Packaged GUI + two MCP processes | Screenshot, video, frame previews, shared ID/conflict, repeated stop, old-ID protection and owner EOF finalization passed |
| Runtime dependencies | Packaged processes passed with system-only PATH, without resolving Python/Rust/FFmpeg from PATH; native MSVC runtime DLLs shipped beside the helper |
| Real CLI setup | Isolated setup and actual installed registration passed for Codex and Claude; the second installed setup reported both unchanged and left the parsed configurations identical |
| Real Codex tool calls | Installed EXE from the saved client configuration: `get_capabilities`, `list_displays`, `recording_status` all succeeded |
| Real Claude tool calls | Installed EXE from the saved client configuration: `get_capabilities`, `list_displays`, `recording_status` all succeeded |

The tested installation is `%LOCALAPPDATA%\Programs\PyShot`. Both CLI clients used their saved PyShot registration. Claude's `-p` run explicitly supplied the existing `%USERPROFILE%\.claude.json` through `--mcp-config` so startup waited for the MCP connection before the first model turn. These smoke tests made no persistent test configuration overrides.

The native static-window test discovered that DWM can retire an idle backing surface. Starting HWND capture now queues an asynchronous normal repaint and has a bounded first-frame retry. This does not inject input or alter window contents. The decoder tests also caught H.264 allocation padding; extracted images now use the display aperture.

Reproducible opt-in hardware scripts:

```powershell
python scripts/test_native_video.py --stress
python scripts/test_native_audio.py
python scripts/test_packaged.py
python scripts/test_cli_clients.py --server dist/PyShot-3.0.1-win-x64/pyshot-mcp.exe --tool-calls --clients codex,claude
```

Close an older PyShot version before the packaged test so the test can start the packaged broker. Native scripts retain evidence under `%TEMP%`; package/CLI scripts write reports under ignored `build/`. Native audio tests open real default devices; packaged tests capture their own synthetic window. The package test intentionally leaves the broker running.

## Remaining release gates

- A 30-minute real audiovisual stimulus test demonstrating content synchronization below 100 ms. The short track-duration result and synthetic QPC tests do **not** establish content latency.
- Clean Windows 10 1903+ and Windows 11 VM runs without preinstalled runtimes. A restricted PATH is useful evidence, not a clean-machine test.
- Physical mixed-DPI/multiple-monitor and negative-origin hardware checks (coordinate unit regressions passed).
- Physical audio unplug, desktop lock and real disk-full interruption checks. Endpoint-error flushing has unit coverage; target closure and client EOF were tested live.
