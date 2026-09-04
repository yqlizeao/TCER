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
    from tcer.gui.widgets import SelectableLabel

    SessionDetailPopup(root, reports[0])
    root.update_idletasks()

    # 元数据值（session ID / 工作目录）渲染为可选中复制文本
    sels = []
    def _collect(w):
        for c in w.winfo_children():
            if isinstance(c, SelectableLabel):
                sels.append(c)
            _collect(c)
    _collect(root)
    assert sels, "SessionDetailPopup 未渲染 SelectableLabel"
    sid = reports[0].meta.session_id or ""
    assert any(sid and sid in s.get("1.0", "end-1c") for s in sels), \
        "session ID 应出现在可选中文本中（可拖选复制）"


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


def test_metric_panel_group_collapse(root, reports):
    """MetricPanel 分组标题可点击折叠/展开整组（header 绑定 + body pack_forget）。"""
    from tcer.gui.views import MetricPanel

    class _Ctl:
        def __getattr__(self, name):
            return lambda *a, **k: None

    frame = tk.Frame(root)
    frame.pack()
    panel = MetricPanel(frame, _Ctl())
    panel.update(reports[0])
    root.update_idletasks()
    assert panel._groups
    gs = panel._groups[0]                       # G1 会话概况：默认展开
    assert not gs.collapsed and gs.body.winfo_manager() != ""
    g4 = panel._groups[3]                       # G4 代码产出与质量：默认折叠
    assert g4.collapsed and g4.body.winfo_manager() == ""
    assert g4.arrow.cget("text").startswith("▶")
    panel._toggle_group(gs)
    root.update_idletasks()
    assert gs.collapsed
    assert gs.body.winfo_manager() == ""        # pack_forget → 未被几何管理器管理
    assert gs.arrow.cget("text").startswith("▶")
    panel._toggle_group(gs)
    root.update_idletasks()
    assert not gs.collapsed and gs.body.winfo_manager() != ""
    assert gs.arrow.cget("text").startswith("▼")
    frame.destroy()


def test_model_compare_group_collapse(root, reports):
    """ModelCompareView 分组可折叠；M_QUAL（代码质量与行为）默认折叠，跨 update 保持。"""
    from tcer.gui.views import ModelCompareView

    frame = tk.Frame(root)
    frame.pack()
    cv = ModelCompareView(frame)
    cv.update(reports)
    root.update_idletasks()
    qual = next((g for g in cv._groups if "代码质量" in g.name), None)
    assert qual is not None
    assert qual.collapsed and qual.body.winfo_manager() == ""   # M_QUAL 默认折叠
    assert qual.arrow.cget("text").startswith("▶")
    cv._toggle_group(qual, "M_QUAL")
    root.update_idletasks()
    assert not qual.collapsed and qual.body.winfo_manager() != ""
    assert cv._group_collapsed["M_QUAL"] is False                # 状态记入 dict
    other = next(g for g in cv._groups if "代码质量" not in g.name)
    assert not other.collapsed                                   # 其他组默认展开
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


def test_mac_button_command_compat(root):
    """_MacButton：command 兼容 tk.Button 的构造传入与 .config(command=) 重设。

    Windows 上 flat_button 仍返回 tk.Button；本测试直接构造 _MacButton 验证其
    command 拦截逻辑（菜单按钮 _make_tool_menu 依赖 .config(command=)）。
    """
    from tcer.gui.widgets import _MacButton

    fired = []
    def cb_a():
        fired.append("a")
    def cb_b():
        fired.append("b")
    btn = _MacButton(root, command=cb_a, base_bg="#111", hover_bg="#222",
                     text="t", bg="#111", fg="#fff")
    assert btn._command is cb_a, "构造时 command 应记录"
    assert btn._click_id is not None, "command 应绑定 <Button-1>"

    btn.config(command=cb_b)               # 菜单按钮 _make_tool_menu 的用法
    assert btn._command is cb_b, "config(command=) 应更新 command"
    assert btn._click_id is not None

    btn.config(command=None)               # 解绑
    assert btn._command is None and btn._click_id is None

    btn.config(bg="#333")                  # 普通 config 走 tk.Label，不被 command 拦截
    assert btn.cget("bg") == "#333"


def test_check_row_toggle(root):
    """CheckRow：整行点击 toggle var；外部改 var 后 _draw 反映选中态。"""
    from tcer.gui.widgets import CheckRow

    var = tk.BooleanVar(value=False)
    frame = tk.Frame(root)
    frame.pack()
    calls: list = []
    row = CheckRow(frame, "测试项", var, on_toggle=lambda: calls.append(var.get()))
    root.update_idletasks()
    assert not var.get()
    row.click()
    assert var.get() and calls == [True]
    var.set(False)          # 外部改 var（如单选取消其他）
    row._draw()             # 重画应反映 var=False
    row.click()
    assert var.get()
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
    from tcer.gui.views import ScoreRankingView

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame)
    # 合成 reports 有净增行与成本 → 有综合效率分:正常模式
    view.update(reports)
    assert not view._fallback_tcer
    # 去掉综合效率分 → 回退按 TCER 排名,提示条出现
    import copy
    stripped = [copy.copy(r) for r in reports]
    for r in stripped:
        r.score = None
        r.tier = None
    view.update(stripped)
    assert view._fallback_tcer
    assert view._ranking and view._ranking[0][1] == max(r.tcer for r in stripped)
    root.update_idletasks()
    # 回到正常模式提示条隐藏
    view.update(reports)
    assert not view._fallback_tcer
    frame.destroy()


def _walk_labels(w, out):
    from tcer.gui.widgets import SelectableLabel
    for c in w.winfo_children():
        if isinstance(c, SelectableLabel):  # tk.Text 子类：用 get 取全文本（可选中复制）
            try:
                out.append(c.get("1.0", "end-1c"))
            except tk.TclError:
                pass
        elif isinstance(c, tk.Label):
            try:
                out.append(c.cget("text"))
            except tk.TclError:
                pass
        _walk_labels(c, out)


def test_ranking_decompose_uses_ssot_labels(root, reports):
    """选中会话后三轴分解面板渲染，标签取自指标 SSOT（综合效率分全称 +
    产出效率/成本/质量三轴中文名）。"""
    from tcer.gui.views import ScoreRankingView, _SCORE_NAME
    from tcer.gui.metric_defs import SCORE_AXES

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame)
    view.update(reports)
    kids = view._tree.get_children()
    assert kids, "排名表应有数据行"
    # 会话视角由 set_view_mode 进入（点行只选中、不翻转视角）。
    view.set_view_mode("session", reports[0])
    root.update_idletasks()

    texts = []
    _walk_labels(view._decomp_inner, texts)
    blob = "\n".join(texts)
    # 全称出现，三轴中文名出现（SSOT 驱动）
    assert _SCORE_NAME in blob
    for a in SCORE_AXES:
        assert a.name in blob, f"轴名缺失: {a.name}"
    # 概览区一句话解释可见（去术语门槛）
    assert any("产出效率" in t and "成本"in t and "质量" in t for t in texts)
    frame.destroy()


def test_ranking_row_click_keeps_project_view(root, reports):
    """点排名行不翻转视角：项目视角下点行 → 仍是项目视角（右栏保持项目洞察），
    只把选中的 sid 通知控制器。视角切换单一入口 = 左上角分段控件。"""
    from tcer.gui.views import ScoreRankingView

    calls = []

    class _Ctl:
        def on_select_session(self, sid):
            calls.append(sid)

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame, controller=_Ctl())
    view.update(reports)
    assert view._view_mode == "project"

    kids = view._tree.get_children()
    view._tree.selection_set(kids[0])
    view._on_tree_select()
    root.update_idletasks()

    assert view._view_mode == "project", "点排名行不得翻转到会话视角"
    assert calls, "点行应通知控制器选中 sid"
    texts = []
    _walk_labels(view._decomp_inner, texts)
    assert any("洞察与意见 (项目)" in t for t in texts), "右栏应保持项目视角"
    frame.destroy()


def test_ranking_insights_section_renders(root, reports):
    """洞察与意见区块渲染：至少一条带标记(勾/箭头)的可执行洞察出现在分解面板。"""
    from tcer.gui.views import ScoreRankingView

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame)
    view.update(reports)
    kids = view._tree.get_children()
    assert kids
    # 会话视角由 set_view_mode 进入（点行只选中、不翻转视角）。
    view.set_view_mode("session", reports[0])
    root.update_idletasks()

    texts = []
    _walk_labels(view._decomp_inner, texts)
    # 章节标题出现（CollapsibleSection 头部可能带 ▼ 前缀，故用 in）
    assert any("洞察与意见" in t for t in texts)
    # 至少一条洞察带行首标记（\u2713 亮点 / ! 拖累 / \u2192 改进）
    assert any(t[:1] in ("\u2713", "!", "\u2192") for t in texts)
    frame.destroy()


def test_ranking_empty_state_shows_project_insights(root):
    """空态（未选会话）展示项目级跨会话洞察：多会话反复出现的系统性短板。"""
    import copy
    from tcer.gui.views import ScoreRankingView

    base = _report("p0", 300)
    # 造 3 个高返工会话（系统性 churn drag）。
    reps = []
    for i in range(3):
        r = copy.copy(base)
        r.meta = copy.copy(base.meta)
        r.meta.session_id = f"p{i}"
        r.churn_ratio = 0.5
        reps.append(r)

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame)
    view.update(reps)  # no selection -> empty state renders project insights
    root.update_idletasks()

    texts = []
    _walk_labels(view._decomp_inner, texts)
    assert any("洞察与意见 (项目)" in t for t in texts), "empty state should show 项目视角洞察"
    assert any("系统性" in t for t in texts), "systemic drag should surface"
    frame.destroy()


def test_ranking_dual_view_switch(root, reports):
    """排名页项目/会话双视角：视角只由 set_view_mode（左上角分段控件）切换。
    点排名行只选中会话（触发 on_select_session），绝不翻转视角；程序化选中不触发回调。"""
    from tcer.gui.views import ScoreRankingView

    calls = []

    class _Ctl:
        def on_select_session(self, sid):
            calls.append(("sess", sid))

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame, controller=_Ctl())
    view.update(reports)

    def texts():
        out = []
        _walk_labels(view._decomp_inner, out)
        return out

    # 项目视角（默认）
    assert any("洞察与意见 (项目)" in t for t in texts())
    # 控制器驱动会话视角
    view.set_view_mode("session", reports[0])
    root.update_idletasks()
    assert any("洞察与意见 (会话)" in t for t in texts())
    assert view._tree.selection()  # 会话行高亮
    # 回到项目视角，清空选中
    view.set_view_mode("project")
    root.update_idletasks()
    assert any("洞察与意见 (项目)" in t for t in texts())
    assert not view._tree.selection()
    # 用户点排名行 → on_select_session 一次；视角保持项目（不翻转）。
    calls.clear()
    kids = view._tree.get_children()
    view._tree.selection_set(kids[1])
    view._on_tree_select()
    assert calls and calls[0][0] == "sess"
    assert view._view_mode == "project", "点排名行不得翻转视角"
    assert any("洞察与意见 (项目)" in t for t in texts()), "点行后右栏仍是项目视角"
    # 程序化选中不触发回调
    calls.clear()
    view.set_view_mode("session", reports[2])
    assert calls == []
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
    # module-scoped root 累积先前测试的 Toplevel，用 any/all 避免与其它弹窗的
    # SelectableLabel 顺序耦合（labels[0] 可能是早先弹窗的 session id 等）。
    texts = [s.get("1.0", "end-1c") for s in labels]
    assert all(s.cget("state") == "disabled" for s in labels), "SelectableLabel 应只读"
    assert any("短消息" in t for t in texts), "短消息应写入某 SelectableLabel"
    assert any(int(s.cget("height")) >= 1 for s in labels), "高度应撑开"


def test_user_msgs_popup_grouped(root):
    """聚合视图：``[(会话标识, [消息])]`` 形态渲染来源标识条 + 各会话消息卡片。"""
    from tcer.gui.popups import UserMsgsPopup
    from tcer.gui.widgets import SelectableLabel

    grouped = [
        ("会话一 · abc123def456…", ["来自会话一的消息", "会话一第二条"]),
        ("会话二 · zzz999…", ["来自会话二的消息"]),
    ]
    UserMsgsPopup(root, grouped)
    root.update_idletasks()

    def _collect(w, acc):
        for c in w.winfo_children():
            if isinstance(c, SelectableLabel):
                acc.append(c)
            _collect(c, acc)

    labels: list = []
    _collect(root, labels)
    texts = [lbl.get("1.0", "end-1c") for lbl in labels]
    # 3 条消息全部渲染为可选中正文
    assert any("来自会话一的消息" in t for t in texts)
    assert any("来自会话二的消息" in t for t in texts)
    # 来源标识条文字出现在某个普通 Label（非 SelectableLabel）里
    found_bar = {"one": False, "two": False}

    def _scan_labels(w):
        import tkinter as tk
        for c in w.winfo_children():
            if isinstance(c, tk.Label):
                t = c.cget("text")
                if "会话一 ·" in t:
                    found_bar["one"] = True
                if "会话二 ·" in t:
                    found_bar["two"] = True
            _scan_labels(c)

    _scan_labels(root)
    assert found_bar["one"] and found_bar["two"], "来源标识条未渲染"


def test_session_label_date_title_sid():
    """会话来源标识 = 日期 · 标题(限长) · sessionid(限长)。"""
    from types import SimpleNamespace
    from tcer.gui.app import TcerGui
    from tcer.core import format as fmt_mod

    # 2026-07-30 local
    ms = int(__import__("datetime").datetime(2026, 7, 30, 9, 0).timestamp() * 1000)
    long_title = "扩展项目来源图标方案与配色统一收口整理并补充说明文档细节"  # > 24 字符
    assert len(long_title) > 24
    meta = SimpleNamespace(title=long_title,
                           session_id="2162e1ca-0c5b-4d9e-abcd-ffff")
    report = SimpleNamespace(meta=meta, usage=SimpleNamespace(started_at=ms))
    label = TcerGui._session_label(report)
    assert label.startswith("2026-07-30 · ")
    # 标题截断到 24 字符 + 省略号
    assert long_title[:24] + "…" in label
    # sessionid 截断到 12 字符 + 省略号
    assert "2162e1ca-0c5…" in label


def test_session_label_no_timestamp_omits_date():
    """无 started_at 时省略日期段，仅标题(+sid)。"""
    from types import SimpleNamespace
    from tcer.gui.app import TcerGui

    meta = SimpleNamespace(title="短标题", session_id="abc123")
    report = SimpleNamespace(meta=meta, usage=SimpleNamespace(started_at=None))
    label = TcerGui._session_label(report)
    assert label == "短标题 · abc123"


def test_claude_user_messages_excludes_subagent_prompts(tmp_path):
    """子代理文件的 user 消息(Task 派发 prompt)不并入用户消息弹窗。

    子代理不与真人交互,其 jsonl 里的 user 消息全是主代理经 Task 工具下发的
    指令("You are researching…"),并入会混出假"用户消息"。
    """
    import json
    import types
    from tcer.gui.app import TcerGui

    sid = "SID-test"
    proj = tmp_path / "hash"
    proj.mkdir(parents=True)
    main = proj / f"{sid}.jsonl"
    main.write_text(json.dumps({"type": "user", "message": {"role": "user",
                     "content": [{"type": "text", "text": "我的真实消息"}]}}) + "\n",
                    encoding="utf-8")
    sub_dir = proj / sid / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-x.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": "You are researching the local repo…"}]}}) + "\n",
        encoding="utf-8")

    report = types.SimpleNamespace(meta=types.SimpleNamespace(path=main))
    msgs = TcerGui._claude_user_messages(report)
    assert msgs == ["我的真实消息"]
    assert not any(m.startswith("You are ") for m in msgs)


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


def test_files_touched_popup_with_search_footprint(root):
    """FilesTouchedPopup 三块（文件列表 / 目录热度 / 搜索足迹）都渲染不崩；
    搜索路径经独立参数传入，与文件列表分开。"""
    from tcer.gui.popups import FilesTouchedPopup
    from tcer.gui.widgets import SelectableLabel

    details = {"/proj/a.py": 3, "/proj/sub/b.py": 2, "/proj/c.py": 1}
    searched = {"/proj/sub": 18, "/proj": 9, "/proj/a.py": 4}
    FilesTouchedPopup(root, details, searched)
    root.update_idletasks()

    # 文件/搜索路径渲染为可选中复制
    sels = []
    def _collect(w):
        for c in w.winfo_children():
            if isinstance(c, SelectableLabel):
                sels.append(c)
            _collect(c)
    _collect(root)
    texts = [s.get("1.0", "end-1c") for s in sels]
    assert any("a.py" in t for t in texts), "文件路径应可选中间"

    # 无 searched 时（Claude 常态）也不崩
    FilesTouchedPopup(root, details, None)
    root.update_idletasks()


def test_update_popup_release_notes_selectable(root):
    """UpdatePopup 发布说明渲染为可选中复制文本（A 类长文本改造）。"""
    from tcer.gui.popups import UpdatePopup
    from tcer.gui.widgets import SelectableLabel

    release = {"tag": "v9.9.9", "notes": "发布说明正文样例，可选中复制。",
               "url": "https://example.com"}
    UpdatePopup(root, "v1.0.0", release, controller=None)
    root.update_idletasks()

    sels = []
    def _collect(w):
        for c in w.winfo_children():
            if isinstance(c, SelectableLabel):
                sels.append(c)
            _collect(c)
    _collect(root)
    texts = [s.get("1.0", "end-1c") for s in sels]
    assert any("发布说明正文样例" in t for t in texts), "发布说明应渲染为可选中文本"


def test_ranking_insights_are_selectable_no_copy_button(root):
    """ScoreView 洞察/规则/推荐文本为 SelectableLabel；复制按钮已由选中复制取代。"""
    import copy
    from tcer.gui.views import ScoreRankingView
    from tcer.gui.widgets import SelectableLabel

    base = _report("p0", 300)
    reps = []
    for i in range(3):
        r = copy.copy(base)
        r.meta = copy.copy(base.meta)
        r.meta.session_id = f"p{i}"
        r.churn_ratio = 0.5
        reps.append(r)

    frame = tk.Frame(root)
    frame.pack()
    view = ScoreRankingView(frame)
    view.update(reps)
    root.update_idletasks()

    # 分解面板内有 SelectableLabel（洞察/规则/推荐文本）
    sels = []
    def _collect(w):
        for c in w.winfo_children():
            if isinstance(c, SelectableLabel):
                sels.append(c)
            _collect(c)
    _collect(view._decomp_inner)
    assert sels, "洞察区应渲染 SelectableLabel"

    # 复制按钮已被选中复制取代 —— 遍历 Button 确认无「复制规则/复制指令」残留
    btn_texts = []
    def _scan_btn(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Button):
                btn_texts.append(c.cget("text"))
            _scan_btn(c)
    _scan_btn(view._decomp_inner)
    assert not any("复制规则" in t or "复制指令" in t for t in btn_texts), \
        "选中复制取代后不应再有复制按钮"
    frame.destroy()


def test_radar_popup_axes_track_live_baselines(root, reports):
    """RadarPopup 构建不崩 + 归一化刻度从 SSOT 取（不硬编码 76.59/8.22）。

    历史 bug：radar 的 tcer/cpe 轴 ref 硬编码，config 基准迁移后刻度失真。改为
    _resolve_axes 从 metrics.TCER_BASELINE / CPE_BASELINE 实时取值；综合效率分轴
    有界 0–100，ref 固定 100（÷100 归一），不依赖基准。
    """
    from tcer.gui.popups import RadarPopup

    axes = {k: ref for k, _n, ref in RadarPopup._resolve_axes()}
    assert axes["tcer"] == metrics.TCER_BASELINE
    assert axes["cpe"] == metrics.CPE_BASELINE
    assert axes["score"] == 100.0
    # 构建弹窗（无头下 canvas 渲染不崩即通过）。
    RadarPopup(root, reports[0], reports)
    root.update_idletasks()


def test_session_column_pin_flag_marks(root, reports):
    """会话卡片置顶/红旗:置顶排序、标记图标构建、_apply_marks 重排不崩。"""
    from tcer.gui.views import SessionColumn

    class _Ctl:
        def __init__(self):
            self.root = root
        def on_select_session(self, sid): pass
        def show_session_detail(self, sid): pass
        def toggle_session_pin(self, sid): pass
        def toggle_session_flag(self, sid): pass
        def delete_session(self, report): pass

    col = SessionColumn(root, _Ctl())
    # s1 置顶(即便不是最新也排第一),s2 红旗(建 flag-on 图标)
    col.update(reports, pinned={"s1"}, flagged={"s2"})
    root.update_idletasks()
    assert len(col._cards) == 3
    assert col._reports[0].meta.session_id == "s1"   # 置顶优先于时间序
    assert "s2" in col._flagged

    # _apply_marks 改置顶集合 → 重排,新置顶项排前(s2 仍红旗)
    col._apply_marks({"s3"}, {"s2"}, keep_sid="s3", reset=False)
    root.update_idletasks()
    assert col._reports[0].meta.session_id == "s3"
    assert len(col._cards) == 3
    # 红旗快速过滤：开启只看 flagged 会话(s2)
    col._flag_only.set(True)
    col._render()
    root.update_idletasks()
    assert len(col._reports) == 1
    assert col._reports[0].meta.session_id == "s2"
    # 模型模糊搜索:搜 "opus" 匹配 claude-opus-4-8(全部 3 个)
    col._flag_only.set(False)
    col._filter_var.set("opus")
    root.update_idletasks()
    assert len(col._reports) == 3
    col._filter_var.set("")
    root.update_idletasks()



def test_session_column_right_click_menu(root, reports, monkeypatch):
    """会话卡片右键菜单包含 LLM 深度解读独立分组。"""
    from tcer.gui.views import SessionColumn, FlatMenu
    called = []

    class _Ctl:
        def __getattr__(self, name):
            return lambda *a, **kw: called.append(name)

    col = SessionColumn(root, _Ctl())
    col.update(reports)
    root.update_idletasks()

    # 捕获 FlatMenu.tk_popup
    created_menus = []
    orig_popup = FlatMenu.tk_popup
    monkeypatch.setattr(FlatMenu, "tk_popup", lambda self, x, y: created_menus.append(self))

    class FakeEvent:
        x_root = 100
        y_root = 100

    r = col._reports[0]
    sid = r.meta.session_id
    col._on_right_click(FakeEvent(), r, sid)
    assert len(created_menus) == 1
    menu = created_menus[0]
    labels = []
    for row in menu._body.winfo_children():
        for ch in row.winfo_children():
            if isinstance(ch, tk.Label):
                labels.append(ch.cget("text"))
    assert "LLM 过程解读" in labels
    assert "相空间动力学分析" in labels

def test_on_analysis_bail_paths_reset_status():
    """切时间区间后当前 generation 的结果被丢弃时，右上角状态必须落地，
    不能永远卡在「分析中…」（历史 bug：静默 return 泄漏状态，只能重开项目）。

    - proj is None（时间筛选后无可见项目被清空）→ 复位「就绪」。
    - ref 不匹配（结果属旧选中项目）→ 不静默丢弃，立即为当前项目重跑。
    """
    from types import SimpleNamespace
    from tcer.gui.app import TcerGui
    from tcer.gui import views

    # -- 分支 1：proj is None → set_status("就绪") --
    statuses = []
    stub = SimpleNamespace(
        _selected_project=lambda: None,
        filter=SimpleNamespace(set_status=lambda s: statuses.append(s)),
    )
    a = SimpleNamespace(project_ref=None)
    TcerGui._on_analysis(stub, a)
    assert statuses == ["就绪"]

    # -- 分支 2：ref 错位 → 触发一次 reanalyze，不静默泄漏 --
    proj = SimpleNamespace(source="claude", key="cur", config_root=None)
    other = SimpleNamespace(source="claude", key="old", config_root=None)
    # ref_uid 需能区分两个 ref；否则本测试前提不成立
    assert views.ref_uid(proj) != views.ref_uid(other)
    reanalyzed = []
    stub2 = SimpleNamespace(
        _selected_project=lambda: proj,
        filter=SimpleNamespace(set_status=lambda s: None),
        reanalyze=lambda: reanalyzed.append(True),
    )
    a2 = SimpleNamespace(project_ref=other)
    TcerGui._on_analysis(stub2, a2)
    assert reanalyzed == [True]


def test_trend_chart_without_pil(root, reports, monkeypatch):
    """缺 Pillow（零依赖环境）时图表回退 canvas 原生绘制，不抛 ImportError。"""
    from tcer.gui import charts
    from tcer.gui.views import TrendChart

    monkeypatch.setattr(charts, "_HAS_PIL", False)
    frame = tk.Frame(root)
    frame.pack()
    tc = TrendChart(frame)
    tc.update(reports)
    for mode in ("trend", "scatter"):
        tc._mode.set(mode)
        tc._switch_mode()
        root.update_idletasks()
        tc.update(reports)
    frame.destroy()


def test_trend_chart_matrix_mode(root, reports):
    """相关矩阵模式：绘制 N×N 网格 + 点击下钻散点轴。"""
    from tcer.gui.views import TrendChart

    frame = tk.Frame(root)
    frame.pack()
    tc = TrendChart(frame)
    tc.update(reports)
    tc._set_mode("matrix")
    root.update_idletasks()
    tc.update(reports)
    assert len(tc._matrix_chart.canvas.find_all()) >= 16  # 4×4 对角+格子起步
    # 下钻：设好散点 X/Y
    tc._drill_to_scatter("cost", "tcer")
    assert tc._mode.get() == "scatter"
    assert tc._scatter_chart._label_to_key[tc._scatter_chart._x_var.get()] == "cost"
    assert tc._scatter_chart._label_to_key[tc._scatter_chart._y_var.get()] == "tcer"
    frame.destroy()


def test_session_timeline_drill_and_overlays(root, reports):
    """时间线弹窗：点击回合展开明细；CHR/累计净增/压缩竖线不炸。"""
    from tcer.gui.popups import SessionTimelinePopup

    r = reports[0]
    # 造叠加曲线数据（压缩 + 逐回合 LOC）
    r.usage.compaction_turns = [1]
    r.usage.turn_net_locs = [(0, 10, 0), (1, -2, 0)]
    r.usage.tool_ops = [ToolOp(0, "Edit", "a.py")]
    p = SessionTimelinePopup(root, r)
    root.update_idletasks()
    p._draw()
    # 模拟点击第一根回合条
    x0, x1, i = p._bar_x[0]
    p._on_click(type("E", (), {"x": (x0 + x1) / 2})())
    assert len(p._detail.winfo_children()) == 1
    # 点空白收起
    p._on_click(type("E", (), {"x": 2})())
    assert len(p._detail.winfo_children()) == 0


def test_session_timeline_convergence_view(root, reports):
    """收敛诊断视图：剪刀差/重试横带/标记绘制不炸；降级与往返切换。"""
    from tcer.gui.popups import SessionTimelinePopup

    r = reports[0]
    r.usage.compaction_turns = [1]
    r.usage.turn_net_locs = [(0, 10, 0), (1, -2, 0), (2, 30, 0)]
    # 同文件 3 连 Edit（turn 1-3）→ 重试循环 span；turn 3 无 TurnStat
    # （回合号空洞）恰好覆盖 span 端点钳边。
    r.usage.tool_ops = [ToolOp(0, "Read", "a.py"),
                        ToolOp(1, "Edit", "a.py"), ToolOp(2, "Edit", "a.py"),
                        ToolOp(3, "Edit", "a.py"), ToolOp(4, "Edit", "b.py")]
    r.usage.turn_stats = [
        TurnStat(0, ts=1_770_000_000_000, input_tokens=1000, cache_read=5000,
                 output_tokens=800, duration_ms=4000, tool_calls=2),
        TurnStat(1, ts=1_770_000_600_000, input_tokens=1200, cache_read=6000,
                 output_tokens=900, errors=1),
        TurnStat(2, ts=1_770_001_200_000, input_tokens=50_000, cache_write=6000,
                 cache_read=1000, output_tokens=5000),
    ]
    p = SessionTimelinePopup(root, r)
    root.update_idletasks()
    p._set_view("converge")
    root.update_idletasks()
    p._draw()
    assert len(p.canvas.find_all()) > 0
    assert p._retry_idx_spans  # 3 连 Edit → 横带（端点 turn3 钳到 stats 内）
    assert p._cum_net is not None and p._cum_net[-1] == 38  # +10 -2 +30
    assert p._cum_cost  # 空 model 走默认价表，成本仍有数
    assert p._spike_idx == 2  # 大户回合（50k input）
    # 悬浮 + 点击钻取（converge 视图追加字段）。event.x 取整——真实 Tk
    # 鼠标事件的 x 恒为 int，浮点会让 tooltip 的 wm_geometry 炸。
    x0, x1, i = p._bar_x[0]
    ev = type("E", (), {"x": int((x0 + x1) / 2), "y": 100})()
    p._on_motion(ev)
    p._on_click(ev)
    assert len(p._detail.winfo_children()) == 1
    # 降级：无逐回合 LOC 的源（非 Claude）净增线不画、不炸
    r2 = reports[1]
    r2.usage.turn_net_locs = []
    p2 = SessionTimelinePopup(root, r2)
    p2._set_view("converge")
    p2._draw()
    assert p2._cum_net is None
    # 往返切换（flat_button 销毁重建路径）
    p2._set_view("timeline")
    p2._draw()
    assert p2._view == "timeline"


def test_llm_timeline_button_and_interpret(root, reports, monkeypatch, tmp_path):
    """LLM 解读：按钮常驻；未配置点击提示零请求；成功后落盘并回调跳页签。"""
    from tcer.core import llm_client, llm_prefs, llm_reports
    from tcer.gui.popups import SessionTimelinePopup

    monkeypatch.setattr(llm_reports, "_path",
                        lambda: tmp_path / "llm_reports.json")
    r = reports[0]
    import tkinter.messagebox as mb
    called = []
    monkeypatch.setattr(llm_client, "chat",
                        lambda **kw: called.append(kw) or "解读内容")

    # 按钮常驻：未配置也在，点击 → showinfo 提示，零请求
    monkeypatch.setattr(llm_prefs, "enabled", lambda: False)
    p = SessionTimelinePopup(root, r)
    root.update_idletasks()
    assert p._llm_btn is not None
    infos = []
    monkeypatch.setattr(mb, "showinfo",
                        lambda *a, **k: infos.append(a) or "ok")
    p._on_llm_interpret()
    assert called == [] and infos

    monkeypatch.setattr(llm_prefs, "enabled", lambda: True)
    monkeypatch.setattr(llm_prefs, "scope", lambda: "dialog")
    monkeypatch.setattr(llm_prefs, "model", lambda: "test-model")
    monkeypatch.setattr(llm_prefs, "base_url", lambda: "http://localhost:1")
    monkeypatch.setattr(llm_prefs, "api_key", lambda: None)
    saved_ids = []
    p2 = SessionTimelinePopup(root, r, load_user_texts=lambda: ["用户意图消息"],
                              on_report_saved=saved_ids.append)
    root.update_idletasks()
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: False)
    p2._on_llm_interpret()
    assert called == [] and p2._llm_busy is False   # 拒绝确认 → 零请求

    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    p2._on_llm_interpret()   # 占位提示已 pack、busy 置位（也起了真 daemon 线程）
    assert p2._llm_busy is True
    # 同意路径：同步直调 worker（测试 root 无 mainloop，跨线程 after 注册不了，
    # _on_llm_interpret 的线程包装由生产 mainloop 保证；上面的 daemon 线程与本
    # 次调用都会进 called——故下方断言用 any）。
    p2._llm_work("dialog", p2._llm_derived())
    root.update()  # 处理 after 回调 → _llm_done 落盘 + 回调

    assert called and all(c["model"] == "test-model" for c in called)
    assert any("用户意图消息" in c["user"] for c in called)  # loader 在 worker 内生效
    assert p2._llm_busy is False   # _llm_done 复位
    # 报告已持久化 + controller 回调收到 id
    stored = llm_reports.load()
    assert stored and stored[0]["text"] == "解读内容"
    assert stored[0]["model"] == "test-model" and stored[0]["turns"] > 0
    assert saved_ids and stored[0]["id"] in saved_ids

    def _walk_labels(w):
        out = []
        for c in w.winfo_children():
            if isinstance(c, tk.Label):
                out.append(c.cget("text"))
            else:
                out.extend(_walk_labels(c))
        return out
    labels = _walk_labels(p2._llm_panel)
    assert any("已生成并保存" in t for t in labels)      # 成功提示（非全文）

    # dialogue 优先于用户消息采样（Claude 源完整对话时间线）
    p3 = SessionTimelinePopup(root, r,
                              load_user_texts=lambda: ["回退消息"],
                              load_dialogue=lambda: ["[用户] 完整对话意图"])
    p3._llm_work("dialog", p3._llm_derived())
    root.update()
    assert any("完整对话意图" in c["user"] for c in called)
    assert all("回退消息" not in c["user"] for c in called)


def test_llm_reports_view(root, monkeypatch, tmp_path):
    """LLM 报告页签：空状态、加载列表、选中阅读、删除。"""
    from tcer.core import llm_reports
    from tcer.gui.views import LlmReportsView

    monkeypatch.setattr(llm_reports, "_path",
                        lambda: tmp_path / "llm_reports.json")
    v = LlmReportsView(root)
    assert isinstance(v._paned_ref, tk.PanedWindow)
    assert v._sash_target == 330
    v.on_show()
    assert "暂无报告" in v._body_lbl.get("1.0", "end")

    llm_reports.append({"id": "r1", "created_at": 1_770_000_000_000,
                        "session_title": "会话A", "source": "claude",
                        "model": "claude-sonnet-5", "scope": "dialog",
                        "turns": 12, "net_loc": 340, "cost_display": "$1.20",
                        "text": "【业务意图还原】做一个工具。\n"
                                "这个会话的意图是构建 TCER 的图表功能，覆盖趋势\n"
                                "与散点两个视图。详见[设计文档](http://x)。\n\n"
                                "- 要点一：**反馈有效**\n"
                                "  - 二级嵌套要点：细化分析\n"
                                "- 要点二：用 `turn_stats` 对齐\n"
                                "> 引用：*收敛良好*\n"
                                "```python\n代码围栏\n```\n"
                                "## 小结\n"
                                "收尾。"})
    v.on_show()
    assert v._tree.exists("r1")
    v.select_report("r1")
    body = v._body_lbl.get("1.0", "end")
    # 段内硬换行合并成整段（跨显示行也连续：去掉渲染换行后是完整句子）
    flat = body.replace("\n", "")
    assert "覆盖趋势与散点两个视图" in flat
    assert "• 要点一：" in flat and "- " not in flat
    assert "◦ 二级嵌套要点：" in flat
    assert "小结" in flat and "##" not in flat
    # 行内 markdown 剥记号 + tag 生效
    assert "**" not in body and "`" not in body
    assert "详见设计文档。" in flat and "[" not in body and "http" not in body
    assert "▎ 引用：收敛良好" in flat          # 引用前缀 + 斜体剥星号
    assert "代码围栏" in flat and "```" not in body
    assert v._body_lbl.tag_ranges("md_bold")
    assert v._body_lbl.tag_ranges("md_italic")
    assert v._body_lbl.tag_ranges("md_mono")
    assert v._body_lbl.tag_ranges("md_head")
    assert v._body_lbl.tag_ranges("md_h2")
    assert v._body_lbl.tag_ranges("sub_list_item")
    assert v._body_lbl.tag_ranges("code_block")
    # 中英文边界补空格（word wrap 的断点提示）；闭标点前后不插
    assert "构建 TCER 的" in body or "构建 TCER" in flat
    assert "用 turn_stats 对齐" in body or "用 turn_stats" in flat
    assert "UMG、" not in body.replace("UMG 、", "UMG、")  # 标点前无空格污染
    pad = LlmReportsView._pad_cjk_ascii
    assert pad("包括UMG、") == "包括 UMG、"
    assert pad("用turn_stats对齐") == "用 turn_stats 对齐"
    assert pad("问题,请") == "问题,请"      # 半角标点边界不动
    # 多来源多类型支持测试（项目级、模型对比级、会话级）
    llm_reports.append({"id": "r_proj", "created_at": 1_770_000_100_000,
                        "kind": "project", "title": "TCER 项目全局解读",
                        "project_name": "TCER", "model": "deepseek-chat",
                        "scope": "metrics", "text": "1.【业务意图还原】全局评估项目健康度。\n"})
    long_title = "Claude vs DeepSeek 对比分析完整的图表呈现与多视图联动模块"
    llm_reports.append({"id": "r_comp", "created_at": 1_770_000_200_000,
                        "kind": "compare", "title": long_title,
                        "model": "claude-3-7-sonnet", "scope": "full",
                        "text": "对比分析两个模型的产出与收敛。"})
    llm_reports.append({"id": "r_dyn", "created_at": 1_770_000_300_000,
                        "kind": "dynamics", "title": "会话A · 相空间分析",
                        "model": "deepseek-reasoner", "scope": "full",
                        "text": "1.【初始意图降熵评估】质点向狄拉克核心收敛。",
                        "dynamics_data": {
                            "capabilities": {"intent_formalization": 92, "drift_sensitivity": 80, "feedback_mutual_info": 85},
                            "trajectory": [{"turn": 1, "semantic_distance": 0.8, "vector": "positive"}],
                        }})
    v.on_show()
    assert len(v._tree.get_children()) == 4
    # 验证三列布局（去掉模型，由标题和时间占据）且长标题完整保留
    assert tuple(v._tree.cget("columns")) == ("kind", "title", "time")
    assert v._tree.column("time", "width") == 90
    assert str(v._tree.column("time", "anchor")) == "center"
    comp_vals = v._tree.item("r_comp", "values")
    assert len(comp_vals) == 3
    assert comp_vals[1] == long_title
    assert "…" not in comp_vals[1]

    # 类型胶囊过滤
    v._set_kind_filter("project")
    assert len(v._tree.get_children()) == 1
    v.select_report("r_proj")
    assert "全局评估项目健康度" in v._body_lbl.get("1.0", "end")
    assert v._body_lbl.tag_ranges("sec_head")
    assert not v._phase_portrait.container.winfo_manager()  # 普通报告隐藏相图

    # 动力学报告：相图挂载与三能力徽标
    v._set_kind_filter("dynamics")
    assert len(v._tree.get_children()) == 1
    v.select_report("r_dyn")
    assert "意图降熵力 92" in v._phase_portrait.cap_lbl.cget("text")
    assert v._phase_portrait.container.winfo_manager() == "pack"
    # 丰富化相图图元与真实回合映射验证
    dyn_rich = {
        "convergence_type": "dirac",
        "capabilities": {"intent_formalization": 88, "drift_sensitivity": 75, "feedback_mutual_info": 80},
        "trajectory": [
            {"turn": 1, "semantic_distance": 0.85, "vector": "positive", "event": "normal"},
            {"turn": 4, "semantic_distance": 0.70, "vector": "positive", "event": "breakthrough"},
            {"turn": 8, "semantic_distance": 0.78, "vector": "negative", "event": "retry_loop"},
            {"turn": 12, "semantic_distance": 0.35, "vector": "positive", "event": "compaction"},
            {"turn": 15, "semantic_distance": 0.05, "vector": "positive", "event": "normal"},
        ]
    }
    v._phase_portrait.render(dyn_rich, {"cost_display": "$2.50", "turns": 15})
    v._phase_portrait._redraw()
    assert len(v._phase_portrait._pts) == 5
    # 验证真实物理回合严格自底向上推进：T1 的 y 显著大于 T15 的 y（底到顶）
    assert v._phase_portrait._pts[0][1] > v._phase_portrait._pts[-1][1]

    # 验证末端突破时态势徽标自洽转为「吸引子逃逸 / 向心突破」
    dyn_escaped = {
        "convergence_type": "trapped",
        "attractor_trapped": True,
        "capabilities": {"intent_formalization": 30, "drift_sensitivity": 35, "feedback_mutual_info": 45},
        "trajectory": [
            {"turn": 1, "user_turn": 1, "semantic_distance": 0.85, "vector": "neutral"},
            {"turn": 200, "user_turn": 1, "semantic_distance": 0.82, "vector": "trapped", "event": "retry_loop"},
            {"turn": 500, "user_turn": 2, "semantic_distance": 0.35, "vector": "positive", "event": "breakthrough"},
        ]
    }
    v._phase_portrait.render(dyn_escaped, {"cost_display": "$10.00", "turns": 500})
    assert "吸引子逃逸 / 向心突破" in v._phase_portrait.state_badge.cget("text")

    # 验证鼠标移动到质点文本标签位置时依然能灵敏触发 Tooltip
    pt_t200 = v._phase_portrait._pts[1]
    px, py, _, offset_y = pt_t200
    class FakeEvent:
        def __init__(self, x, y):
            self.x = int(round(x))
            self.y = int(round(y))
    v._phase_portrait._on_motion(FakeEvent(px, py + offset_y))
    assert v._phase_portrait._tooltip._win is not None
    assert "用户消息 U1" in v._phase_portrait._tooltip._sig[0][0]
    v._phase_portrait._tooltip.hide()
    # 验证鼠标移动到狄拉克目标点位置时触发目标点专属释义 Tooltip
    tgt_x, tgt_y = v._phase_portrait._tgt_pos
    v._phase_portrait._on_motion(FakeEvent(tgt_x, tgt_y))
    assert v._phase_portrait._tooltip._win is not None
    assert "狄拉克目标点" in v._phase_portrait._tooltip._sig[0][0]
    v._phase_portrait._tooltip.hide()

    # 验证 P1 相速度对偶极限环模式切换与悬停相速度解析
    v._phase_portrait._set_mode("phase_plane")
    assert v._phase_portrait._view_mode == "phase_plane"
    assert len(v._phase_portrait._pts) == 3
    # 验证节点相速度数据存在且悬停能触发速度描述
    new_px, new_py, _, new_off, new_v = v._phase_portrait._pts[1]
    v._phase_portrait._on_motion(FakeEvent(new_px, new_py + new_off))
    assert v._phase_portrait._tooltip._win is not None
    v._phase_portrait._tooltip.hide()
    # 切回时序流形
    v._phase_portrait._set_mode("manifold")
    assert v._phase_portrait._view_mode == "manifold"

    v._set_kind_filter("all")
    v._search_var.set("DeepSeek 对比")
    v._on_search_changed()
    assert len(v._tree.get_children()) == 1
    v._search_var.set("")
    v._on_search_changed()
    assert len(v._tree.get_children()) == 4

    # 复制全文功能测试
    v.select_report("r_proj")
    v._copy_markdown()
    assert v._copy_btn.cget("text") == "已复制 ✓"
    import tkinter.messagebox as mb
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    v._delete_selected()   # 二次确认（mock 放行）
    assert len(v._tree.get_children()) == 3
    v._clear_all()
    assert not v._tree.get_children()

def test_llm_config_popup(root, monkeypatch, tmp_path):
    """LLM 设置弹窗：构建/状态行/半填校验零保存/测试连接入口校验。"""
    from tcer.core import llm_prefs
    from tcer.gui.popups import LlmConfigPopup

    monkeypatch.setattr(llm_prefs, "_prefs_path",
                        lambda: tmp_path / "tcer_llm.json")
    saved = []
    p = LlmConfigPopup(root, config={}, on_save=lambda **kw: saved.append(kw))
    root.update_idletasks()
    p.set_status("正常")
    p.set_status("出错", error=True)

    p._url_var.set("")          # 半填（url 空、model 有）→ 校验失败零保存
    p._model_var.set("m")
    p._do_save()
    assert saved == []
    p._do_test()                # 空 url → 状态行报错，零线程零请求
    assert p._status.cget("text") == "请先填写服务地址与模型"

    p._url_var.set("http://x")  # 齐填 → on_save 收到全部字段
    p._do_save()
    assert saved and saved[0]["base_url"] == "http://x"
    assert saved[0]["scopes"] == ["metrics", "dialog", "tools"]  # 验证默认全部勾选
    assert saved[0]["scope"] == "full"
    # 测试多选取消勾选联动
    p._scope_vars["dialog"].set(False)
    p._on_scope_changed()
    p._do_save()
    assert saved[1]["scopes"] == ["metrics", "tools"]
    assert "对话" not in saved[1]["scope"]

    # 验证 TcerGui._save_llm_config 接收 **cfg 正常工作且保存 scopes
    from tcer.gui.app import TcerGui
    from tcer.core import llm_prefs
    dummy = object.__new__(TcerGui)
    dummy._save_llm_config(**saved[1])
    assert llm_prefs.scopes() == ["metrics", "tools"]


def test_app_session_llm_analysis_dispatch(root, reports, monkeypatch, tmp_path):
    """验证从右键菜单直接调用控制器派发 LLM 任务（自持独立线程池，无 AttributeError）。"""
    import time
    from tcer.core import llm_prefs, llm_reports, llm_client
    from tcer.gui.app import TcerGui

    monkeypatch.setattr(llm_prefs, "_prefs_path", lambda: tmp_path / "tcer_llm.json")
    monkeypatch.setattr(llm_reports, "_path", lambda: tmp_path / "llm_reports.json")
    llm_prefs.save({"base_url": "http://mock", "model": "mock-llm", "api_key": "k"})

    chat_calls = []

    def mock_chat(**kw):
        chat_calls.append(kw)
        return "1.【业务意图还原】测试会话。\n\n```json\n{\"convergence_type\": \"dirac\"}\n```"

    monkeypatch.setattr(llm_client, "chat", mock_chat)

    app = TcerGui(root)
    r = reports[0]

    # 直接发起常规解读与动力学分析（验证多任务并发且不会抛 AttributeError）
    app.run_session_llm_interpret(r)
    app.run_session_dynamics_analysis(r)

    # 等待后台 worker 线程完成
    deadline = time.time() + 3.0
    while time.time() < deadline and len(chat_calls) < 2:
        root.update()
        time.sleep(0.02)

    assert len(chat_calls) == 2
    assert "mock-llm" in chat_calls[0]["model"]

def test_project_profile_popup(root, reports):
    """项目画像弹窗：热点文件/模型混用/技能 MCP 三节渲染不炸。"""
    from types import SimpleNamespace

    from tcer.gui.popups import ProjectProfilePopup

    fake = SimpleNamespace(n_sessions=len(reports), reports=reports)
    ProjectProfilePopup(root, fake)
    root.update_idletasks()


def test_dashboard_custom_metric_persists(root, reports, tmp_path, monkeypatch):
    """仪表盘右键换指标：写入 ui_prefs 并在重建后恢复。"""
    from tcer.core import file_cache, ui_prefs
    from tcer.gui import charts

    file_cache.clear()
    # 隔离 prefs 落盘：app_dirs 不认 TCER_HOME，直接替换 _prefs_path 指向
    # tmp_path，绝不触碰真实 ~/.tcer/tcer_ui.json。
    monkeypatch.setattr(ui_prefs, "_prefs_path",
                        lambda: tmp_path / "tcer_ui.json")
    ui_prefs.save({})
    frame = tk.Frame(root)
    frame.pack()
    tc = charts.DashboardChart(frame)
    tc.update(reports)
    assert tc._metrics[0] == "turns"  # 默认
    tc._pick_metric(0, "tcer")
    tc._draw()
    assert tc._metrics[0] == "tcer"
    # 重建恢复
    tc2 = charts.DashboardChart(frame)
    assert tc2._metrics[0] == "tcer"
    ui_prefs.save({})
    frame.destroy()
    file_cache.clear()




def test_session_column_batched_build_and_pending_select(root):
    """分批构建：首批同步可见、后续批经主循环补齐；构建期选中请求建完补发。"""
    from types import SimpleNamespace

    from tcer.gui.views import SessionColumn

    class _Ctl:
        def __getattr__(self, name):
            return lambda *a, **k: None

    def mk(i):
        return SimpleNamespace(
            meta=SimpleNamespace(session_id=f"sess-{i:03d}", title=f"标题{i}",
                                 path=SimpleNamespace(stem=f"s{i}"),
                                 is_subagent=False),
            usage=SimpleNamespace(started_at=1_770_000_000_000 + i * 3600_000,
                                  ended_at=None, models=set(),
                                  per_model={"m": SimpleNamespace(total=1)},
                                  session_duration_ms=60_000),
            tier=None, cost=0.0,
            files_touched_details=None, high_churn_details=None)

    frame = tk.Frame(root)
    frame.pack()
    sc = SessionColumn(frame, _Ctl())
    sc.update([mk(i) for i in range(60)])
    assert 0 < len(sc._all_cards) <= 25          # 首批同步
    # 构建未完成时请求选中 → 挂起
    assert sc.select_by_sid("sess-000", notify=False) is False  # 最旧 → 末批
    for _ in range(200):                          # 驱动主循环补完剩余批
        root.update()
        if not getattr(sc, "_pending_reports", None):
            break
    assert len(sc._all_cards) == 60
    assert sc._selected is not None               # 延迟选中已补发
    frame.destroy()


def test_project_card_shows_drive_letter(root):
    """项目卡片盘符标识：跨盘同名项目靠盘符区分（名字本身被剥掉盘符）。"""
    from tcer.gui.views import ProjectColumn, project_drive

    class _Ctl:
        def __getattr__(self, name):
            return lambda *a, **k: None

    class _P:
        def __init__(self, key):
            self.key = key
            self.source = "claude"
            self.name = key
            self.path = None

    frame = tk.Frame(root)
    frame.pack()
    col = ProjectColumn(frame, _Ctl())
    col.update([_P("d--GitHub-TCER"), _P("c--GitHub-TCER")])
    root.update_idletasks()
    assert project_drive(_P("d--GitHub-TCER")) == "D"
    assert project_drive(_P("c--GitHub-TCER")) == "C"

    def labels_of(card):
        out = []
        for w in card.frame.winfo_children():
            for c in w.winfo_children():
                if isinstance(c, tk.Label) and c.cget("text"):
                    out.append(c.cget("text"))
        return out

    assert any(t == "D:" for t in labels_of(col._cards[0]))
    assert any(t == "C:" for t in labels_of(col._cards[1]))
    frame.destroy()


def test_clamp_geometry_cross_resolution():
    """跨分辨率/跨机器迁移：恢复的窗口几何须钳进当前屏幕。"""
    from tcer.gui.app import clamp_geometry as cg
    # 1920×1080 存的窗口搬到 1366×768：尺寸收进屏幕
    assert cg("1600x900+169+40", 1366, 768) == "1366x708+169+40"
    # 多显示器拔掉：+3000 落在屏外 → 拉回可视区（标题栏可拖）
    out = cg("1600x900+3000+200", 1920, 1080)
    assert out.startswith("1600x900+") and "+3000" not in out
    # 负偏移（副屏在左）拉回 0
    assert cg("1600x900+-500+-100", 1920, 1080) == "1600x900+0+0"
    # 合法几何原样保留
    assert cg("1555x904+169+40", 1920, 1080) == "1555x904+169+40"
    # 非法输入 → None（走默认居中）
    assert cg("garbage", 1920, 1080) is None
    assert cg("", 1920, 1080) is None


def test_cross_source_models_popup(root):
    from tcer.gui.popups import CrossSourceModelsPopup

    models = [{
        "model": "zz-unlisted-model", "label": "Zz Unlisted", "tokens": 42_000,
        "sources": [
            {"source": "claude", "n": 6, "tcer": 13.9, "cpe": 380.7, "pbar": 5.06,
             "chr": 0.0, "score": 48.7, "net_loc": 120.0, "tools_per_100loc": 40.0},
            {"source": "omp", "n": 19, "tcer": 17.1, "cpe": 77.6, "pbar": 1.14,
             "chr": 0.91, "score": 49.4, "net_loc": 300.0, "tools_per_100loc": None},
        ],
    }]
    CrossSourceModelsPopup(root, models)
    root.update_idletasks()
    # 空数据走「未找到组合」分支，不崩。
    CrossSourceModelsPopup(root, [])
    root.update_idletasks()


def test_real_projects_view(root):
    from tcer.gui.views import RealProjectsView

    scanned = []

    class _Ctl:
        def real_projects_scan(self, *, force=False):
            scanned.append(force)

    v = RealProjectsView(root, controller=_Ctl())
    root.update_idletasks()
    v.on_show()                      # 首次切入 → 触发扫描
    assert scanned == [False]
    rows = [{
        "key": r"c:\github\tcer", "display": "C:\\GitHub\\TCER",
        "totals": {"n": 30, "tokens": 2_000_000_000, "cost": 12.5, "net": 4000,
                   "tcer": 2.0, "cpe": 3.1, "chr": 0.9, "score": 55.0, "tier": "中等"},
        "refs": [
            {"label": "Claude（.claude）", "source": "claude", "icon": "claude",
             "n": 10, "requests": 120, "user_msgs": 30,
             "tokens": 1_500_000_000, "cost": 5.0, "net": 1500,
             "tcer": 1.5, "cpe": 3.3, "chr": 0.95, "score": 54.0, "tier": "中等"},
            {"label": "Grok", "source": "grok", "icon": "grok", "n": 20,
             "requests": 210, "user_msgs": 45, "tokens": 500_000_000,
             "cost": 7.5, "net": 2500, "tcer": 2.5,
             "cpe": 3.0, "chr": 0.8, "score": 56.0, "tier": "良好"},
        ],
    }]
    v.set_rows(rows)
    root.update_idletasks()
    v.set_sort("tcer")               # 排序菜单命令不崩（数值列）
    v.set_sort("display")            # 名称列（字母序分支）
    v._expanded.add(rows[0]["key"])  # 展开态渲染各源明细行（含图标）
    v._render()
    root.update_idletasks()
    assert v._tok(1_500_000_000) == "15.00亿"   # 大数中文量级
    assert v._tok(999) == "999"                 # 不足万位回落千分位
    v.set_rows([])                   # 空数据走占位分支
    root.update_idletasks()
