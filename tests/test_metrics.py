"""Tests for metrics.py — formula correctness and divide-by-zero safety."""
from __future__ import annotations

from pathlib import Path

import pytest

from tcer.core import metrics
from tcer.core.models import SessionMeta, ToolOp, TokenUsage

META = SessionMeta(session_id="s", cwd="/tmp", title=None,
                   path=Path("/tmp/s.jsonl"), is_subagent=False)

try:
    from pytest import approx as pytest_approx
except ImportError:  # pragma: no cover
    def pytest_approx(expected, rel=1e-6):
        class _A:
            def __eq__(self, other):
                return abs(other - expected) <= abs(rel * expected) + 1e-12
        return _A()


def _u(i=0, cw=0, cr=0, o=0) -> TokenUsage:
    return TokenUsage(input_tokens=i, cache_creation_input_tokens=cw,
                      cache_read_input_tokens=cr, output_tokens=o)


def test_cost_usd_list_price():
    # 1M input @ $3, 1M output @ $15, 1M cacheW @ $3.75, 1M cacheR @ $0.30
    u = _u(i=1_000_000, cw=1_000_000, cr=1_000_000, o=1_000_000)
    assert metrics.cost_usd(u) == pytest_approx(3.0 + 3.75 + 0.30 + 15.0)


def test_chr_formula():
    # cache_read / (input + cacheW + cacheR)
    u = _u(i=100, cw=300, cr=600, o=10)
    r = metrics.compute(META, u, net_loc=None)
    assert r.chr == pytest_approx(600 / 1000)


def test_io_ratio_formula():
    u = _u(i=10, cw=0, cr=0, o=5)
    r = metrics.compute(META, u, net_loc=None)
    assert r.io_ratio == pytest_approx(10 / 5)


def test_context_window_used_uses_peak_not_session_sum():
    """Multi-turn sessions must not report 50× window as 'utilization'."""
    u = _u(i=900, cw=0, cr=100, o=50)  # session-summed total_input = 1000
    u.model_context_window = 200
    u.peak_input_tokens = 150  # busiest single turn
    r = metrics.compute(META, u, net_loc=None)
    assert r.context_window_used_ratio == pytest_approx(150 / 200)
    # Without peak, ratio is unknown (not total_input/window)
    u2 = _u(i=900, o=50)
    u2.model_context_window = 200
    u2.peak_input_tokens = 0
    r2 = metrics.compute(META, u2, net_loc=None)
    assert r2.context_window_used_ratio is None


def test_tcer_and_cpe():
    # total = 1Mt tokens, net_loc = 500 → TCER = 500 LOC/Mt
    u = _u(i=500_000, o=500_000)  # total 1,000,000
    r = metrics.compute(META, u, net_loc=500)
    assert r.tcer == pytest_approx(500.0)
    # cost = 500k*3 + 500k*15 per 1e6 = 1.5 + 7.5 = 9.0 ; cpe = 9.0/500*1000 = 18
    assert r.cost == pytest_approx(9.0)
    assert r.cpe == pytest_approx(18.0)


def test_zero_input_yields_none_chr():
    r = metrics.compute(META, _u(o=10), net_loc=None)
    assert r.chr is None
    assert r.io_ratio == pytest_approx(0 / 10)


def test_zero_output_yields_none_io_ratio():
    r = metrics.compute(META, _u(i=10), net_loc=None)
    assert r.io_ratio is None


def test_no_loc_yields_none_tcer_cpe():
    r = metrics.compute(META, _u(i=10, o=10), net_loc=None)
    assert r.tcer is None
    assert r.cpe is None


def test_merge_sums_fields():
    a = _u(i=1, cw=2, cr=3, o=4)
    b = _u(i=10, cw=20, cr=30, o=40)
    m = a.merge(b)
    assert (m.input_tokens, m.cache_creation_input_tokens,
            m.cache_read_input_tokens, m.output_tokens) == (11, 22, 33, 44)


# --------------------------------------------------------------------------- #
# TokenUsage.merge() field guards
# --------------------------------------------------------------------------- #
def test_merge_tool_calls_summed():
    """tool_calls dicts should be merged (summed by tool name)."""
    a = _u(i=100, o=50)
    a.tool_calls = {"Read": 5, "Write": 3, "Edit": 2}

    b = _u(i=200, o=100)
    b.tool_calls = {"Read": 3, "Grep": 10, "Bash": 1}

    m = a.merge(b)
    assert m.tool_calls == {
        "Read": 8,    # 5 + 3
        "Write": 3,   # only in a
        "Edit": 2,    # only in a
        "Grep": 10,   # only in b
        "Bash": 1,    # only in b
    }


def test_merge_tool_ops_rebase_turn_numbers():
    """tool_ops should be merged with turn numbers rebased to continue after self."""
    a = _u(i=100, o=50)
    a.tool_ops = [
        ToolOp(0, "Read", "/a.py"),
        ToolOp(1, "Write", "/a.py"),
    ]

    b = _u(i=200, o=100)
    b.tool_ops = [
        ToolOp(0, "Read", "/b.py"),   # turn 0 in b
        ToolOp(1, "Edit", "/b.py"),   # turn 1 in b
    ]

    m = a.merge(b)

    # a's ops: turn 0, 1 (unchanged)
    assert m.tool_ops[0].turn == 0
    assert m.tool_ops[1].turn == 1

    # b's ops: rebased to turn 2, 3 (continue after a's max turn = 1)
    assert m.tool_ops[2].turn == 2
    assert m.tool_ops[3].turn == 3

    # Verify tools and paths preserved
    assert m.tool_ops[2].tool == "Read"
    assert m.tool_ops[2].path == "/b.py"


def test_merge_thinking_count():
    """thinking_count should be summed during merge."""
    a = _u(i=100, o=50)
    a.thinking_count = 3

    b = _u(i=200, o=100)
    b.thinking_count = 5

    m = a.merge(b)
    assert m.thinking_count == 8  # 3 + 5


def test_merge_user_message_texts():
    """user_message_texts should be concatenated during merge."""
    a = _u(i=100, o=50)
    a.user_message_texts = ["hello", "fix the bug"]

    b = _u(i=200, o=100)
    b.user_message_texts = ["add feature", "write tests"]

    m = a.merge(b)
    assert m.user_message_texts == ["hello", "fix the bug", "add feature", "write tests"]


# --------------------------------------------------------------------------- #
# Composite (G6): 综合效率分 v2 (三正交轴) / TTAF / TA-TCER / CAF / tier
# --------------------------------------------------------------------------- #
# Framework reference baselines (§6.3). Hardcoded here so axis tests stay valid
# even when a user has overwritten composite_baselines.json with personal ones.
_FW = {"tcer_baseline": 76.59, "cpe_baseline": 8.22}


def test_half_sat_axes_neutral_at_baseline():
    # 半饱和：x=基准 → 0.5（中性）；越高产出轴越高，越省成本轴越高。
    assert metrics.output_axis(76.59, 76.59) == pytest_approx(0.5)
    assert metrics.cost_axis(8.22, 8.22) == pytest_approx(0.5)
    assert metrics.output_axis(76.59 * 3, 76.59) > 0.5   # 高产 → >0.5
    assert metrics.cost_axis(8.22 / 3, 8.22) > 0.5        # 更省 → >0.5
    assert metrics.output_axis(None, 76.59) is None


def test_quality_axis_weighted_and_degrades():
    # 三子信号全在：低返工/低错误/高先读后写 → 高质量分。
    full = metrics.quality_axis(0.0, 0.0, 1.0)
    assert full == pytest_approx(1.0)
    # 全缺 → None（无质量信号）。
    assert metrics.quality_axis(None, None, None) is None
    # 部分缺失 → 权重重分配到可用子信号（不为 None）。
    assert metrics.quality_axis(0.2, None, None) is not None


def test_efficiency_score_bounded_and_needs_output():
    s = metrics.efficiency_score(80.0, 8.0, 0.05, 0.02, 0.8, net_loc=600, **_FW)
    assert s is not None and 0.0 <= s <= 100.0
    # 无产出轴（ntcer=None）→ 不评分。
    assert metrics.efficiency_score(None, 8.0, 0.05, 0.02, 0.8, net_loc=600) is None


def test_score_shrinks_small_sessions_toward_center():
    # 相同轴值，小会话被拉向中性 50，大会话保留高分。
    big = metrics.efficiency_score(300.0, 3.0, 0.0, 0.0, 0.9, net_loc=100_000, **_FW)
    tiny = metrics.efficiency_score(300.0, 3.0, 0.0, 0.0, 0.9, net_loc=5, **_FW)
    assert big > tiny
    assert abs(tiny - 50.0) < abs(big - 50.0)


def test_tier_thresholds():
    assert metrics.tier(80.0) == "优秀"
    assert metrics.tier(65.0) == "良好"
    assert metrics.tier(50.0) == "中等"
    assert metrics.tier(30.0) == "待改进"
    assert metrics.tier(10.0) == "低效"
    assert metrics.tier(None) is None


def test_ttaf_table_matches_report():
    # Report §6.4 authoritative values (now only 3 task categories).
    assert metrics.TTAF["code_creation"] == 1.00
    assert metrics.TTAF["code_maintenance"] == 0.45
    assert metrics.TTAF["non_coding"] == 0.20


def test_ta_tcer_debug_example():
    # Report §6.4 worked example: code_maintenance TCER=35.0 → NTCER = 35.0/0.45 ≈ 77.78.
    assert metrics.normalized_tcer(35.0, "code_maintenance") == pytest_approx(35.0 / 0.45)
    assert metrics.normalized_tcer(35.0, "code_creation") == pytest_approx(35.0)  # TTAF 1.0
    assert metrics.normalized_tcer(35.0, "unknown") is None  # unknown task type
    # Legacy alias used by older callers / tests
    assert metrics.normalized_tcer(35.0, "feature") == pytest_approx(35.0)
    assert metrics.resolve_task_type("feature") == "code_creation"
    assert metrics.coerce_task_type("unknown") is None


def test_tool_usage_metrics_counts_mcp_search_as_exploration():
    u = TokenUsage()
    u.tool_calls = {
        "Read": 1,
        "Grep": 1,
        "mcp__tavily__tavily_search": 2,
        "mcp__firecrawl__firecrawl_scrape": 1,
        "Edit": 1,
    }
    m = metrics.tool_usage_metrics(u)
    # Grep(1)+mcp search(2)+Glob(0)+Web(0) = 3 explore / 6 total
    assert m["exploration_ratio"] == pytest_approx(3 / 6)
    # Read(1)+mcp scrape(1) / (Edit+Write=1) = 2.0
    assert m["read_write_ratio"] == pytest_approx(2.0)


def test_tool_usage_metrics_bare_firecrawl_aliases():
    """Some sessions log firecrawl_* without the mcp__server__ prefix."""
    u = TokenUsage()
    u.tool_calls = {
        "Read": 1,
        "firecrawl_search": 3,
        "firecrawl_scrape": 2,
        "Write": 1,
    }
    m = metrics.tool_usage_metrics(u)
    # explore = firecrawl_search(3) / 7
    assert m["exploration_ratio"] == pytest_approx(3 / 7)
    # read = Read(1)+scrape(2)=3 / Write(1) = 3
    assert m["read_write_ratio"] == pytest_approx(3.0)


def test_tool_usage_metrics_no_midword_false_aliases():
    """Segment match only: GetTaskOutput≠get-read, ReportFindings≠find-explore."""
    u = TokenUsage()
    u.tool_calls = {
        "Read": 2,
        "Edit": 2,
        "GetTaskOutput": 19,  # live Grok — must not inflate read
        "ReportFindings": 10,  # live Claude skill — must not inflate explore
        "Grep": 1,
    }
    m = metrics.tool_usage_metrics(u)
    # explore = Grep only (1) / total 34
    assert m["exploration_ratio"] == pytest_approx(1 / 34)
    # read = Read(2) only / Edit(2) = 1.0  (GetTaskOutput ignored)
    assert m["read_write_ratio"] == pytest_approx(1.0)


def test_leaf_has_keyword_segments():
    assert metrics._leaf_has_keyword("firecrawl_search", "search")
    assert metrics._leaf_has_keyword("llm_wiki_read_file", "read")
    assert not metrics._leaf_has_keyword("gettaskoutput", "get")
    assert not metrics._leaf_has_keyword("reportfindings", "find")


def test_infer_task_type_creation_vs_non_coding():
    # High net / low exploration → creation
    assert metrics.infer_task_type(
        net_loc=500, total_tokens=1_000_000,
        exploration_ratio=0.05, edit_ratio=0.2, read_write_ratio=0.5,
    ) == "code_creation"
    # No net, heavy search → non_coding
    assert metrics.infer_task_type(
        net_loc=0, total_tokens=500_000,
        exploration_ratio=0.5, edit_ratio=None, read_write_ratio=8.0,
    ) == "non_coding"
    # Modest net + high edit share → maintenance
    assert metrics.infer_task_type(
        net_loc=40, total_tokens=1_000_000,
        exploration_ratio=0.25, edit_ratio=0.8, read_write_ratio=2.5,
    ) == "code_maintenance"


def test_infer_task_type_none_net_loc_does_not_force_non_coding():
    """LOC unknown (no_loc / no patch signal) must not score as zero output."""
    # Write-heavy tools, low exploration — should lean creation, not non_coding.
    assert metrics.infer_task_type(
        net_loc=None, total_tokens=1_000_000,
        exploration_ratio=0.05, edit_ratio=0.2, read_write_ratio=0.5,
    ) == "code_creation"
    # Explicit zero LOC still non_coding when search-heavy.
    assert metrics.infer_task_type(
        net_loc=0, total_tokens=500_000,
        exploration_ratio=0.5, edit_ratio=None, read_write_ratio=8.0,
    ) == "non_coding"


def test_majority_task_type():
    assert metrics.majority_task_type(
        ["code_maintenance", "code_maintenance", "code_creation"]
    ) == "code_maintenance"
    assert metrics.majority_task_type([]) == "code_creation"
    assert metrics.is_auto_task_type("auto") and metrics.is_auto_task_type("自动")
    assert not metrics.is_auto_task_type("code_creation")


@pytest.mark.parametrize("task_type,expected_factor", [
    ("code_creation", 1.0),
    ("code_maintenance", 0.45),
    ("non_coding", 0.2),
])
def test_ta_tcer_all_ttaf_types(task_type, expected_factor):
    """All TTAF-defined task types should produce correct NTCER."""
    tcer = 50.0
    result = metrics.normalized_tcer(tcer, task_type)
    assert result is not None, f"normalized_tcer returned None for task_type={task_type}"
    assert result == pytest_approx(tcer / expected_factor)


def test_ttaf_table_completeness():
    """TTAF table should contain all expected task types (3 categories)."""
    expected_types = {"code_creation", "code_maintenance", "non_coding"}
    actual_types = {k for k in metrics.TTAF.keys() if not k.startswith("_")}

    assert expected_types == actual_types, (
        f"TTAF table missing types: {expected_types - actual_types}, "
        f"or has unexpected types: {actual_types - expected_types}"
    )


def test_caf_formula():
    # CAF = TotalInput / (input + cache_write). Heavy cache reads → CAF >> 1.
    u = _u(i=100, cw=100, cr=800)  # total_input=1000, denom=200
    assert metrics.caf(u) == pytest_approx(1000 / 200)
    assert metrics.caf(_u(o=10)) is None  # no input/cache_write → undefined


def test_compute_populates_composite_fields():
    # End-to-end: compute() fills CAF / TA-TCER / 综合效率分（三轴，无需仓库扫描）。
    u = _u(i=400_000, cw=100_000, o=500_000)  # total 1,000,000
    r = metrics.compute(META, u, net_loc=500, task_type="code_maintenance")
    assert r.tcer == pytest_approx(500.0)
    assert r.ntcer == pytest_approx(500.0 / 0.45)
    assert r.ta_tcer == pytest_approx(500.0 / 0.45)  # backward compat
    assert r.caf == pytest_approx(500_000 / 500_000)  # total_input / (input+cacheW)
    assert r.score is not None and r.tier is not None
    assert r.score_output_axis is not None and r.score_cost_axis is not None
    assert r.task_type == "code_maintenance"
    assert r.task_category == "code_maintenance"
    assert r.ttaf == 0.45


def test_compute_composite_none_without_net_loc():
    # net_loc=None → TCER/CPE/综合效率分 皆 None，但 CAF（纯 token）仍有值。
    u = _u(i=400_000, cw=100_000, o=500_000)
    r = metrics.compute(META, u, net_loc=None, task_type="code_creation")
    assert r.tcer is None
    assert r.score is None
    assert r.caf is not None  # CAF needs only token usage
    assert r.task_type == "code_creation"
    assert r.task_category == "code_creation"
    assert r.ttaf == 1.0


def test_churn_ratio_formula():
    # churn = deleted / added.
    assert metrics.churn_ratio(1000, 200) == pytest_approx(0.20)
    assert metrics.churn_ratio(1000, 0) == pytest_approx(0.0)  # pure additions
    assert metrics.churn_ratio(0, 50) is None  # nothing added → undefined
    assert metrics.churn_ratio(None, None) is None


def test_compute_populates_churn():
    u = _u(i=500_000, o=500_000)
    r = metrics.compute(META, u, net_loc=800, code_added=1000, code_deleted=200)
    assert r.code_added == 1000
    assert r.code_deleted == 200
    assert r.churn_ratio == pytest_approx(0.20)


def _single_model_report(model, *, added, deleted, reworked=None,
                         net_loc=200, i=600_000, o=400_000):
    """A SessionReport whose usage is entirely one model (primary), for
    compare_models tests."""
    u = _u(i=i, o=o)
    mu = u.bucket(model)
    mu.input_tokens = i
    mu.output_tokens = o
    u.models.add(model)
    return metrics.compute(META, u, net_loc=net_loc,
                           code_added=added, code_deleted=deleted,
                           code_reworked=reworked)


def test_compare_models_churn_uses_self_rework():
    # Per-model 返工率 must match SessionReport.churn_ratio: self-rework
    # (reworked / added), NOT gross deleted / added. Regression guard for the
    # model-comparison tab diverging from the ranking/panel tabs.
    r = _single_model_report("model-x", added=1000, deleted=800, reworked=100)
    comps = metrics.compare_models([r])
    assert len(comps) == 1
    mc = comps[0]
    assert mc.model_id == "model-x"
    assert mc.churn_ratio == pytest_approx(0.10)   # gross would be 0.80


def test_compare_models_churn_falls_back_to_deleted():
    # Sessions predating code_reworked (None) fall back to gross deletions,
    # consistent with compute().
    r = _single_model_report("model-y", added=1000, deleted=300, reworked=None)
    mc = metrics.compare_models([r])[0]
    assert mc.churn_ratio == pytest_approx(0.30)


def test_compare_models_aggregates_per_model_fields():
    # Two distinct models → two buckets; derived per-model fields populate.
    rx = _single_model_report("model-x", added=400, deleted=40, reworked=40, net_loc=360)
    ry = _single_model_report("model-y", added=100, deleted=10, reworked=10, net_loc=90)
    comps = {mc.model_id: mc for mc in metrics.compare_models([rx, ry])}
    assert set(comps) == {"model-x", "model-y"}
    assert comps["model-x"].churn_ratio == pytest_approx(0.10)
    assert comps["model-x"].net_loc_per_session == pytest_approx(360)
    assert comps["model-y"].net_loc_per_session == pytest_approx(90)


# --------------------------------------------------------------------------- #
# New quality metrics: tool errors, thinking, files_touched, file quality
# --------------------------------------------------------------------------- #
def test_tool_error_rate():
    u = _u(i=500_000, o=500_000)
    u.tool_calls = {"Read": 10, "Write": 5, "Bash": 5}
    u.tool_errors = 4
    r = metrics.compute(META, u, net_loc=100)
    assert r.tool_error_rate == pytest_approx(4 / 20)


def test_tool_error_rate_zero_tools():
    u = _u(i=500_000, o=500_000)
    u.tool_errors = 0
    r = metrics.compute(META, u, net_loc=100)
    assert r.tool_error_rate is None


def test_files_touched_count():
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "Read", "/a.py"),
        ToolOp(0, "Read", "/b.py"),
        ToolOp(0, "Read", "/c.py"),
        ToolOp(1, "Write", "/a.py"),
        ToolOp(1, "Write", "/d.py"),
        ToolOp(2, "Edit", "/b.py"),
    ]
    r = metrics.compute(META, u, net_loc=100)
    # unique files: a, b, c, d = 4
    assert r.files_touched == 4
    assert r.files_touched_details is not None
    # a.py: read + write = 2 ops
    assert r.files_touched_details["/a.py"] == 2


def test_files_touched_excludes_search_only_paths():
    """涉及文件 = 真正读/写/改过的文件；只被 Grep/Glob 搜过的路径不计入。

    Grok/omp 的 Grep 会带 path，常为目录（如 .../tcer/core），把它计成「文件」会
    虚高「独立文件数」并污染涉及文件弹窗。Claude 的 Grep 无 path，天然不受影响。
    只被搜索工具碰过的路径排除；若同一路径也被 Read/Write/Edit 碰过则保留。
    """
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "Grep", "/proj/src"),      # 目录：仅搜索 → 排除
        ToolOp(0, "Glob", "/proj"),          # 目录：仅搜索 → 排除
        ToolOp(0, "Grep", "/proj/a.py"),     # 文件但仅搜索 → 排除
        ToolOp(1, "Read", "/proj/a.py"),     # a.py 也被读 → 保留
        ToolOp(2, "Edit", "/proj/a.py"),
        ToolOp(3, "Write", "/proj/b.py"),    # b.py 写入 → 保留
    ]
    r = metrics.compute(META, u, net_loc=100)
    assert r.files_touched == 2, r.files_touched_details
    assert set(r.files_touched_details) == {"/proj/a.py", "/proj/b.py"}
    assert "/proj/src" not in r.files_touched_details
    assert "/proj" not in r.files_touched_details
    # 搜索路径独立收集到「搜索足迹」——含目录与文件，按搜索次数计。
    assert r.searched_paths_details == {"/proj/src": 1, "/proj": 1, "/proj/a.py": 1}


def test_searched_paths_details_none_when_no_search():
    """无搜索工具调用时 searched_paths_details 为 None（不显示空区块）。"""
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [ToolOp(0, "Read", "/a.py"), ToolOp(1, "Edit", "/a.py")]
    r = metrics.compute(META, u, net_loc=100)
    assert r.searched_paths_details is None


def test_thinking_count_passthrough():
    u = _u(i=500_000, o=500_000)
    u.thinking_count = 7
    r = metrics.compute(META, u, net_loc=100)
    assert r.thinking_count == 7


def test_file_quality_metrics():
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        # Turn 0: search + read
        ToolOp(0, "Grep", "/a.py"),   # search a.py
        ToolOp(0, "Grep", "/b.py"),   # search b.py
        ToolOp(0, "Grep", "/c.py"),   # search c.py (no edit follows)
        ToolOp(0, "Read", "/a.py"),
        ToolOp(0, "Read", "/b.py"),
        ToolOp(0, "Read", "/d.py"),
        # Turn 1: edit within window (≤3 turns from search)
        ToolOp(1, "Edit", "/a.py"),   # edit a.py (turn 1 ≤ 0+3) ✓
        ToolOp(1, "Write", "/d.py"),  # write d.py (read before) ✓
        # Turn 5: edit outside window (>3 turns from turn 0 search)
        ToolOp(5, "Edit", "/b.py"),   # edit b.py (turn 5 > 0+3) ✗ for search, but read_before ✓
    ]
    r = metrics.compute(META, u, net_loc=100)
    # search_edit_ratio (turn-based): 3 searches at turn 0; an Edit/Write occurs at
    # turn 1 (within 0+3 window) → all 3 searches are "productive" → 3/3 = 1.0.
    assert r.search_edit_ratio == pytest_approx(1.0)
    # read_before_write: files with write/edit = {a, d, b}
    #   a.py: read turn 0, first write turn 1 → read before ✓
    #   d.py: read turn 0, first write turn 1 → read before ✓
    #   b.py: read turn 0, first write turn 5 → read before ✓
    # ratio = 3/3 = 1.0
    assert r.read_before_write == pytest_approx(1.0)


def test_search_edit_ratio_real_shape_no_path():
    """Real Grep/Glob carry no file_path → op.path is "". The turn-based
    search_edit_ratio must still work (the old path-based version returned None)."""
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "Grep", ""),        # repo-wide search, no path (real shape)
        ToolOp(1, "Edit", "/x.py"),   # follow-up edit within window ✓
        ToolOp(8, "Glob", ""),        # late search, no edit follows ✗
    ]
    r = metrics.compute(META, u, net_loc=10)
    # 2 searches; 1 followed by an edit within 3 turns → 1/2
    assert r.search_edit_ratio == pytest_approx(0.5)


def test_search_edit_ratio_outside_window():
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "Grep", ""),
        ToolOp(9, "Edit", "/x.py"),   # 9 > 0+3 → not productive
    ]
    r = metrics.compute(META, u, net_loc=10)
    assert r.search_edit_ratio == pytest_approx(0.0)


def test_search_edit_ratio_counts_mcp_and_bare_search_aliases():
    """Live sessions often search only via firecrawl_search / mcp query tools."""
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "firecrawl_search", ""),
        ToolOp(1, "Edit", "/a.py"),  # follow-up ✓
        ToolOp(5, "mcp__tavily__tavily_search", ""),
        ToolOp(10, "Write", "/b.py"),  # outside window ✗
        ToolOp(11, "ToolSearch", ""),  # meta — must not count
        ToolOp(12, "Edit", "/c.py"),
    ]
    r = metrics.compute(META, u, net_loc=10)
    # 2 code-searches; 1 productive → 0.5
    assert r.search_edit_ratio == pytest_approx(0.5)


def test_file_quality_no_searches():
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "Edit", "/a.py"),
    ]
    r = metrics.compute(META, u, net_loc=0)
    # No searches → ste = None
    assert r.search_edit_ratio is None
    # Write without prior read → rbw = 0/1 = 0.0
    assert r.read_before_write == pytest_approx(0.0)


def test_file_quality_write_before_read():
    """Write first, Read later — should NOT count as read-before-write."""
    u = _u(i=500_000, o=500_000)
    u.tool_ops = [
        ToolOp(0, "Write", "/a.py"),  # write first
        ToolOp(1, "Read", "/a.py"),   # read after
    ]
    r = metrics.compute(META, u, net_loc=100)
    # Read was NOT before Write → rbw = 0/1
    assert r.read_before_write == pytest_approx(0.0)


def test_user_msgs_passthrough():
    u = _u(i=500_000, o=500_000)
    u.user_msgs = 12
    u.user_message_texts = ["hello", "fix the bug"]
    r = metrics.compute(META, u, net_loc=100)
    assert r.usage.user_msgs == 12
    assert len(r.usage.user_message_texts) == 2




def test_infer_task_type_extended_signals():
    """扩展信号:文档主导→非编码;高错误率+重 Bash→维护;缺失时不变。"""
    # 中等产出、几乎全是文档 → 非编码(文档/调研)
    assert metrics.infer_task_type(
        net_loc=50, total_tokens=1_000_000,
        doc_net_loc=45,
    ) == "non_coding"
    # 同样产出但无文档信号 → 保持原判(创作/维护)
    assert metrics.infer_task_type(
        net_loc=50, total_tokens=1_000_000,
    ) != "non_coding"
    # 强创作信号(高伪 TCER)不被单一文档信号翻转
    assert metrics.infer_task_type(
        net_loc=200, total_tokens=1_000_000,
        doc_net_loc=180,
    ) == "code_creation"
    # 中等产出 + 高工具错误率 + 重 Bash → 维护
    assert metrics.infer_task_type(
        net_loc=30, total_tokens=1_000_000,
        tool_error_rate=0.2, bash_ratio=0.6,
    ) == "code_maintenance"
    # 中等产出、测试行主导 → 维护倾斜
    assert metrics.infer_task_type(
        net_loc=50, total_tokens=1_000_000,
        test_net_loc=40,
    ) == "code_maintenance"


def test_retry_loop_metrics_detects_runs():
    from tcer.core.metrics import retry_loop_metrics
    from tcer.core.models import TokenUsage, ToolOp

    u = TokenUsage()
    u.tool_ops = [
        ToolOp(0, "Read", "a.py"),
        ToolOp(1, "Edit", "a.py"), ToolOp(2, "Edit", "a.py"), ToolOp(3, "Edit", "a.py"),
        ToolOp(4, "Edit", "a.py"),   # 4 连 Edit a.py → 一个长度 4 的循环
        ToolOp(5, "Read", "b.py"),
        ToolOp(6, "Read", "b.py"), ToolOp(7, "Read", "b.py"),  # 3 连 → 一个长度 3
        ToolOp(8, "Bash", ""), ToolOp(9, "Bash", ""), ToolOp(10, "Bash", ""),  # 无路径不参与
        ToolOp(11, "Grep", "src/"), ToolOp(12, "Grep", "src/"),  # 2 连不够
    ]
    m = retry_loop_metrics(u)
    assert m["count"] == 2
    assert m["max_len"] == 4
    assert m["details"] == {"Edit:a.py": 4, "Read:b.py": 3}
    # 无循环
    u2 = TokenUsage()
    u2.tool_ops = [ToolOp(0, "Read", "x.py"), ToolOp(1, "Edit", "x.py")]
    assert retry_loop_metrics(u2) == {"count": 0, "max_len": 0, "details": None}


def test_compute_populates_retry_loop_fields():
    from tcer.core.metrics import compute
    from tcer.core.models import SessionMeta, TokenUsage, ToolOp

    meta = SessionMeta(session_id="s", cwd="/tmp", title=None, path=Path("/tmp/s.jsonl"),
                       is_subagent=False)
    u = TokenUsage(input_tokens=1000, output_tokens=100)
    u.tool_ops = [ToolOp(i, "Edit", "a.py") for i in range(5)]
    r = compute(meta, u, net_loc=10, task_type="feature")
    assert r.retry_loop_count == 1
    assert r.retry_loop_max_len == 5
    assert r.retry_loop_details == {"Edit:a.py": 5}


def test_retry_loop_insight_fires():
    from tcer.core.insights import session_insights
    from tcer.core.metrics import compute
    from tcer.core.models import SessionMeta, TokenUsage, ToolOp

    meta = SessionMeta(session_id="s", cwd="/tmp", title=None, path=Path("/tmp/s.jsonl"),
                       is_subagent=False)
    u = TokenUsage(input_tokens=1000, output_tokens=100)
    u.tool_ops = [ToolOp(i, "Edit", "a.py") for i in range(6)]
    r = compute(meta, u, net_loc=10, task_type="feature")
    hits = [i for i in session_insights(r) if i.metric == "retry_loop_count"]
    assert hits and "a.py" in hits[0].evidence


def test_turn_cost_analysis_spike_and_invalidation():
    from tcer.core.metrics import turn_cost_analysis
    from tcer.core.models import TokenUsage, TurnStat

    u = TokenUsage()
    u.turn_stats = [
        TurnStat(0, ts=1, input_tokens=1000, cache_write=100, cache_read=5000,
                 output_tokens=200, model="claude-sonnet-4-5"),
        TurnStat(1, ts=2, input_tokens=1000, cache_write=100, cache_read=6000,
                 output_tokens=200, model="claude-sonnet-4-5"),
        # 大户回合 + 缓存失效（cw 翻倍、cr 回落）
        TurnStat(2, ts=3, input_tokens=30000, cache_write=5000, cache_read=1000,
                 output_tokens=5000, model="claude-sonnet-4-5"),
    ]
    m = turn_cost_analysis(u)
    assert m["spike_turn"] == 2
    assert m["max_turn_share"] > 0.30
    assert m["max_turn_cost"] > 0
    assert m["cache_invalidation_events"] == 1
    # 无数据
    assert turn_cost_analysis(TokenUsage())["max_turn_share"] is None


def test_compute_populates_turn_cost_fields():
    from tcer.core.metrics import compute
    from tcer.core.models import SessionMeta, TokenUsage, TurnStat

    meta = SessionMeta(session_id="s", cwd="/tmp", title=None, path=Path("/tmp/s.jsonl"),
                       is_subagent=False)
    u = TokenUsage(input_tokens=1000, output_tokens=100)
    u.turn_stats = [
        TurnStat(0, ts=1, input_tokens=100, cache_write=0, cache_read=0,
                 output_tokens=10, model="claude-sonnet-4-5"),
        TurnStat(1, ts=2, input_tokens=5000, cache_write=0, cache_read=0,
                 output_tokens=500, model="claude-sonnet-4-5"),
    ]
    r = compute(meta, u, net_loc=10, task_type="feature")
    assert r.turn_cost_spike_turn == 1
    assert r.turn_cost_max_share is not None and r.turn_cost_max_share > 0.5


def test_activity_metrics_ratio_and_gap():
    from tcer.core.metrics import activity_metrics
    from tcer.core.models import TokenUsage, TurnStat

    T = 1_770_000_000_000
    u = TokenUsage()
    u.session_duration_ms = 30 * 60_000
    u.turn_stats = [
        # 3 回合各 5 分钟 AI 耗时 → 15/30 = 0.5
        TurnStat(0, ts=T, duration_ms=5 * 60_000),
        # 间隔 10 分钟（在 1–30 分钟窗口内）
        TurnStat(1, ts=T + 10 * 60_000, duration_ms=5 * 60_000),
        # 间隔 2 分钟
        TurnStat(2, ts=T + 12 * 60_000, duration_ms=5 * 60_000),
    ]
    m = activity_metrics(u)
    assert m["ai_active_ratio"] == 0.5
    assert sorted([10.0, 2.0])[0] == 2.0
    assert m["user_gap_median_min"] == 6.0  # median(10, 2)
    # 无 duration / 无窗口内间隔 → None
    u2 = TokenUsage()
    u2.turn_stats = [TurnStat(0, ts=T)]
    assert activity_metrics(u2) == {"ai_active_ratio": None, "user_gap_median_min": None}
    # 窗口外间隔不计：<1 分钟与 >30 分钟
    u3 = TokenUsage()
    u3.turn_stats = [TurnStat(0, ts=T), TurnStat(1, ts=T + 30_000),
                     TurnStat(2, ts=T + 31 * 60_000)]
    assert activity_metrics(u3)["user_gap_median_min"] is None
    # Grok 审批等待从活跃时间中扣除
    u4 = TokenUsage()
    u4.session_duration_ms = 10 * 60_000
    u4.permission_wait_ms_total = 5 * 60_000
    u4.turn_stats = [TurnStat(0, ts=T, duration_ms=8 * 60_000)]
    assert activity_metrics(u4)["ai_active_ratio"] == 0.3


def test_segment_metrics_decay_and_compaction():
    from tcer.core.metrics import segment_metrics
    from tcer.core.models import TokenUsage, TurnStat

    u = TokenUsage()
    # 9 回合：前 3 回合每回合 10 行 1Mt，后 3 回合每回合 1 行 1Mt → 末/首 = 0.1
    u.turn_stats = []
    u.turn_net_locs = []
    for i in range(9):
        u.turn_stats.append(TurnStat(i, ts=i, input_tokens=500_000,
                                     cache_read=400_000, output_tokens=100_000))
        loc = 10 if i < 3 else (5 if i < 6 else 1)
        u.turn_net_locs.append((i, loc, 0))
    u.compaction_turns = [5]
    m = segment_metrics(u)
    assert m["decay_ratio"] == 0.1
    segs = m["segments"]
    assert segs[0]["tcer"] == 10 and segs[2]["tcer"] == 1
    assert segs[1]["compactions"] == 1
    # 无逐回合 LOC → tcer None（token 曲线仍可用）
    u2 = TokenUsage()
    u2.turn_stats = u.turn_stats
    m2 = segment_metrics(u2)
    assert m2["decay_ratio"] is None
    assert m2["segments"][0]["tokens"] > 0
    # 空
    assert segment_metrics(TokenUsage())["decay_ratio"] is None


def test_compute_populates_decay_ratio():
    from tcer.core.metrics import compute
    from tcer.core.models import SessionMeta, TokenUsage, TurnStat

    meta = SessionMeta(session_id="s", cwd="/tmp", title=None, path=Path("/tmp/s.jsonl"),
                       is_subagent=False)
    u = TokenUsage(input_tokens=1000, output_tokens=100)
    u.turn_stats = [TurnStat(i, ts=i, input_tokens=100, output_tokens=10) for i in range(9)]
    u.turn_net_locs = [(i, 10 if i < 3 else 1, 0) for i in range(9)]
    r = compute(meta, u, net_loc=40, task_type="feature")
    assert abs(r.efficiency_decay_ratio - 0.1) < 1e-9


def test_merge_rebases_turn_net_locs_by_turn_stats():
    """merge 的 turn_net_locs 必须按 turn_stats 的最大回合 rebase（与
    turn_stats 同一编号空间），否则末尾无工具回合的会话合并后 LOC 错段。"""
    from tcer.core.models import TokenUsage, TurnStat

    a = TokenUsage(input_tokens=10, output_tokens=5)
    a.turn_stats = [TurnStat(i, ts=i, input_tokens=1, output_tokens=1)
                    for i in range(5)]          # 5 回合
    a.tool_ops = []                             # 无工具 → tool_ops max = -1
    a.turn_net_locs = [(0, 10, 0)]
    b = TokenUsage(input_tokens=10, output_tokens=5)
    b.turn_stats = [TurnStat(i, ts=i, input_tokens=1, output_tokens=1)
                    for i in range(3)]
    b.turn_net_locs = [(0, 7, 0)]
    m = a.merge(b)
    # b 的回合 0 必须 rebase 到 5 之后（若按 tool_ops 的 -1 会错位到 0）
    assert m.turn_net_locs == [(0, 10, 0), (5, 7, 0)]


def test_segment_metrics_turn_number_not_index():
    """段边界按回合号（TurnStat.turn）归属，不是列表下标——零 usage 桩造成
    回合号空洞时，尾段 LOC 不得被丢弃。"""
    from tcer.core.metrics import segment_metrics
    from tcer.core.models import TokenUsage, TurnStat

    u = TokenUsage()
    # 6 个 stat 但回合号 0..5 中 2 号被零 usage 桩消耗（无 TurnStat）→ 5/9 类空洞
    u.turn_stats = [TurnStat(t, ts=t, input_tokens=100, output_tokens=10)
                    for t in (0, 1, 3, 4, 5, 6)]
    # LOC 挂在真实回合号上：末段（回合 5、6）各 10 行，首段（0、1）各 1 行
    u.turn_net_locs = [(0, 1, 0), (1, 1, 0), (5, 10, 0), (6, 10, 0)]
    u.compaction_turns = [6]
    m = segment_metrics(u)
    segs = m["segments"]
    # 首段 LOC=2；末段（下标 4..5 = 回合 5、6）LOC=20——旧行为会丢掉回合 6（下标<6 但按 range(4,6) 查到）
    assert segs[0]["added"] == 2
    assert segs[2]["added"] == 20
    assert segs[2]["compactions"] == 1   # 压缩位置也按回合号归段
    assert abs(m["decay_ratio"] - 10.0) < 1e-9  # 20/2
