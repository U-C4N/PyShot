"""Length-prefixed JSON over a local, current-user-only Windows named pipe."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import uuid

import pywintypes
import win32api
import win32con
import win32file
import win32pipe
import win32security

from .errors import PyShotError

MAX_MESSAGE = 1_048_576


def identity():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        session = win32security.GetTokenInformation(token, win32security.TokenSessionId)
        return win32security.ConvertSidToStringSid(sid), session
    finally:
        token.Close()


def pipe_name():
    sid, session = identity()
    return rf"\\.\pipe\PyShot.v3.{sid}.{session}"


def security_attributes():
    sid, _ = identity()
    # Protected DACL grants only the current user; reject network clients too.
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;GA;;;{sid})", win32security.SDDL_REVISION_1)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _read_exact(handle, size):
    chunks = []
    while size:
        _, data = win32file.ReadFile(handle, size)
        if not data:
            raise EOFError("Pipe closed")
        chunks.append(data)
        size -= len(data)
    return b"".join(chunks)


def _receive(handle):
    size, = struct.unpack("<I", _read_exact(handle, 4))
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("Invalid IPC message size")
    message = json.loads(_read_exact(handle, size))
    if not isinstance(message, dict):
        raise ValueError("IPC message must be an object")
    return message


def _send(handle, message):
    data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ValueError("IPC message too large")
    win32file.WriteFile(handle, struct.pack("<I", len(data)) + data)


class BrokerServer:
    def __init__(self, service):
        self.service = service
        self._stopped = threading.Event()
        self._thread = None
        self._handles = set()
        self._lock = threading.Lock()
        self._listener = self._new_pipe(first=True)

    def _new_pipe(self, first=False):
        return win32pipe.CreateNamedPipe(
            pipe_name(), win32pipe.PIPE_ACCESS_DUPLEX | (0x00080000 if first else 0),
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | 0x8,
            32, 65536, 65536, 5000, security_attributes())

    def start(self):
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def _listen(self):
        handle = self._listener
        while not self._stopped.is_set():
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # Client connected before ConnectNamedPipe.
                        raise
                if self._stopped.is_set():
                    handle.Close()
                    break
                with self._lock:
                    self._handles.add(handle)
                threading.Thread(target=self._serve, args=(handle,), daemon=True).start()
                while not self._stopped.is_set():
                    try:
                        handle = self._new_pipe()
                        break
                    except pywintypes.error as exc:
                        if exc.winerror != 231:
                            raise
                        # Existing clients remain usable when all instances are
                        # occupied. Accept again once a client releases a slot.
                        self._stopped.wait(0.1)
                if self._stopped.is_set():
                    return
                self._listener = handle
            except pywintypes.error:
                if not self._stopped.is_set():
                    self._stopped.set()
                break

    def _serve(self, handle):
        owner = uuid.uuid4().hex
        try:
            while not self._stopped.is_set():
                request = _receive(handle)
                try:
                    params = request.get("params") or {}
                    if request.get("version") != 1 or not isinstance(params, dict):
                        raise PyShotError("protocol_error", "Unsupported IPC request")
                    result = self.service.call(owner, request.get("method"), params)
                    response = {"id": request.get("id"), "result": result}
                except PyShotError as exc:
                    response = {"id": request.get("id"), "error": exc.as_dict()}
                except (TypeError, ValueError) as exc:
                    response = {"id": request.get("id"), "error": PyShotError("invalid_params", str(exc)).as_dict()}
                except Exception as exc:
                    response = {"id": request.get("id"), "error": PyShotError("operation_failed", str(exc)).as_dict()}
                _send(handle, response)
        except (pywintypes.error, EOFError, ValueError):
            pass
        finally:
            try:
                self.service.disconnect(owner)
            except Exception:
                pass
            with self._lock:
                self._handles.discard(handle)
            handle.Close()

    def close(self):
        self._stopped.set()
        # Wake synchronous accept without blocking the Tk thread during shutdown.
        try:
            handle = win32file.CreateFile(pipe_name(), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                          0, None, win32con.OPEN_EXISTING, 0, None)
            handle.Close()
        except pywintypes.error:
            pass
        try:
            self.service.close()
        finally:
            with self._lock:
                handles = list(self._handles)
            for handle in handles:
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except pywintypes.error:
                    pass
            if self._thread:
                self._thread.join(2)


def _launch():
    if getattr(sys, "frozen", False):
        command = [str(Path(sys.executable).with_name("PyShot.exe")), "--background"]
    else:
        executable = Path(sys.executable).with_name("pythonw.exe")
        command = [str(executable), str(Path(__file__).resolve().parents[1] / "pyshot.py"), "--background"]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                     close_fds=True)


class BrokerClient:
    def __init__(self, autostart=True):
        self._handle = None
        self._lock = threading.Lock()
        self.autostart = autostart

    def _connect(self):
        deadline = time.monotonic() + 20
        launched = False
        while True:
            try:
                self._handle = win32file.CreateFile(pipe_name(), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                                    0, None, win32con.OPEN_EXISTING, 0, None)
                return
            except pywintypes.error as exc:
                if exc.winerror == 5:
                    raise PyShotError("access_denied", "Cannot connect to this user's PyShot broker") from exc
                if exc.winerror not in (2, 231) or time.monotonic() >= deadline:
                    raise PyShotError("broker_unavailable", "PyShot broker did not respond. Close any older PyShot copy and retry.") from exc
                if not launched and self.autostart and exc.winerror == 2:
                    _launch()
                    launched = True
                elif not self.autostart and exc.winerror == 2:
                    raise PyShotError("broker_unavailable", "PyShot is not running") from exc
                time.sleep(0.1)

    def call(self, method, params=None):
        with self._lock:
            if self._handle is None:
                self._connect()
            ident = uuid.uuid4().hex
            try:
                _send(self._handle, {"version": 1, "id": ident, "method": method, "params": params or {}})
                response = _receive(self._handle)
            except (pywintypes.error, EOFError, ValueError) as exc:
                self.close()
                raise PyShotError("connection_lost", "PyShot broker connection was lost") from exc
            if response.get("id") != ident:
                raise PyShotError("protocol_error", "Mismatched IPC response")
            if "error" in response:
                raise PyShotError(**response["error"])
            return response["result"]

    def close(self):
        if self._handle is not None:
            self._handle.Close()
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
