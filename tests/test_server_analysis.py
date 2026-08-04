"""Tests for the server Decision Lab (stratified cohort comparison + advice).

The statistics are the product here, so these tests pin down the properties
that make the output trustworthy rather than just exercising the code paths:

- a **planted** effect is recovered, and a **planted confounder** is removed;
- small samples and zero-crossing intervals produce *no* claim;
- ratio aggregation is a ratio of sums, not a mean of ratios;
- CTEI never survives aggregation (``CLAUDE.md`` rule 9).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "server" / "backend"
sys.path.insert(0, str(_BACKEND))

analysis = pytest.importorskip("analysis")
insights = pytest.importorskip("insights")


# --------------------------------------------------------------------------- #
# Row factory
# --------------------------------------------------------------------------- #
def row(rid: int, *, tcer=None, model="m1", task="code_creation", tokens=100_000,
        cost=1.0, churn=0.1, tool_calls=None, tool_variants=None, **extra) -> dict:
    """A row shaped like ``db._fetch_rows`` output (dict of columns + canonicals)."""
    import json as _json
    d = {
        "id": rid, "tcer": tcer, "c_model": model, "model": model,
        "c_person": "p", "c_project": "proj",
        "task_type": task, "total_tokens": tokens, "cost_usd": cost,
        "net_loc": 100, "code_added": 120, "churn_ratio": churn,
        "chr": 0.8, "tool_error_rate": 0.05, "read_before_write": 0.7,
        "cpe": None, "session_duration_minutes": 10.0,
        "tool_calls_json": _json.dumps(tool_calls) if tool_calls else None,
        "tool_variants_json": _json.dumps(tool_variants) if tool_variants else None,
        "source": "claude", "reasoning_effort": None, "permission_profile": None,
        "approval_policy": None, "collaboration_mode": None, "cli_version": None,
    }
    d.update(extra)
    return d


# --------------------------------------------------------------------------- #
# Robust statistics
# --------------------------------------------------------------------------- #
def test_median_handles_both_parities():
    assert analysis.median([3, 1, 2]) == 2
    assert analysis.median([4, 1, 2, 3]) == 2.5
    assert analysis.median([]) is None


def test_winsorized_mean_caps_outlier_leverage():
    """One extreme value must not drag the center the way a plain mean does.

    Winsorizing bounds an outlier's leverage, it does not delete it — at n=10
    a 10% tail is a single point, so the reduction is an order of magnitude
    rather than total. That residual pull is exactly why the **median** is the
    headline statistic and the winsorized mean is only the cross-check.
    """
    vals = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1_000_000]
    plain = sum(vals) / len(vals)
    w = analysis.winsorized_mean(vals)
    assert plain > 99_000          # the mean is destroyed by the outlier
    assert w < plain / 5           # winsorizing pulls it back hard
    assert analysis.median(vals) == 1   # the headline stat is untouched


def test_describe_reports_spread_not_just_center():
    d = analysis.describe([1, 2, 3, 4, 5])
    assert d["n"] == 5 and d["median"] == 3
    assert d["p25"] == 2 and d["p75"] == 4
    assert d["min"] == 1 and d["max"] == 5


def test_bootstrap_is_deterministic():
    """Same data must give the same interval — a CI that changes on refresh is
    indistinguishable from a bug to anyone reading the dashboard."""
    a = {"s": [10.0, 12.0, 11.0, 13.0, 9.0]}
    b = {"s": [5.0, 6.0, 4.0, 7.0, 5.5]}
    first = analysis.compare_groups(a, b)
    second = analysis.compare_groups(a, b)
    assert first == second


# --------------------------------------------------------------------------- #
# Effect detection
# --------------------------------------------------------------------------- #
def test_detects_a_real_difference():
    a = {"s": [float(v) for v in range(20, 40)]}
    b = {"s": [float(v) for v in range(0, 20)]}
    r = analysis.compare_groups(a, b)
    assert r["diff"] == pytest.approx(20.0, abs=2)
    assert r["ci_low"] > 0                      # interval excludes zero
    assert analysis.grade(20, 20, r) == "strong"


def test_identical_groups_yield_no_claim():
    vals = [float(v) for v in range(20)]
    r = analysis.compare_groups({"s": list(vals)}, {"s": list(vals)})
    assert r["ci_low"] <= 0 <= r["ci_high"]
    assert analysis.grade(20, 20, r) == "none"


def test_small_cohort_is_insufficient_regardless_of_effect():
    """A huge apparent effect on 3 sessions is still not evidence."""
    r = analysis.compare_groups({"s": [100.0, 101.0, 99.0]}, {"s": [1.0, 2.0, 0.0]})
    assert analysis.grade(3, 3, r) == "insufficient"


def test_unstratifiable_data_falls_back_and_says_so():
    """One session per stratum can't support a within-stratum comparison."""
    a = {f"s{i}": [float(10 + i)] for i in range(12)}
    b = {f"s{i}": [float(i)] for i in range(12)}
    r = analysis.compare_groups(a, b)
    assert r["stratified"] is False
    assert r["diff"] is not None          # still answers, at a lower grade
    assert analysis.grade(12, 12, r) in ("moderate", "none")


# --------------------------------------------------------------------------- #
# The headline property: confounder removal
# --------------------------------------------------------------------------- #
def _confounded_rows() -> list[dict]:
    """Two models with identical *true* skill, but a skewed task mix.

    Task type triples the metric. Model ``fast`` draws mostly the easy task,
    ``slow`` mostly the hard one. Unstratified, ``fast`` looks far better; it
    isn't. Within any task type the two are identical.
    """
    rows = []
    rid = 0
    for model, easy_n, hard_n in (("fast", 18, 6), ("slow", 6, 18)):
        for _ in range(easy_n):
            rid += 1
            rows.append(row(rid, model=model, task="easy", tcer=300.0 + rid % 5))
        for _ in range(hard_n):
            rid += 1
            rows.append(row(rid, model=model, task="hard", tcer=100.0 + rid % 5))
    return rows


def test_stratification_removes_the_planted_confounder():
    rows = _confounded_rows()
    rep = analysis.compare(rows, "model", "tcer")
    fast = next(c for c in rep["cohorts"] if c["label"] == "fast")

    # A naive dashboard would report a large gap...
    assert fast["naive_diff"] > 100
    # ...but the stratified estimate collapses it, and the CI covers zero, so
    # the engine refuses to rank them.
    assert abs(fast["diff"]) < 20
    assert fast["ci_low"] <= 0 <= fast["ci_high"]
    assert fast["grade"] == "none"


def test_insights_emits_nothing_for_a_purely_confounded_difference():
    assert insights.generate(_confounded_rows())["findings"] == []


def test_real_effect_survives_stratification():
    """Same task mix, genuinely better model → the finding must come through."""
    rows = []
    rid = 0
    for model, bonus in (("good", 150.0), ("bad", 0.0)):
        for task in ("easy", "hard"):
            base = 300.0 if task == "easy" else 100.0
            for _ in range(12):
                rid += 1
                rows.append(row(rid, model=model, task=task, tcer=base + bonus + rid % 7))
    rep = analysis.compare(rows, "model", "tcer")
    good = next(c for c in rep["cohorts"] if c["label"] == "good")
    assert good["grade"] in ("strong", "moderate")
    assert good["diff"] > 100
    assert good["ci_low"] > 0
    assert rep["cohorts"][0]["label"] == "good"   # ranked first


# --------------------------------------------------------------------------- #
# Metric orientation
# --------------------------------------------------------------------------- #
def test_lower_is_better_metrics_flip_the_oriented_sign():
    """For cost, spending *more* must read as worse even though diff is positive."""
    rows = []
    rid = 0
    for model, cost in (("pricey", 5.0), ("cheap", 1.0)):
        for task in ("easy", "hard"):
            for _ in range(10):
                rid += 1
                rows.append(row(rid, model=model, task=task, cost=cost + rid % 3 * 0.1))
    rep = analysis.compare(rows, "model", "cost_usd")
    pricey = next(c for c in rep["cohorts"] if c["label"] == "pricey")
    assert pricey["diff"] > 0            # costs more
    assert pricey["diff_oriented"] < 0   # which is worse
    assert rep["higher_is_better"] is False


# --------------------------------------------------------------------------- #
# Multi-valued dimensions (Skill / MCP)
# --------------------------------------------------------------------------- #
def test_mcp_dimension_derives_servers_from_tool_names():
    r = row(1, tool_calls={"Read": 3, "mcp__zread__search_doc": 2,
                           "mcp__web-search__query": 1})
    assert analysis._mcp_servers(r) == ["web-search", "zread"]
    assert analysis._mcp_servers(row(2)) == []


def test_skill_dimension_reads_tool_variants():
    r = row(1, tool_variants={"Skill:dataviz": 2, "Agent:Explore": 1})
    assert analysis.DIMENSIONS["skill"].get(r) == ["dataviz"]
    assert analysis.DIMENSIONS["subagent"].get(r) == ["Explore"]


def test_addon_compared_against_sessions_that_did_not_use_it():
    rows = []
    rid = 0
    for used in (True, False):
        for task in ("easy", "hard"):
            base = 300.0 if task == "easy" else 100.0
            for _ in range(10):
                rid += 1
                rows.append(row(
                    rid, task=task, tcer=base + (140.0 if used else 0.0) + rid % 5,
                    tool_variants={"Skill:helper": 1} if used else None))
    rep = analysis.compare(rows, "skill", "tcer")
    assert rep["multi"] is True
    helper = next(c for c in rep["cohorts"] if c["label"] == "helper")
    assert helper["sessions"] == 20
    assert helper["contrast_stats"]["n"] == 20      # the not-used sessions
    assert helper["grade"] in ("strong", "moderate")
    assert helper["diff"] > 100


# --------------------------------------------------------------------------- #
# Guardrails and honesty
# --------------------------------------------------------------------------- #
def test_efficiency_win_with_a_quality_regression_is_flagged():
    """More output per token but much more rework must not read as a clean win."""
    rows = []
    rid = 0
    for fast in (True, False):
        for task in ("easy", "hard"):
            base = 300.0 if task == "easy" else 100.0
            for _ in range(12):
                rid += 1
                rows.append(row(
                    rid, model="quick" if fast else "steady", task=task,
                    tcer=base + (160.0 if fast else 0.0) + rid % 5,
                    churn=(0.45 if fast else 0.05) + (rid % 3) * 0.01))
    found = insights.generate(rows)["findings"]
    win = next(f for f in found if f["subject"] == "quick" and f["kind"] == "choice")
    assert win["caveats"], "质量护栏回退时必须给出提醒"
    assert "返工" in " ".join(win["caveats"])
    assert win["severity"] == "medium"   # downgraded from high


def test_too_few_rows_produce_coverage_notes_not_findings():
    res = insights.generate([row(i, tcer=100.0 + i) for i in range(4)])
    assert res["findings"] == []
    assert res["coverage"], "空结果必须解释原因"
    assert res["sessions_analyzed"] == 4


def test_multi_dimension_with_one_value_is_still_comparable():
    """used-vs-not-used needs only one value; the coverage note must not
    claim otherwise."""
    rows = [row(i, tool_variants={"Skill:only": 1} if i % 2 else None)
            for i in range(30)]
    notes = {n["dimension"]: n for n in insights._coverage_notes(rows)}
    assert "skill" not in notes


def test_every_report_carries_the_no_causation_caveat():
    rows = [row(i, tcer=100.0 + i) for i in range(20)]
    assert "因果" in analysis.compare(rows, "model", "tcer")["caveat"]
    assert "因果" in insights.generate(rows)["caveat"]


# --------------------------------------------------------------------------- #
# Aggregation correctness (db layer)
# --------------------------------------------------------------------------- #
def test_aggregate_ratios_are_ratios_of_sums_not_means_of_ratios():
    """Simpson's paradox guard: a tiny session must not outvote a huge one."""
    db = pytest.importorskip("db")
    big = {"total_tokens": 1_000_000, "input_tokens": 100_000, "output_tokens": 100_000,
           "cache_write_tokens": 100_000, "cache_read_tokens": 700_000,
           "net_loc": 1000, "cost_usd": 10.0, "code_added": 1000, "churn_ratio": 0.10,
           "tool_call_count": 100, "tool_error_count": 5, "chr": 0.7,
           "tool_error_rate": 0.05, "read_before_write": 0.5, "search_edit_ratio": 0.5}
    small = dict(big, total_tokens=1000, input_tokens=100, output_tokens=100,
                 cache_write_tokens=100, cache_read_tokens=700, net_loc=1,
                 cost_usd=0.01, code_added=10, churn_ratio=0.90,
                 tool_call_count=1, tool_error_count=1, tool_error_rate=1.0)
    agg = db._agg_metrics([big, small])
    # Mean of the two churn ratios would be 0.50; the correct answer is
    # (0.10*1000 + 0.90*10) / 1010 ≈ 0.108.
    assert agg["churn_ratio"] == pytest.approx(0.1079, abs=0.001)
    assert agg["tool_error_rate"] == pytest.approx(6 / 101, abs=0.001)
    assert agg["_stat"]["churn_ratio"] == "weighted"


def test_aggregate_score_is_recomputed_not_averaged():
    """综合效率分聚合有效——但只在从聚合自身的轴输入重算时（tcer 代理产出轴 +
    聚合 cpe/churn/tool_error/read_before_write），匹配桌面 audit 的
    ``aggregate_score_recompute``。对各会话 score 取平均是同类错误。"""
    db = pytest.importorskip("db")
    metrics = pytest.importorskip("tcer.core.metrics")

    def mk(tok, net, cost, score):
        return {"total_tokens": tok, "net_loc": net, "cost_usd": cost, "score": score,
                "input_tokens": tok // 10, "output_tokens": tok // 10,
                "cache_write_tokens": tok // 10, "cache_read_tokens": tok - 3 * (tok // 10),
                "code_added": net, "churn_ratio": 0.1, "chr": 0.7,
                "tool_call_count": 1, "tool_error_count": 0, "tool_error_rate": 0.0,
                "read_before_write": 0.5, "search_edit_ratio": 0.5}

    # Per-session score values are deliberately absurd (999 / 0) so an average
    # would be nowhere near the recomputed answer.
    agg = db._agg_metrics([mk(1_000_000, 1000, 10.0, 999.0), mk(1000, 1, 0.01, 0.0)])
    expected = metrics.efficiency_score(
        agg["tcer"], agg["cpe"], agg["churn_ratio"], agg["tool_error_rate"],
        agg["read_before_write"], net_loc=agg["net_loc"])
    assert agg["score"] == pytest.approx(round(expected, 2))
    assert agg["tier"] == metrics.tier(agg["score"])
    assert agg["_stat"]["score"] == "recomputed (tcer-proxy)"
    assert "score" in db._METRICS
