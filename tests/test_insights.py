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


def test_ordering_good_then_drag_then_tip():
    r = _report(600, code_added=650, code_reworked=300)
    kinds = [i.kind for i in session_insights(r)]
    order = {"good": 0, "drag": 1, "tip": 2}
    ranks = [order[k] for k in kinds]
    assert ranks == sorted(ranks), kinds


def test_all_insights_are_wellformed():
    r = _report(600, code_added=650, code_reworked=300)
    for i in session_insights(r):
        assert isinstance(i, Insight)
        assert i.kind in ("good", "drag", "tip")
        assert i.title and i.evidence
        if i.kind == "drag":
            assert i.action  # every drag must be actionable

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
