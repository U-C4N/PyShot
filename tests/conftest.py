"""Shared fixtures for the test modules that were added after test_pure.py.

test_pure.py keeps its own path setup and log fixture; a module-level fixture
of the same name simply overrides the one here.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyshot  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    # Keeps bogus entries out of the real %TEMP%\pyshot.log, which the README's
    # Troubleshooting section tells users to read.
    monkeypatch.setattr(pyshot, "LOG_FILE", tmp_path / "pyshot.log")


@pytest.fixture(scope="session")
def tk_root():
    """One hidden Tk root for every test that needs a canvas or a PhotoImage."""
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as exc:      # no usable display; not expected on Windows
        if os.environ.get("CI"):
            raise                   # in CI a missing Tk must fail, not skip every Tk test
        pytest.skip(f"Tk is unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()
