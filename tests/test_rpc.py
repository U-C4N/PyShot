import threading

import pytest
import win32security

from pyshot_core import rpc
from pyshot_core.errors import PyShotError


def test_pipe_dacl_only_current_user():
    attributes = rpc.security_attributes()
    dacl = attributes.SECURITY_DESCRIPTOR.GetSecurityDescriptorDacl()
    assert dacl.GetAceCount() == 1
    assert win32security.ConvertSidToStringSid(dacl.GetAce(0)[2]) == rpc.identity()[0]


def test_real_named_pipe_two_clients_and_disconnect(monkeypatch):
    import uuid
    name = rf"\\.\pipe\PyShot.test.{uuid.uuid4().hex}"
    monkeypatch.setattr(rpc, "pipe_name", lambda: name)
    owners = set()
    disconnected = threading.Event()
    class Service:
        def call(self, owner, method, params):
            owners.add(owner)
            if method == "error":
                raise PyShotError("example", "Expected failure", {"id": 1})
            return params
        def disconnect(self, owner):
            disconnected.set()
        def close(self):
            pass
    server = rpc.BrokerServer(Service())
    server.start()
    try:
        with rpc.BrokerClient(autostart=False) as first, rpc.BrokerClient(autostart=False) as second:
            assert first.call("echo", {"text": "Türkçe"}) == {"text": "Türkçe"}
            assert second.call("echo", {"n": 2}) == {"n": 2}
            with pytest.raises(PyShotError) as exc:
                first.call("error")
            assert exc.value.code == "example"
            assert len(owners) == 2
        assert disconnected.wait(3)
    finally:
        server.close()


def test_pipe_capacity_does_not_shutdown_broker(monkeypatch):
    import uuid
    import time
    monkeypatch.setattr(rpc, "pipe_name", lambda: rf"\\.\pipe\PyShot.test.{uuid_value}")
    uuid_value = uuid.uuid4().hex
    class Service:
        def call(self, owner, method, params):
            return {"ok": True}
        def disconnect(self, owner):
            pass
        def close(self):
            pass
    server = rpc.BrokerServer(Service())
    server.start()
    clients = []
    try:
        for _ in range(32):
            client = rpc.BrokerClient(autostart=False)
            clients.append(client)
            assert client.call("echo")["ok"]
        time.sleep(0.15)
        assert not server._stopped.is_set()
        assert clients[0].call("echo")["ok"]
        clients.pop().close()
        with rpc.BrokerClient(autostart=False) as extra:
            assert extra.call("echo")["ok"]
    finally:
        for client in clients:
            client.close()
        server.close()
