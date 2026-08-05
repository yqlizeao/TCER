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
