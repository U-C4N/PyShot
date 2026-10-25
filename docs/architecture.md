# PyShot 3.0 local architecture

`pyshot.py` remains the GUI entry point. `pyshot_core` shares physical-pixel capture, output paths, platform helpers, recording ownership and IPC. Tk objects are touched only on the main thread; recorder startup/finalization and pipe requests run on worker threads. Global hotkeys use RegisterHotKey, without input hooks.

Each Windows user/session owns one GUI broker. The separate console `pyshot-mcp.exe` serves MCP on stdio through official Python SDK 2.3.0 and maintains a persistent broker connection. stdout contains protocol messages; logs use stderr. The broker uses a protected current-user SID DACL and `PIPE_REJECT_REMOTE_CLIENTS`. Messages have a 1 MiB limit and explicit request IDs. The pipe is not a network service.

The broker owns one recording process. It allocates a unique immutable recording ID and `.partial.mp4` path before starting `pyshot-recorder.exe`. EOF from the owning MCP connection stops that recording; another client's connection closing cannot stop it. Repeated stop returns the stored result, and an old ID cannot stop a newer recording. Completed IDs remain available for the lifetime of the broker.

## Native media pipeline

The Rust helper uses `windows-capture` only for Windows Graphics Capture. It copies BGRA frames into a bounded latest-frame slot. A separate 30 FPS scheduler repeats the last image on a static desktop and checks stop requests independently of frame callbacks. A custom Media Foundation Sink Writer handles H.264 video and optional AAC audio; write backpressure bounds memory. The initial even canvas stays fixed and later window sizes are letterboxed.

WASAPI workers capture microphone and/or loopback packets with QPC timestamps. PCM is resampled to 48 kHz stereo and placed on a timeline relative to the first video's QPC timestamp. Missing ranges become silence; late samples are discarded and each packet's placement follows QPC to avoid accumulating independent device-clock drift. Combined sources are mixed with clipping protection. Actual long-duration A/V accuracy still requires hardware validation, described in the release checklist.

On target loss or device failure the helper attempts finalization and reports an interruption. If finalization succeeds the broker atomically renames the partial file. Disk or process failure can leave a partial file. Frames are decoded by a native Media Foundation Source Reader, not an external codec executable.

## MCP argument examples

Use a returned monitor or window ID. Coordinates are physical desktop pixels, including negative positions on monitors to the left or above the primary display.

```json
{"target":{"kind":"monitor","monitor_id":"primary"}}
```

Pass that object to `capture_screenshot`. For `recording_start`, optional audio defaults to `none`:

```json
{
  "target":{"kind":"region","region":{"x":100,"y":100,"width":801,"height":601}},
  "audio":"none",
  "duration_seconds":10
}
```

Audio modes are `none`, `microphone`, `system` and `both`; `microphone_id` and `system_audio_id` can select IDs returned by `list_audio_devices`. Omit them to use Windows defaults. To capture a window use `{"kind":"window","window_id":"<returned ID>"}` as the target.

Pass the returned `recording_id` to `recording_status` or `recording_stop`. After completion, pass it with `timestamps_seconds`, containing one to eight finite times greater than or equal to zero and less than the completed duration, to `recording_frames`. Screenshot previews are bounded at 1600×1200; original files retain native dimensions. The MCP result contains both images and structured metadata.

Common errors include `recording_active` (with the current ID), `unknown_recording`, `target_lost`, `invalid_target`, `not_completed`, `invalid_timestamps`, `recorder_unavailable` and `finalizing`. After `finalizing`, poll the same ID instead of creating a new recording. After broker restart, old IDs are unknown; saved files remain on disk.

## Sources

- [Microsoft Sink Writer](https://learn.microsoft.com/en-us/windows/win32/medfound/tutorial--using-the-sink-writer-to-encode-video)
- [WGC frame QPC time](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframe.systemrelativetime)
- [WASAPI packet QPC time](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudiocaptureclient-getbuffer)
- [Official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/whats-new/)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
