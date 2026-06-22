"""Guard the metric_defs single-source-of-truth invariant.

Every metric key declared in ``GROUPS`` must be produced by ``report_values``;
otherwise the grid would silently show "-" for an undefined key. This catches
drift between the definitions and the formatter.
"""
from __future__ import annotations

from pathlib import Path

from tcer.core import metrics
from tcer.gui import metric_defs
from tcer.core.models import SessionMeta, TokenUsage


def _report():
    meta = SessionMeta(session_id="s", cwd="/tmp", title="t",
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=500_000, output_tokens=500_000)
    return metrics.compute(meta, u, net_loc=400, loc_accumulated=10_000,
                           task_type="feature", code_added=420, code_deleted=20)


def test_every_layer_key_is_formatted():
    vals = metric_defs.report_values(_report())
    missing = metric_defs.ALL_KEYS - set(vals)
    assert not missing, f"metric_defs keys missing from report_values: {missing}"


def test_groups_cover_six_groups():
    assert [g.id for g in metric_defs.GROUPS] == ["G1", "G2", "G3", "G4", "G5", "G6"]


def test_tcer_is_the_only_english_metric_name():
    """Requirement: GUI shows full Chinese; TCER is the sole abbreviation kept."""
    english = [m.name for group in metric_defs.GROUPS for m in group.metrics
               if m.name.isascii() and any(c.isalpha() for c in m.name)]
    assert english == ["TCER"], f"unexpected English metric names: {english}"


# --------------------------------------------------------------------------- #
# SSOT engine: format_value / raw_value / display / fmt coverage
# --------------------------------------------------------------------------- #
def test_every_metric_has_a_format_spec():
    """Each session metric's fmt is populated from _SESSION_FMT (default 'text')."""
    for g in metric_defs.GROUPS:
        for m in g.metrics:
            assert m.fmt, f"{m.key} has empty fmt"
            assert m.fmt == metric_defs._SESSION_FMT.get(m.key, "text")


def test_format_value_tokens():
    assert metric_defs.format_value("total_tokens", 5_000_000) == "5,000,000"
    assert metric_defs.format_value("chr", 0.959) == "95.9%"      # pct ×100
    assert metric_defs.format_value("cost", 1.2345) == "$1.2345"   # money 4dp
    assert metric_defs.format_value("cost_per_mt", 1.6) == "$1.60"  # money2 2dp
    assert metric_defs.format_value("tcer", 60.13) == "60.1"        # float:0.0
    assert metric_defs.format_value("ncpi", 0.0305) == "0.030"      # float:0.000


def test_format_value_none_is_dash():
    for key in ("cost", "chr", "tcer", "ncpi", "cost_per_mt", "net_loc"):
        assert metric_defs.format_value(key, None) == "-"


def test_display_matches_report_values():
    r = _report()
    rv = metric_defs.report_values(r)
    for key in metric_defs.ALL_KEYS:
        assert metric_defs.display(r, key) == rv[key]


def test_report_values_golden_strings():
    """Deterministic synthetic report → exact display strings (regression lock)."""
    meta = SessionMeta(session_id="s", cwd="/tmp", title="t",
                       path=Path("/tmp/s.jsonl"), is_subagent=False,
                       entrypoint="claude-vscode")
    u = TokenUsage(input_tokens=20_000, cache_creation_input_tokens=180_000,
                   cache_read_input_tokens=4_700_000, output_tokens=100_000,
                   assistant_msgs=50, empty_usage_skipped=3, user_msgs=12,
                   thinking_count=7)
    u.tool_calls = {"Read": 10, "Edit": 5}
    r = metrics.compute(meta, u, net_loc=400, loc_accumulated=10_000,
                        task_type="code_creation", code_added=420,
                        code_deleted=20, code_reworked=20)
    v = metric_defs.report_values(r)
    assert v["total_tokens"] == "5,000,000"
    assert v["input"] == "20,000"
    assert v["cache_read"] == "4,700,000"
    assert v["chr"] == "95.9%"               # 4.7M / 4.9M
    assert v["net_loc"] == "400"
    assert v["added"] == "420"
    assert v["deleted"] == "20"
    assert v["churn"] == "4.8%"              # reworked 20 / added 420
    assert v["user_msgs"] == "12"
    assert v["turns"] == "50（+3 跳过）"
    assert v["thinking_count"] == "7"
    assert v["tools"] == "15 次（2 种）"
    assert v["entrypoint"] == "claude-vscode"
    assert v["task_type"] == "代码创作"
    assert v["edit_ratio"] == "100.0%"       # Edit / (Edit+Write) = 5/5
    assert v["read_write_ratio"] == "2.0"     # Read/(Write+Edit)=10/5; a ratio, not %
    assert v["grade"] in ("优秀", "良好", "中等", "低效", "极端低效")


def test_raw_value_scaling_and_none():
    """raw_value: only chr is scaled to 0–100; text & high_churn_files → None."""
    r = _report()
    # chr scaled to 0–100
    assert metric_defs.raw_value(r, "chr") == r.chr * 100.0
    # other pct metrics stay native (matches long-standing chart behaviour)
    assert metric_defs.raw_value(r, "churn") == r.churn_ratio
    # text metrics are not numeric
    assert metric_defs.raw_value(r, "models") is None
    assert metric_defs.raw_value(r, "grade") is None
    # high_churn_files has no chart attr mapping → None (faithful to old behaviour)
    assert metric_defs.raw_value(r, "high_churn_files") is None


# --------------------------------------------------------------------------- #
# Per-model SSOT (MODEL_GROUPS / model_display / model_raw)
# --------------------------------------------------------------------------- #
def test_every_model_key_resolvable():
    """Every MODEL_GROUPS key has a value+display extractor (drift guard)."""
    keys = {m.key for g in metric_defs.MODEL_GROUPS for m in g.metrics}
    assert keys == set(metric_defs._MODEL_EXTRACTORS)
    assert keys == set(metric_defs.MODEL_BY_KEY)


def test_model_display_golden():
    """model_display reproduces the model tab's exact strings + special cases."""
    mc = metrics.ModelComparison(model_id="x", display_name="X")
    mc.input_tokens, mc.output_tokens = 20_000, 1_500_000
    mc.cache_creation_tokens, mc.cache_read_tokens = 180_000, 4_700_000
    mc.cost = 12.5
    mc.cost_share = 33.3
    mc.tokens_per_dollar = 1_600_000
    mc.code_per_dollar = 42.0
    mc.token_share = 50.0
    mc.cache_hit_ratio = 0.876
    mc.session_count = 7
    mc.net_loc_per_session = 123.4
    mc.tool_error_rate = 0.05
    mc.churn_ratio = 0.082
    md = metric_defs.model_display
    assert md(mc, "m_total_tokens") == "6,400,000"      # full count, like the grid
    assert md(mc, "m_cost") == "$12.5000"               # fmt_money (4 dp), like the grid
    assert md(mc, "m_cost_share") == "33.3%"
    assert md(mc, "m_tokens_per_dollar") == "1,600,000/$"
    assert md(mc, "m_code_per_dollar") == "42.0 行/$"
    assert md(mc, "m_cache_hit_ratio") == "87.6%"
    assert md(mc, "m_session_count") == "7"
    assert md(mc, "m_churn") == "8.2%"


def test_model_linked_metrics_match_grid():
    """Linked per-model metrics render byte-identically to the 指标分类 grid
    (the single-source guarantee). Drift here is exactly what we want to catch."""
    mc = metrics.ModelComparison(model_id="x", display_name="X")
    mc.input_tokens, mc.output_tokens = 20_000, 1_500_000
    mc.cache_creation_tokens, mc.cache_read_tokens = 180_000, 4_700_000
    mc.cost = 12.5
    mc.cache_hit_ratio = 0.876
    mc.churn_ratio = 0.082
    fv = metric_defs.format_value
    md = metric_defs.model_display
    assert md(mc, "m_total_tokens") == fv("total_tokens", mc.total_tokens)
    assert md(mc, "m_input") == fv("input", mc.input_tokens)
    assert md(mc, "m_output") == fv("output", mc.output_tokens)
    assert md(mc, "m_cache_read") == fv("cache_read", mc.cache_read_tokens)
    assert md(mc, "m_cache_hit_ratio") == fv("chr", mc.cache_hit_ratio)
    assert md(mc, "m_churn") == fv("churn", mc.churn_ratio)


def test_model_display_free_and_none():
    """cost==0 → 免费 / ∞; None metrics → '-'."""
    mc = metrics.ModelComparison(model_id="f", display_name="Free")
    mc.cost = 0.0
    mc.tokens_per_dollar = None
    mc.code_per_dollar = None
    mc.churn_ratio = None
    md = metric_defs.model_display
    assert md(mc, "m_cost") == "免费"
    assert md(mc, "m_tokens_per_dollar") == "∞"
    assert md(mc, "m_code_per_dollar") == "∞"
    assert md(mc, "m_churn") == "-"


def test_model_tip_borrowed_from_session():
    """Linked model metrics borrow the session metric's name+tip; others None."""
    tip = metric_defs.model_tip("m_cache_hit_ratio")
    assert tip is not None and tip.startswith("缓存命中率")
    assert metric_defs.model_tip("m_cost_share") is None  # no session counterpart


