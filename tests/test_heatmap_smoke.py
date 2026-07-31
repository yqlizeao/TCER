"""HeatmapChart 离屏烟测：pytest 不覆盖 Canvas 绘制路径，语法正确但运行时
AttributeError（如 c.c.create_rectangle 手滑）能溜过全绿测试——本测试真实
实例化并驱动两种视图的 _draw、格子命中、下钻会话查找与色阶切换。

无显示环境（CI headless）自动 skip。
"""
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    r.geometry("1000x700")
    yield r
    r.destroy()


def _mk_report(i: int):
    ts = (datetime(2026, 6, 20) + timedelta(days=i % 40, hours=(i * 7) % 24)
          ).timestamp() * 1000
    return SimpleNamespace(
        usage=SimpleNamespace(started_at=ts),
        meta=SimpleNamespace(session_id=f"s{i}", title=f"会话 {i}",
                             path=SimpleNamespace(stem=f"s{i}")))


@pytest.fixture()
def heatmap(root, monkeypatch):
    from tcer.gui import charts
    from tcer.gui.charts import HeatmapChart
    rng = random.Random(1)
    monkeypatch.setattr(charts, "raw_value", lambda r, key: rng.expovariate(1 / 4))
    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True)
    selected = []
    hm = HeatmapChart(frame, controller=SimpleNamespace(
        on_select_session=selected.append))
    root.update()
    hm.update([_mk_report(i) for i in range(120)])
    hm._selected = selected
    return hm


def test_calendar_draws_cells(heatmap):
    assert len(heatmap.canvas.find_all()) > 50


def test_hours_view_and_back(heatmap, root):
    heatmap._set_view("hours")
    root.update()
    assert len(heatmap.canvas.find_all()) > 100   # 7×24 格 + 标签 + 边际条
    heatmap._set_view("calendar")
    root.update()
    assert len(heatmap.canvas.find_all()) > 50


def test_hit_and_drilldown(heatmap):
    heatmap._set_view("hours")
    ev = SimpleNamespace(x=int(heatmap._ox) + 5, y=int(heatmap._oy) + 5)
    bucket = heatmap._hit_bucket(ev)
    assert bucket == (0, 0)
    assert len(heatmap._sessions_in(bucket)) >= 1


def test_legend_quartiles_shown(heatmap):
    assert "≤" in heatmap._legend_var.get()


def test_down_sentiment_uses_bad_ramp(heatmap):
    from tcer.gui import theme
    heatmap._mode_var.set("返工率均值")
    heatmap._draw()
    assert heatmap._ramp() == theme.HEATMAP_RAMP_BAD
    heatmap._mode_var.set("会话数")
    heatmap._draw()
    assert heatmap._ramp() == theme.HEATMAP_RAMP
