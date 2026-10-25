import pytest

from pyshot_core import capture
from pyshot_core.errors import PyShotError


@pytest.fixture
def displays(monkeypatch):
    monitors = [{"monitor_id": "LEFT", "hmonitor": 11, "primary": False,
                 "x": -1920, "y": -200, "width": 1920, "height": 1080},
                {"monitor_id": "MAIN", "hmonitor": 12, "primary": True,
                 "x": 0, "y": 0, "width": 2560, "height": 1440}]
    monkeypatch.setattr(capture, "list_displays", lambda: monitors)


def test_negative_region_coordinates_remain_physical(displays):
    target = capture.resolve_target({"kind": "region", "region": {"x": -1801, "y": -101, "width": 301, "height": 201}})
    assert target["x"] == -1801 and target["hmonitor"] == 11
    assert target["monitor_x"] == -1920 and target["width"] == 301


def test_primary_selection(displays):
    assert capture.resolve_target({"kind": "monitor"})["monitor_id"] == "MAIN"


def test_removed_target_is_error(displays):
    with pytest.raises(PyShotError, match="unavailable"):
        capture.resolve_target({"kind": "monitor", "monitor_id": "OLD"})


@pytest.mark.parametrize("region", [dict(x=True, y=0, width=5, height=5), dict(x=0, y=0, width=-1, height=5),
                                   dict(x=9000, y=0, width=5, height=5)])
def test_invalid_regions(displays, region):
    with pytest.raises(PyShotError):
        capture.resolve_target({"kind": "region", "region": region})
