"""GUI 冒烟测试:无头构建全部图表模式与新增弹窗(合成数据,不依赖本地会话)。

无显示环境(CI headless)自动 skip。目的不是像素级验证,而是拦住
NameError / 签名漂移 / 组件构建崩溃这类回归——此前靠手工冒烟。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tcer.core import metrics
from tcer.core.models import SessionMeta, TokenUsage, ToolOp, TurnStat

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="module")
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("无显示环境,跳过 GUI 冒烟")
    r.withdraw()
    yield r
    r.destroy()


def _report(sid: str, net: int = 300) -> metrics.SessionReport:
    meta = SessionMeta(session_id=sid, cwd="/tmp", title=f"标题-{sid}",
                       path=Path(f"/tmp/{sid}.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=200_000, output_tokens=100_000,
                   cache_read_input_tokens=700_000,
                   models={"claude-opus-4-8"},
                   started_at=1_770_000_000_000, ended_at=1_770_003_600_000,
                   assistant_msgs=5, user_msgs=3,
                   tool_calls={"Read": 4, "Edit": 3, "Bash": 2})
    u.per_model = {"claude-opus-4-8": __import__(
        "tcer.core.models", fromlist=["ModelUsage"]).ModelUsage(
        input_tokens=200_000, cache_read_input_tokens=700_000,
        output_tokens=100_000)}
    u.tool_ops = [ToolOp(0, "Read", "a.py"), ToolOp(1, "Edit", "a.py"),
                  ToolOp(1, "Bash", ""), ToolOp(2, "Edit", "b.py")]
    u.turn_stats = [
        TurnStat(0, ts=1_770_000_000_000, input_tokens=1000, cache_read=5000,
                 output_tokens=800, duration_ms=4000, tool_calls=2),
        TurnStat(1, ts=1_770_000_600_000, input_tokens=1200, cache_read=6000,
                 output_tokens=900, errors=1),
    ]
    return metrics.compute(meta, u, net_loc=net, task_type="feature")


@pytest.fixture(scope="module")
def reports():
    return [_report("s1", 300), _report("s2", 900), _report("s3", 120)]


def test_trend_chart_all_modes_and_update(root, reports):
    from tcer.gui.views import TrendChart

    frame = tk.Frame(root)
    frame.pack()
    tc = TrendChart(frame)
    tc.update(reports)
    for mode in ("scatter", "dashboard", "heatmap", "trend"):
        tc._mode.set(mode)
        tc._switch_mode()
        root.update_idletasks()
        tc.update(reports)  # 非趋势模式下 update 不得 TclError
        tc.select_session_by_sid("s1")
    # 仪表板按日聚合
    tc._mode.set("dashboard")
    tc._switch_mode()
    root.update_idletasks()
    tc._dashboard._daily.set(True)
    tc._dashboard._draw()
    frame.destroy()


def test_session_compare_popup(root, reports):
    from tcer.gui.popups import SessionComparePopup

    p = SessionComparePopup(root, reports, preselect_sid="s2")
    root.update_idletasks()
    assert len(p._selected()) >= 2
    p._vars[2].set(p._labels[2])
    p._render()
    root.update_idletasks()


def test_session_timeline_popup(root, reports):
    from tcer.gui.popups import SessionTimelinePopup

    p = SessionTimelinePopup(root, reports[0])
    root.update_idletasks()
    p._draw()


def test_tool_sequence_popup(root, reports):
    from tcer.gui.popups import ToolSequencePopup

    ToolSequencePopup(root, reports[0].usage, " · 测试")
    root.update_idletasks()


def test_project_overview_popup(root, reports):
    from tcer.gui.popups import ProjectOverviewPopup

    class _FakeAnalysis:
        def __init__(self, rep):
            self.aggregate = rep
            self.n_sessions = 1

    class _FakeRef:
        source = "claude"
        key = "p"
        name = "p"

    p = ProjectOverviewPopup(root, [(_FakeRef(), _FakeAnalysis(reports[0]))])
    root.update_idletasks()
    p._sort_by("tcer")
    p._sort_by("net")


def test_session_detail_popup(root, reports):
    from tcer.gui.popups import SessionDetailPopup

    SessionDetailPopup(root, reports[0])
    root.update_idletasks()


def test_metric_panel_renders(root, reports):
    from tcer.gui.views import MetricPanel

    class _Ctl:
        def __getattr__(self, name):  # show_* 回调占位
            return lambda *a, **k: None

    frame = tk.Frame(root)
    frame.pack()
    panel = MetricPanel(frame, _Ctl())
    panel.update(reports[0])
    root.update_idletasks()
    panel.clear()
    frame.destroy()


def test_flat_button_and_card_hover(root):
    from tcer.gui.widgets import Card, flat_button

    frame = tk.Frame(root)
    frame.pack()
    btn = flat_button(frame, "测试", lambda: None)
    btn.pack()
    btn2 = flat_button(frame, "主操作", lambda: None, primary=True)
    btn2.pack()
    card = Card(frame, on_click=lambda c: None)
    card._on_hover()
    card.set_selected(True)
    card._on_hover()   # 选中态 hover 不改边框
    card._on_unhover()
    card.set_selected(False)
    root.update_idletasks()
    frame.destroy()


def test_scrollframe_autohide_scrollbar(root):
    from tcer.gui.widgets import ScrollFrame

    frame = tk.Frame(root, width=200, height=120)
    frame.pack_propagate(False)
    frame.pack()
    sf = ScrollFrame(frame)
    # 内容超出 → 滚动条出现;清空 → 隐藏
    for i in range(40):
        tk.Label(sf.inner, text=f"行 {i}").pack()
    root.update_idletasks()
    sf.update_scroll()
    root.update()
    frame.destroy()


def test_project_column_empty_state_and_preferred(root):
    from tcer.gui.views import ProjectColumn

    class _Ctl:
        def on_select_project(self, idx):
            self.selected = idx
        def __getattr__(self, name):
            return lambda *a, **k: None

    frame = tk.Frame(root)
    frame.pack()
    ctl = _Ctl()
    col = ProjectColumn(frame, ctl)
    col.update([])   # 空状态引导不崩
    root.update_idletasks()

    class _P:
        def __init__(self, key):
            self.key = key
            self.source = "claude"
            self.name = key
    ps = [_P("a"), _P("b"), _P("c")]
    col.update(ps, set(), preferred_key="b")
    assert getattr(ctl, "selected", None) == 1  # 恢复到 b
    frame.destroy()


def test_ranking_falls_back_to_tcer(root, reports):
    from tcer.gui.views import CteiRankingView

    frame = tk.Frame(root)
    frame.pack()
    view = CteiRankingView(frame)
    # 合成 reports 有净增行与成本 → 有 CTEI:正常模式
    view.update(reports)
    assert not view._fallback_tcer
    # 去掉 CTEI → 回退按 TCER 排名,提示条出现
    import copy
    stripped = [copy.copy(r) for r in reports]
    for r in stripped:
        r.ctei = None
        r.grade = None
    view.update(stripped)
    assert view._fallback_tcer
    assert view._ranking and view._ranking[0][1] == max(r.tcer for r in stripped)
    root.update_idletasks()
    # 回到正常模式提示条隐藏
    view.update(reports)
    assert not view._fallback_tcer
    frame.destroy()
