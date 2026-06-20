"""Guard the metric_defs single-source-of-truth invariant.

Every metric key declared in ``LAYERS`` must be produced by ``report_values``;
otherwise the grid would silently show "-" for an undefined key. This catches
drift between the definitions and the formatter.
"""
from __future__ import annotations

from pathlib import Path

from tcer import metrics
from tcer.gui import metric_defs
from tcer.models import SessionMeta, TokenUsage


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


def test_layers_cover_six_framework_layers():
    assert [l.id for l in metric_defs.LAYERS] == ["L0", "L1", "L2", "L3", "L4", "L5"]


def test_tcer_is_the_only_english_metric_name():
    """Requirement: GUI shows full Chinese; TCER is the sole abbreviation kept."""
    english = [m.name for layer in metric_defs.LAYERS for m in layer.metrics
               if m.name.isascii() and any(c.isalpha() for c in m.name)]
    assert english == ["TCER"], f"unexpected English metric names: {english}"
