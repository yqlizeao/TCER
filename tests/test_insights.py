"""Tests for the offline session insight engine (tcer.core.insights)."""
from __future__ import annotations

from pathlib import Path

from tcer.core import metrics
from tcer.core.insights import Insight, session_insights
from tcer.core.models import ModelUsage, SessionMeta, TokenUsage


def _report(net_loc, *, code_added, code_reworked, tool_errors=0,
            tools=None, cache_read=700_000):
    meta = SessionMeta(session_id="s", cwd="/tmp", title="t",
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=200_000, output_tokens=100_000,
                   cache_read_input_tokens=cache_read,
                   models={"claude-opus-4-8"},
                   tool_calls=tools if tools is not None else {"Read": 8, "Edit": 6, "Bash": 4})
    u.tool_errors = tool_errors
    u.per_model = {"claude-opus-4-8": ModelUsage(
        input_tokens=200_000, cache_read_input_tokens=cache_read, output_tokens=100_000)}
    return metrics.compute(meta, u, net_loc=net_loc, task_type="code_creation",
                           code_added=code_added, code_reworked=code_reworked)


def test_unscored_session_returns_single_tip():
    meta = SessionMeta(session_id="x", cwd="/tmp", title="t",
                       path=Path("/tmp/x.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    out = session_insights(r)
    assert len(out) == 1
    assert out[0].kind == "tip"
    assert out[0].metric == "score"


def test_high_churn_produces_actionable_drag():
    r = _report(600, code_added=650, code_reworked=300)  # churn ~46%
    out = session_insights(r)
    churn = [i for i in out if i.metric == "churn"]
    assert churn and churn[0].kind == "drag"
    assert churn[0].action  # drag must carry a concrete next step
    assert "%" in churn[0].evidence  # evidence is grounded in a real number


def test_low_churn_produces_good_no_action():
    r = _report(600, code_added=650, code_reworked=5)  # churn <1%
    out = session_insights(r)
    churn = [i for i in out if i.metric == "churn"]
    assert churn and churn[0].kind == "good"
    assert churn[0].action == ""  # praise needs no action


def test_tool_error_needs_min_samples():
    # 3 tool calls, 2 errors = 67% error but below TOOL_MIN_CALLS -> no err insight
    r = _report(600, code_added=620, code_reworked=20,
                tool_errors=2, tools={"Bash": 3})
    assert not [i for i in session_insights(r) if i.metric == "tool_error_rate"]
    # 20 calls, 6 errors = 30% -> drag surfaces
    r2 = _report(600, code_added=620, code_reworked=20,
                 tool_errors=6, tools={"Bash": 20})
    errs = [i for i in session_insights(r2) if i.metric == "tool_error_rate"]
    assert errs and errs[0].kind == "drag"


def test_unseen_writes_flagged():
    r = _report(600, code_added=620, code_reworked=20)
    r.unseen_writes = 5
    out = session_insights(r)
    unseen = [i for i in out if i.metric == "unseen_writes"]
    assert unseen and unseen[0].kind == "drag" and unseen[0].action


def test_churn_evidence_names_top_file():
    """高返工洞察带上「改得最多的文件 + 次数」的具体证据。"""
    r = _report(600, code_added=650, code_reworked=300)
    r.high_churn_details = {"/proj/src/views.py": 5, "/proj/a.py": 2}
    ev = [i for i in session_insights(r) if i.metric == "churn"][0].evidence
    assert "views.py" in ev and "5 次" in ev


def test_tool_error_evidence_names_top_tool():
    """工具错误洞察带上「出错最多的工具 + 次数」的具体证据。"""
    r = _report(600, code_added=620, code_reworked=20,
                tool_errors=6, tools={"Bash": 20})
    r.usage.tool_errors_by_tool = {"Bash": 5, "Edit": 1}
    ev = [i for i in session_insights(r) if i.metric == "tool_error_rate"][0].evidence
    assert "Bash" in ev and "5 次" in ev


def test_project_tool_failure_aggregation():
    """项目级：跨会话工具失败集中在某工具时，产出带工具名+次数的具体摩擦。"""
    from tcer.core.insights import project_insights
    reports = []
    for i in range(3):
        r = _report(600, code_added=650, code_reworked=30)
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        r.usage.tool_errors_by_tool = {"Bash": 8, "Edit": 1}
        reports.append(r)
    hits = [i for i in project_insights(reports)
            if "集中在 Bash" in i.title]
    assert hits and hits[0].kind == "drag" and hits[0].action
    assert "Bash" in hits[0].evidence and "%" in hits[0].evidence


def test_activity_overview_aggregates_deterministically():
    from tcer.core.insights import activity_overview
    reports = []
    for i, nl in enumerate((50, 300, 1500)):
        r = _report(nl, code_added=nl + 20, code_reworked=10,
                    tools={"Bash": 5, "Edit": 3})
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        reports.append(r)
    ov = activity_overview(reports)
    assert ov.n_sessions == 3
    assert ov.total_tool_calls == (5 + 3) * 3
    # 工具聚合正确
    tools = dict(ov.top_tools)
    assert tools["Bash"] == 15 and tools["Edit"] == 9
    # 规模分档覆盖三档
    labels = [k for k, _ in ov.size_dist]
    assert any("小" in l for l in labels) and any("大" in l for l in labels)


def test_claude_md_suggestion_from_systemic_drag():
    from tcer.core.insights import claude_md_suggestions
    reports = []
    for i in range(3):
        r = _report(600, code_added=650, code_reworked=350)  # 高返工 → 系统性 churn
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        reports.append(r)
    sugg = claude_md_suggestions(reports)
    churn_rule = [s for s in sugg if s.metric == "churn"]
    assert churn_rule, "系统性返工应生成 CLAUDE.md 规则"
    assert churn_rule[0].rule and "3/3" in churn_rule[0].evidence


def test_claude_md_suggestions_empty_without_systemic_drag():
    from tcer.core.insights import claude_md_suggestions
    # 单会话 → 无跨会话系统性信号
    r = _report(600, code_added=650, code_reworked=5)
    assert claude_md_suggestions([r]) == []


def test_churn_severity_graded_copy():
    """工文案按严重度分级：三个档位给不同 title（不再一句呆板文案）。"""
    from tcer.core.insights import _churn_copy
    mild = _churn_copy(0.35)[0]
    mid = _churn_copy(0.50)[0]
    severe = _churn_copy(0.70)[0]
    assert mild != mid != severe and mild != severe
    assert "严重" in severe  # 最高档措辞更重


def test_claude_md_rule_is_structured_block():
    """CLAUDE.md 建议是结构化规则块（## 标题 + 多条），不是单句泛泛建议。"""
    from tcer.core.insights import claude_md_suggestions
    reports = []
    for i in range(3):
        r = _report(600, code_added=650, code_reworked=350)
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        reports.append(r)
    sugg = [s for s in claude_md_suggestions(reports) if s.metric == "churn"]
    assert sugg
    rule = sugg[0].rule
    assert rule.startswith("## ")           # markdown 小节标题
    assert rule.count("\n") >= 2            # 多条规则，非单句
    assert "-" in rule                       # 有条目符号


def test_horizon_has_expanded_scenarios():
    """horizon 覆盖扩充后的多场景：低测试→测试驱动、体量大→自审。"""
    from tcer.core.insights import horizon_suggestions
    reports = []
    for i in range(4):
        r = _report(1500, code_added=1600, code_reworked=60,
                    tool_errors=5, tools={"Bash": 10})
        r.test_loc_ratio = 0.0  # 触发测试驱动自动实现
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        reports.append(r)
    titles = [rc.title for rc in horizon_suggestions(reports)]
    assert any("测试驱动" in t for t in titles)
    assert any("自审" in t for t in titles)


def test_feature_suggestions_grounded_on_signals():
    from tcer.core.insights import feature_suggestions
    reports = []
    for i in range(6):
        r = _report(600, code_added=650, code_reworked=30,
                    tool_errors=3, tools={"Bash": 10, "Edit": 5})
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        reports.append(r)
    recos = feature_suggestions(reports)
    assert recos, "有摩擦信号应产出建议"
    # 每条建议证据非空、prompt 可粘贴
    for rc in recos:
        assert rc.title and rc.why
    # 会话数 >=5 触发「流程固化」建议
    assert any("清单" in rc.title for rc in recos)


def test_horizon_suggestions_grounded():
    from tcer.core.insights import horizon_suggestions
    reports = []
    for i in range(3):
        r = _report(1500, code_added=1600, code_reworked=50,
                    tool_errors=5, tools={"Bash": 10})
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        reports.append(r)
    recos = horizon_suggestions(reports)
    assert recos
    # 大会话 >=2 触发并行子代理建议
    assert any("并行" in rc.title for rc in recos)
    assert all(rc.prompt for rc in recos)  # horizon 都带可粘贴 prompt


def test_feature_horizon_empty_on_no_reports():
    from tcer.core.insights import feature_suggestions, horizon_suggestions
    assert feature_suggestions([]) == []
    assert horizon_suggestions([]) == []


def test_project_tool_failure_below_threshold_silent():
    """工具失败总量不够 或 分散时，不硬凑摩擦洞察。"""
    from tcer.core.insights import project_insights
    reports = []
    for i in range(3):
        r = _report(600, code_added=650, code_reworked=30)
        r.meta = SessionMeta(session_id=f"s{i}", cwd="/tmp", title="t",
                             path=Path(f"/tmp/s{i}.jsonl"), is_subagent=False)
        r.usage.tool_errors_by_tool = {"Bash": 1}  # 总量 3 < 阈值 10
        reports.append(r)
    assert not [i for i in project_insights(reports) if "集中在" in i.title]


def test_high_churn_files_flagged():
    r = _report(600, code_added=620, code_reworked=20)
    r.high_churn_file_count = 3
    hits = [i for i in session_insights(r) if i.metric == "high_churn_file_count"]
    assert hits and hits[0].kind == "drag" and hits[0].action
    assert "3" in hits[0].evidence


def test_edit_verify_low_and_high():
    r = _report(600, code_added=620, code_reworked=20)
    r.edit_verify_ratio = 0.05
    low = [i for i in session_insights(r) if i.metric == "edit_verify_ratio"]
    assert low and low[0].kind == "drag" and low[0].action
    r.edit_verify_ratio = 0.9
    high = [i for i in session_insights(r) if i.metric == "edit_verify_ratio"]
    assert high and high[0].kind == "good" and high[0].action == ""


def test_test_coverage_needs_scale():
    # below NET_LOC_MIN_FOR_TEST -> no test insight even if ratio is 0
    small = _report(50, code_added=60, code_reworked=5)
    small.test_loc_ratio = 0.0
    assert not [i for i in session_insights(small) if i.metric == "test_loc_ratio"]
    # at scale, near-zero test ratio -> tip
    big = _report(600, code_added=620, code_reworked=20)
    big.test_loc_ratio = 0.0
    low = [i for i in session_insights(big) if i.metric == "test_loc_ratio"]
    assert low and low[0].kind == "tip"
    # healthy test ratio -> good
    big.test_loc_ratio = 0.25
    good = [i for i in session_insights(big) if i.metric == "test_loc_ratio"]
    assert good and good[0].kind == "good"


def test_context_window_high_flagged():
    r = _report(600, code_added=620, code_reworked=20)
    r.context_window_used_ratio = 0.9
    hits = [i for i in session_insights(r) if i.metric == "context_window_used_ratio"]
    assert hits and hits[0].kind == "tip" and hits[0].action


def test_cache_ineffective_flagged():
    r = _report(600, code_added=620, code_reworked=20)
    r.cache_efficiency = 0.5  # reads < writes
    r.usage.cache_creation_input_tokens = 100_000
    hits = [i for i in session_insights(r) if i.metric == "cache_efficiency"]
    assert hits and hits[0].kind == "tip" and hits[0].action


def test_repeated_corrections_flagged():
    r = _report(600, code_added=620, code_reworked=20)
    r.usage.correction_msg_count = 4
    hits = [i for i in session_insights(r) if i.metric == "correction_msg_count"]
    assert hits and hits[0].kind == "tip" and hits[0].action
    assert "4" in hits[0].evidence


def test_ordering_good_then_drag_then_tip():
    r = _report(600, code_added=650, code_reworked=300)
    kinds = [i.kind for i in session_insights(r)]
    order = {"good": 0, "drag": 1, "cost": 2, "tip": 3}
    ranks = [order[k] for k in kinds]
    assert ranks == sorted(ranks), kinds


def test_all_insights_are_wellformed():
    r = _report(600, code_added=650, code_reworked=300)
    for i in session_insights(r):
        assert isinstance(i, Insight)
        assert i.kind in ("good", "drag", "cost", "tip")
        assert i.title and i.evidence
        if i.kind in ("drag", "cost"):
            assert i.action  # 拖累项与金额项都必须给可执行下一步


def test_cost_high_absolute_spend():
    r = _report(600, code_added=620, code_reworked=20)
    r.cost = 9.0
    hits = [i for i in session_insights(r) if i.kind == "cost" and i.metric == "cost"]
    assert hits and hits[0].action
    assert "9" in hits[0].evidence


def test_cost_cpe_above_baseline():
    from tcer.core import metrics
    r = _report(600, code_added=620, code_reworked=20)
    r.cpe = metrics.CPE_BASELINE * 2.0  # 2× 基准 -> 触发
    hits = [i for i in session_insights(r) if i.metric == "cpe"]
    assert hits and hits[0].kind == "cost" and hits[0].action


def test_cost_churn_waste():
    r = _report(600, code_added=650, code_reworked=300)  # churn ~46%
    hits = [i for i in session_insights(r) if i.metric == "cost_churn"]
    assert hits and hits[0].kind == "cost" and hits[0].action
    assert "%" in hits[0].evidence


def test_low_cost_session_has_no_cost_insights():
    r = _report(600, code_added=650, code_reworked=5)  # low churn
    r.cost = 0.5  # cheap
    # cpe below baseline (default fixture), no churn waste, no high spend
    assert not [i for i in session_insights(r) if i.kind == "cost"]

# --- cross-session (project-level) insights ---------------------------------
from tcer.core.insights import project_insights


def test_project_insights_empty_below_two_sessions():
    assert project_insights([]) == []
    r = _report(400, code_added=450, code_reworked=10)
    assert project_insights([r]) == []


def test_project_insights_surfaces_systemic_drag():
    # 3 of 4 sessions have high churn -> systemic drag with prevalence evidence.
    reports = [
        _report(300, code_added=350, code_reworked=180),
        _report(400, code_added=450, code_reworked=240),
        _report(350, code_added=400, code_reworked=200),
        _report(900, code_added=950, code_reworked=20),
    ]
    out = project_insights(reports)
    churn = [i for i in out if i.metric == "churn" and i.kind == "drag"]
    assert churn, "systemic churn drag should surface"
    assert churn[0].action, "systemic drag must carry an action"
    assert "/4" in churn[0].evidence, "evidence must show N/M prevalence"
    assert churn[0].title.startswith("系统性")


def test_project_insights_drag_before_good():
    reports = [
        _report(300, code_added=350, code_reworked=180),
        _report(400, code_added=450, code_reworked=240),
        _report(350, code_added=400, code_reworked=200),
    ]
    kinds = [i.kind for i in project_insights(reports)]
    if "drag" in kinds and "good" in kinds:
        assert kinds.index("drag") < kinds.index("good")


def test_project_insights_rare_drag_not_systemic():
    # Only 1 of 4 has high churn -> below 40% prevalence, not surfaced as systemic.
    reports = [
        _report(300, code_added=350, code_reworked=180),
        _report(900, code_added=950, code_reworked=20),
        _report(850, code_added=900, code_reworked=15),
        _report(800, code_added=850, code_reworked=10),
    ]
    out = project_insights(reports)
    churn_drag = [i for i in out if i.metric == "churn" and i.kind == "drag"]
    assert not churn_drag, "a 1/4 drag is not systemic"


def test_project_insights_wellformed():
    reports = [
        _report(300, code_added=350, code_reworked=180),
        _report(400, code_added=450, code_reworked=240),
    ]
    for i in project_insights(reports):
        assert i.kind in ("good", "drag", "tip")
        assert i.title and i.evidence
        if i.kind == "drag":
            assert i.action
