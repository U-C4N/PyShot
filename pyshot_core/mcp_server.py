"""Official MCP SDK stdio adapter; the broker owns all capture and GUI state."""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import io
import json
import logging
from pathlib import Path
import threading
from typing import Annotated, Literal

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool, ToolAnnotations
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .errors import PyShotError


LOG = logging.getLogger(__name__)
INSTRUCTIONS = (
    "PyShot captures this Windows desktop and records local MP4 video. Use list_displays "
    "or list_windows to choose a target; regions use physical desktop pixels within one display. "
    "capture_screenshot returns a visible image plus the full-resolution file metadata. "
    "recording_start begins one recording shared with PyShot's hotkeys; recording_stop "
    "finishes it. Use recording_frames to inspect up to eight moments of a completed video. "
    "Audio is off unless requested. The recording stops if its originating CLI disconnects."
)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Rectangle(InputModel):
    x: int = Field(description="Physical desktop X coordinate; can be negative.")
    y: int = Field(description="Physical desktop Y coordinate; can be negative.")
    width: int = Field(ge=2)
    height: int = Field(ge=2)


class MonitorTarget(InputModel):
    kind: Literal["monitor"] = "monitor"
    monitor_id: str = Field(default="primary", min_length=1)


class RegionTarget(InputModel):
    kind: Literal["region"]
    region: Rectangle


class WindowTarget(InputModel):
    kind: Literal["window"]
    window_id: str = Field(min_length=1, description="Window ID returned by list_windows.")


Target = Annotated[MonitorTarget | RegionTarget | WindowTarget, Field(discriminator="kind")]


class CaptureInput(InputModel):
    target: Target = Field(default_factory=MonitorTarget)


class StartInput(CaptureInput):
    audio: Literal["none", "microphone", "system", "both"] = "none"
    microphone_id: str | None = Field(default=None, min_length=1)
    system_audio_id: str | None = Field(default=None, min_length=1)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400,
                                         description="Optional maximum recording duration in seconds.")


class StatusInput(InputModel):
    recording_id: str | None = Field(default=None, min_length=1)


class StopInput(InputModel):
    recording_id: str = Field(min_length=1)


class FramesInput(StopInput):
    timestamps_seconds: list[Annotated[float, Field(ge=0)]] = Field(min_length=1, max_length=8)


TOOL_SPECS = {
    "get_capabilities": (InputModel, "Report capture, video, audio, and platform capabilities.", True),
    "list_displays": (InputModel, "List monitor IDs, primary display, bounds, and pixel dimensions.", True),
    "list_windows": (InputModel, "List visible capturable window IDs, titles, and physical bounds.", True),
    "list_audio_devices": (InputModel, "List microphone and system-audio endpoints for optional recording audio.", True),
    "capture_screenshot": (CaptureInput, "Capture a monitor, physical desktop region, or window. Returns an inline image and original file metadata.", False),
    "recording_start": (StartInput, "Start an MP4 recording of a monitor, region, or window. Audio defaults to none. Returns a recording_id; only one recording may run.", False),
    "recording_status": (StatusInput, "Read the state and metadata for a recording; omit the ID for the current recording.", True),
    "recording_stop": (StopInput, "Stop and finalize the specified recording. Repeating stop for the same completed ID is safe.", False),
    "recording_frames": (FramesInput, "Extract one to eight timestamped images from a completed recording for visual inspection.", False),
}


def _tools() -> list[Tool]:
    return [Tool(
        name=name,
        description=description,
        input_schema=model.model_json_schema(),
        output_schema={"type": "object"},
        annotations=ToolAnnotations(read_only_hint=read_only, destructive_hint=False,
                                    idempotent_hint=read_only or name == "recording_stop",
                                    open_world_hint=False),
    ) for name, (model, description, read_only) in TOOL_SPECS.items()]


def _error_result(code: str, message: str, details=None) -> CallToolResult:
    data = {"error": {"code": code, "message": message, "details": details or {}}}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                          structured_content=data, is_error=True)


def _preview(path: str) -> ImageContent:
    """Bound inline images independently of the full-resolution saved artifact."""
    image_path = Path(path)
    with Image.open(image_path) as original:
        image = original.convert("RGB")
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data = buffer.getvalue()
    mime_type = "image/png"
    if len(data) > 2 * 1024 * 1024:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
        data, mime_type = buffer.getvalue(), "image/jpeg"
    return ImageContent(type="image", mime_type=mime_type,
                        data=base64.b64encode(data).decode("ascii"))


def _success_result(method: str, data: dict) -> CallToolResult:
    content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]
    if method == "capture_screenshot":
        path = data.get("preview_path") or data.get("path")
        if not path:
            raise PyShotError("INVALID_BROKER_RESULT", "Screenshot response has no image path")
        content.append(_preview(path))
    elif method == "recording_frames":
        for frame in data.get("frames", []):
            content.append(TextContent(type="text", text=f"Frame at {frame['timestamp_seconds']} seconds"))
            content.append(_preview(frame["path"]))
    return CallToolResult(content=content, structured_content=data, is_error=False)


class BrokerBridge:
    """One persistent broker client and serialized calls per MCP process."""
    def __init__(self, client):
        self.client = client
        self.lock = threading.RLock()

    def call(self, name: str, arguments: dict) -> CallToolResult:
        if name not in TOOL_SPECS:
            return _error_result("UNKNOWN_TOOL", f"Unknown PyShot tool: {name}")
        try:
            model = TOOL_SPECS[name][0].model_validate(arguments)
        except ValidationError as exc:
            # Avoid serializing Pydantic's raw input fields or exception objects.
            errors = [{"field": ".".join(str(part) for part in error["loc"]),
                       "message": error["msg"]} for error in exc.errors()]
            return _error_result("INVALID_ARGUMENT", "Invalid tool arguments", {"errors": errors})
        try:
            with self.lock:
                result = self.client.call(name, model.model_dump(exclude_none=True))
                if not isinstance(result, dict):
                    raise PyShotError("INVALID_BROKER_RESULT", "The broker returned invalid metadata")
                return _success_result(name, result)
        except PyShotError as exc:
            return _error_result(exc.code, exc.message, exc.details)
        except (OSError, ValueError, KeyError) as exc:
            LOG.warning("Could not read PyShot tool result: %s", exc)
            return _error_result("ARTIFACT_ERROR", "Could not read the generated image or recording metadata")
        except Exception:
            LOG.exception("Unexpected PyShot broker error")
            return _error_result("INTERNAL_ERROR", "PyShot could not complete this operation")

    def close(self):
        with self.lock:
            self.client.close()


def create_server(client_factory=None) -> Server:
    """Create a real SDK server. Tests supply an in-memory broker, never the desktop."""
    if client_factory is None:
        from .rpc import BrokerClient
        client_factory = BrokerClient

    @asynccontextmanager
    async def lifespan(_server):
        bridge = BrokerBridge(client_factory())
        try:
            yield bridge
        finally:
            # EOF/cancellation must close the pipe, which finalizes this client's
            # active recording on the broker. Do not let cancellation skip close.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(bridge.close)

    async def list_tools(_context, _params):
        return ListToolsResult(tools=_tools())

    async def call_tool(context, params):
        return await anyio.to_thread.run_sync(context.lifespan_context.call,
                                             params.name, params.arguments or {})

    return Server("pyshot", version=__version__, instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool, lifespan=lifespan)


def serve():
    async def run():
        server = create_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    anyio.run(run)
