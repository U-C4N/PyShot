import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pyshot
from pyshot_core.windows import set_dpi_awareness


def api(context=False, legacy=False, current=0):
    return SimpleNamespace(SetProcessDpiAwarenessContext=Mock(return_value=context),
                           SetProcessDPIAware=Mock(return_value=legacy),
                           GetThreadDpiAwarenessContext=Mock(return_value=123),
                           AreDpiAwarenessContextsEqual=Mock(return_value=False),
                           GetAwarenessFromDpiAwarenessContext=Mock(return_value=current))


def test_failed_dpi_calls_do_not_report_success():
    assert set_dpi_awareness(api(), SimpleNamespace(SetProcessDpiAwareness=Mock(return_value=-1))) == "none"


def test_hresult_success_is_zero():
    assert set_dpi_awareness(api(), SimpleNamespace(SetProcessDpiAwareness=Mock(return_value=0))) == "per-monitor"


def test_preexisting_dpi_is_queried():
    user = api(current=2)
    shcore = SimpleNamespace(SetProcessDpiAwareness=Mock(return_value=-2147024891))
    assert set_dpi_awareness(user, shcore) == "per-monitor"
    user.SetProcessDPIAware.assert_not_called()


def test_mutex_access_denied_is_running(monkeypatch):
    kernel = SimpleNamespace(OpenMutexW=Mock(return_value=0), CloseHandle=Mock())
    monkeypatch.setattr(pyshot, "_K32", kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    assert pyshot._is_running()
    kernel.CloseHandle.assert_not_called()


def test_missing_mutex_is_not_running(monkeypatch):
    monkeypatch.setattr(pyshot, "_K32", SimpleNamespace(OpenMutexW=lambda *a: 0))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2)
    assert not pyshot._is_running()


def test_quit_checks_setevent_and_closes_handle(monkeypatch):
    kernel = SimpleNamespace(OpenEventW=Mock(return_value=42), SetEvent=Mock(return_value=0), CloseHandle=Mock())
    monkeypatch.setattr(pyshot, "_K32", kernel)
    assert not pyshot._signal_quit()
    kernel.CloseHandle.assert_called_once_with(42)
