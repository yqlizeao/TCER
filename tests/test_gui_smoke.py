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
    col.update(ps, set(), preferred_uid="b")
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


def test_ref_uid_disambiguates_same_key_cross_root():
    from pathlib import Path
    from tcer.core.models import ProjectRef
    from tcer.gui import views
    h = "c--GitHub-Demo"
    ra, rb = Path("/home/u/.claude"), Path("/home/u/.claude-proxy")
    ra_ref = ProjectRef(source="claude", key=h, display_name=h, cwd=None,
                        path=ra / "projects" / h, config_root=ra)
    rb_ref = ProjectRef(source="claude", key=h, display_name=h, cwd=None,
                        path=rb / "projects" / h, config_root=rb)
    other = ProjectRef(source="codex", key="cx", display_name="cx", cwd="/x", path=None)
    refs = [ra_ref, rb_ref, other]
    assert views.ref_uid(ra_ref) == "claude:.claude:" + h
    assert views.ref_uid(rb_ref) == "claude:.claude-proxy:" + h
    assert views.ref_uid(ra_ref) != views.ref_uid(rb_ref)
    assert views.ref_uid(other) == "codex:cx"
    assert views.find_ref_by_uid(refs, views.ref_uid(rb_ref)) is rb_ref
    assert views.find_ref_by_uid(refs, h) is ra_ref  # 裸 key 降级取首个
    assert views.find_ref_by_uid(refs, None) is None


def test_user_msgs_popup_renders(root):
    """UserMsgsPopup 正文用 SelectableLabel(disabled Text) —— 拦构建崩溃 +
    校验文本写入/置 disabled;不断言确切 height(无头下 count 取值不稳定)。"""
    from tcer.gui.popups import UserMsgsPopup
    from tcer.gui.widgets import SelectableLabel

    long_msg = "验证自动换行与高度撑开的多行用户消息文本。" * 15
    UserMsgsPopup(root, ["短消息", long_msg])
    root.update_idletasks()

    def _collect(w, acc):
        for c in w.winfo_children():
            if isinstance(c, SelectableLabel):
                acc.append(c)
            _collect(c, acc)

    labels: list = []
    _collect(root, labels)
    assert labels, "UserMsgsPopup 未渲染出 SelectableLabel"
    first = labels[0]
    assert first.cget("state") == "disabled"          # 只读但可选中复制
    assert "短消息" in first.get("1.0", "end-1c")      # 文本已写入
    assert int(first.cget("height")) >= 1


def test_calendar_popup_renders_and_picks(root):
    """CalendarPopup 构建 + 月份切换 + 选日期/清除回调不崩（拦 Toplevel 回归）。"""
    from tcer.gui.widgets import CalendarPopup

    picked: list = []
    anchor = tk.Label(root, text="x")
    anchor.pack()
    root.update_idletasks()
    p = CalendarPopup(anchor, lambda s: picked.append(s), anchor=anchor,
                      initial="2026-07-15")
    root.update_idletasks()
    assert p._year == 2026 and p._month == 7   # initial 解析定位到该月
    p._shift(-1)                                # ← 6 月
    assert p._month == 6
    p._shift(1)                                 # → 回 7 月
    p._pick(1)                                  # 点选 7/1 → 回调并关闭
    assert picked == ["2026-07-01"]

    p2 = CalendarPopup(anchor, lambda s: picked.append(s), anchor=anchor, initial="")
    root.update_idletasks()
    p2._clear()                                 # ✕ 清除 → 空串回调
    assert picked[-1] == ""
    anchor.destroy()


def test_filter_bar_presets_today(root):
    """FilterBar 预设：今天 → since=今日；全部 → 起止归空。"""
    from datetime import datetime
    from tcer.gui.views import FilterBar

    class _Ctl:
        def __init__(self):
            self.view_mode = tk.StringVar(value="project")

        def __getattr__(self, name):            # reanalyze / show_* / refresh_* 占位
            return lambda *a, **k: None

    frame = tk.Frame(root)
    frame.pack()
    bar = FilterBar(frame, _Ctl())
    root.update_idletasks()

    bar._set_preset("today")
    assert bar.since_var.get() == datetime.now().strftime("%Y-%m-%d")
    assert bar.until_var.get() == ""

    bar._set_preset("all")
    assert bar.since_var.get() == "" and bar.until_var.get() == ""
    frame.destroy()


def test_project_column_set_hidden(root):
    """ProjectColumn 隐藏范围外项目：pack_forget + 计数标签 + notify=False 不回调。"""
    from tcer.gui.views import ProjectColumn

    class _Ctl:
        def __init__(self):
            self.selected = "UNSET"

        def on_select_project(self, idx):
            self.selected = idx

        def __getattr__(self, name):
            return lambda *a, **k: None

    class _P:
        def __init__(self, key):
            self.key = key
            self.source = "claude"
            self.name = key

    frame = tk.Frame(root)
    frame.pack()
    ctl = _Ctl()
    col = ProjectColumn(frame, ctl)
    col.update([_P("a"), _P("b"), _P("c")], hidden_projects={1})
    root.update_idletasks()
    assert col._cards[1].frame.winfo_manager() == ""    # 隐藏 → 未被几何管理器管理
    assert col._cards[0].frame.winfo_manager() != ""    # 可见
    assert "隐藏 1" in col.count_label.cget("text")
    col.set_hidden(set())                                # 恢复全显
    root.update_idletasks()
    assert col._cards[1].frame.winfo_manager() != ""
    assert "隐藏" not in col.count_label.cget("text")
    ctl.selected = "UNSET"
    col.select_idx(0, notify=False)                      # 不回调 controller
    assert ctl.selected == "UNSET"
    frame.destroy()


def test_filter_bar_since_routes_to_apply_time_filter(root):
    """起始日期变化走 apply_time_filter；结束日期/任务类型走 reanalyze。"""
    from tcer.gui.views import FilterBar

    class _Ctl:
        def __init__(self):
            self.view_mode = tk.StringVar(value="project")
            self.calls: list = []

        def apply_time_filter(self):
            self.calls.append("apply")

        def reanalyze(self):
            self.calls.append("reanalyze")

        def refresh_projects(self):
            self.calls.append("refresh")

        def __getattr__(self, name):
            return lambda *a, **k: None

    frame = tk.Frame(root)
    frame.pack()
    ctl = _Ctl()
    bar = FilterBar(frame, ctl)
    root.update_idletasks()

    bar._set_preset("today")
    assert ctl.calls == ["apply"]
    ctl.calls.clear()
    bar._validate_and_reanalyze(bar.since_var)
    assert ctl.calls == ["apply"]
    ctl.calls.clear()
    bar._validate_and_reanalyze(bar.until_var)
    assert ctl.calls == ["reanalyze"]
    ctl.calls.clear()
    bar._on_task_type_change(None)
    assert ctl.calls == ["reanalyze"]
    frame.destroy()
