from types import SimpleNamespace

import pytest

from tcer.gui.platform import dpi_pixels, fit_window, window_client_bounds
from tcer.gui.views import metric_columns


@pytest.mark.parametrize("dpi, expected", [(96, 148), (96.1, 148), (144, 222), (192, 296), (120, 185)])
def test_dpi_pixels(dpi, expected):
    widget = SimpleNamespace(winfo_fpixels=lambda unit: dpi)
    assert dpi_pixels(widget, 148) == expected
    assert dpi_pixels(widget, 0) == 0


@pytest.mark.parametrize("size, expected", [
    ((1472, 900, 64, 110), (1472, 900, 64, 110)),
    ((2160, 1080, 64, 110), (1568, 1050, 0, 0)),
    ((1080, 540, -100, -100), (1080, 540, 0, 0)),
    ((100, 100, 1600, 1200), (100, 100, 1468, 950)),
])
def test_fit_window(size, expected):
    assert fit_window(*size, (0, 0, 1568, 1050)) == expected


def test_window_client_bounds_fallback(monkeypatch):
    monkeypatch.setattr("tcer.gui.platform.PLATFORM", "linux")
    root = SimpleNamespace(winfo_screenwidth=lambda: 1600, winfo_screenheight=lambda: 1200)
    assert window_client_bounds(root) == (0, 0, 1600, 1200)


@pytest.mark.parametrize("width, minimum, expected", [
    (1200, 150, 6), (900, 200, 4), (599, 200, 2), (0, 200, 1), (100, 0, 6),
])
def test_metric_columns(width, minimum, expected):
    assert metric_columns(width, minimum) == expected
