"""Protocol tests exercise the official SDK against a harmless fake broker."""
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from PIL import Image

from pyshot_core.errors import PyShotError
from pyshot_core.mcp_server import BrokerBridge, TOOL_SPECS, create_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeBroker:
    def __init__(self, image=None):
        self.image = image
        self.calls = []
        self.closed = False
        self.error = None

    def call(self, method, params=None):
        self.calls.append((method, params))
        if self.error:
            raise self.error
        if method == "capture_screenshot":
            return {"path": str(self.image), "preview_path": str(self.image),
                    "width": 3200, "height": 1800}
        if method == "recording_frames":
            return {"recording_id": params["recording_id"],
                    "frames": [{"path": str(self.image), "timestamp_seconds": t}
                               for t in params["timestamps_seconds"]]}
        if method == "recording_start":
            return {"recording_id": "recording-1", "state": "recording"}
        if method == "recording_stop":
            return {"recording_id": params["recording_id"], "state": "completed"}
        return {"available": True, "method": method}

    def close(self):
        self.closed = True


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["modern", "legacy"])
async def test_real_sdk_lists_and_calls_tools_in_both_protocol_eras(mode):
    broker = FakeBroker()
    server = create_server(lambda: broker)
    # In-process modern mode is called "auto": it discovers the current era.
    options = {} if mode == "modern" else {"mode": "legacy"}
    async with Client(server, **options) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == set(TOOL_SPECS)
        result = await client.call_tool("get_capabilities", {})
        assert not result.is_error
        assert result.structured_content["available"] is True
        assert json.loads(result.content[0].text)["method"] == "get_capabilities"
        assert not broker.closed
    assert broker.closed


@pytest.mark.anyio
async def test_screenshot_returns_image_and_full_resolution_metadata(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (3200, 1800), "red").save(path)
    broker = FakeBroker(path)
    async with Client(create_server(lambda: broker)) as client:
        result = await client.call_tool("capture_screenshot", {"target": {"kind": "monitor"}})
        assert not result.is_error
        assert result.structured_content["width"] == 3200
        assert result.structured_content["height"] == 1800
        image = next(block for block in result.content if block.type == "image")
        with Image.open(io.BytesIO(base64.b64decode(image.data))) as preview:
            assert preview.size == (1600, 900)
        assert broker.calls[-1][1] == {"target": {"kind": "monitor", "monitor_id": "primary"}}
    with Image.open(path) as original:
        assert original.size == (3200, 1800)


@pytest.mark.anyio
async def test_broker_errors_are_tool_results_not_protocol_failures():
    broker = FakeBroker()
    broker.error = PyShotError("ALREADY_RECORDING", "A recording is running", {"recording_id": "other"})
    async with Client(create_server(lambda: broker)) as client:
        result = await client.call_tool("recording_start", {})
        assert result.is_error
        assert result.structured_content["error"] == {
            "code": "ALREADY_RECORDING", "message": "A recording is running",
            "details": {"recording_id": "other"},
        }


@pytest.mark.parametrize("method,args", [
    ("capture_screenshot", {"target": {"kind": "region", "region": {"x": 0, "y": 0, "width": 0, "height": 1}}}),
    ("capture_screenshot", {"target": {"kind": "window"}}),
    ("recording_start", {"audio": "speaker-and-mic"}),
    ("recording_start", {"duration_seconds": float("nan")}),
    ("recording_start", {"duration_seconds": 86401}),
    ("recording_stop", {"recording_id": ""}),
    ("recording_frames", {"recording_id": "r", "timestamps_seconds": list(range(9))}),
    ("recording_frames", {"recording_id": "r", "timestamps_seconds": [-1]}),
    ("get_capabilities", {"ignored": True}),
])
def test_invalid_arguments_do_not_reach_broker(method, args):
    broker = FakeBroker()
    result = BrokerBridge(broker).call(method, args)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"
    assert broker.calls == []


def test_physical_negative_region_and_audio_ids_forward_unchanged():
    broker = FakeBroker()
    target = {"kind": "region", "region": {"x": -1920, "y": -100, "width": 400, "height": 300}}
    result = BrokerBridge(broker).call("recording_start", {
        "target": target, "audio": "both", "microphone_id": "mic-1", "system_audio_id": "speaker-2",
        "duration_seconds": 5,
    })
    assert not result.is_error
    assert broker.calls[0][1] == {"target": target, "audio": "both", "microphone_id": "mic-1",
                                  "system_audio_id": "speaker-2", "duration_seconds": 5.0}


@pytest.mark.anyio
async def test_frames_return_timestamped_image_blocks(tmp_path):
    path = tmp_path / "frame.png"
    Image.new("RGB", (32, 32), "blue").save(path)
    broker = FakeBroker(path)
    async with Client(create_server(lambda: broker)) as client:
        result = await client.call_tool("recording_frames", {
            "recording_id": "recording-1", "timestamps_seconds": [0, 1.25],
        })
        assert not result.is_error
        assert sum(block.type == "image" for block in result.content) == 2
        assert result.content[1].text == "Frame at 0.0 seconds"
        assert result.content[3].text == "Frame at 1.25 seconds"


@pytest.mark.anyio
async def test_real_stdio_entry_lists_tools_without_opening_broker():
    entry = Path(__file__).resolve().parents[1] / "pyshot_mcp.py"
    async with Client(StdioServerParameters(command=sys.executable, args=[str(entry), "serve"])) as client:
        result = await client.list_tools()
        assert {tool.name for tool in result.tools} == set(TOOL_SPECS)


def test_mcp_import_does_not_import_gui_or_tk():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c",
                             "import pyshot_core.mcp_server, sys; "
                             "assert 'pyshot' not in sys.modules; assert 'tkinter' not in sys.modules"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_pipe_clients_have_distinct_owners_and_disconnect_independently(monkeypatch):
    from pyshot_core import rpc

    # Exercise real Windows IPC on a unique test pipe, without launching PyShot
    # or accessing the desktop. The service only observes connection ownership.
    test_pipe = rf"\\.\pipe\PyShot.test.{uuid.uuid4().hex}"
    monkeypatch.setattr(rpc, "pipe_name", lambda: test_pipe)

    class OwnershipService:
        def __init__(self):
            self.disconnected = set()
            self.events = {}

        def call(self, owner, method, params):
            self.events[owner] = threading.Event()
            return {"owner": owner}

        def disconnect(self, owner):
            self.disconnected.add(owner)
            if owner in self.events:
                self.events[owner].set()

        def close(self):
            pass

    service = OwnershipService()
    server = rpc.BrokerServer(service)
    server.start()
    first, second = rpc.BrokerClient(autostart=False), rpc.BrokerClient(autostart=False)
    try:
        first_owner = first.call("whoami")["owner"]
        second_owner = second.call("whoami")["owner"]
        assert first_owner != second_owner
        first.close()
        assert service.events[first_owner].wait(3)
        assert second_owner not in service.disconnected
        assert second.call("whoami")["owner"] == second_owner
        second.close()
        assert service.events[second_owner].wait(3)
    finally:
        first.close()
        second.close()
        server.close()
