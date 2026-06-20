"""Tests for metrics.compute_baselines / save_baselines (personal CTEI baselines)."""
from __future__ import annotations

import json
from pathlib import Path

from tcer import metrics
from tcer.models import SessionMeta, TokenUsage


def _report(net_loc: int):
    meta = SessionMeta(session_id="s", cwd="/tmp", title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=500_000, output_tokens=500_000)
    return metrics.compute(meta, u, net_loc=net_loc, loc_accumulated=10_000,
                           task_type="feature")


def test_compute_baselines_uses_median_and_mean():
    # tcer values 80, 40, 120 → median 80; cpe varies; ncpi varies
    reports = [_report(n) for n in (400, 200, 600)]
    out = metrics.compute_baselines(reports)
    assert out is not None
    assert set(out) == {"tcer", "ncpi", "cpe"}
    tcer_vals = sorted(r.tcer for r in reports)
    assert out["tcer"] == tcer_vals[len(tcer_vals) // 2]  # median


def test_compute_baselines_none_when_no_complete_data():
    # net_loc=None → tcer/ncpi/cpe all None → no valid session
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert metrics.compute_baselines([r]) is None


def test_save_baselines_writes_and_refreshes_globals(tmp_path):
    real_path = metrics._COMPOSITE_CONFIG_PATH
    orig = (metrics.TCER_BASELINE, metrics.NCPI_BASELINE, metrics.CPE_BASELINE)
    tmp = tmp_path / "composite_baselines.json"
    tmp.write_text(real_path.read_text(encoding="utf-8"), encoding="utf-8")
    metrics._COMPOSITE_CONFIG_PATH = tmp
    try:
        metrics._load_composite_config.cache_clear()
        metrics.save_baselines({"tcer": 123.45, "ncpi": 0.555, "cpe": 9.9})
        cfg = json.loads(tmp.read_text(encoding="utf-8"))
        assert cfg["ctei_baselines"]["tcer"] == 123.45
        assert cfg["ctei_baselines"]["ncpi"] == 0.555
        assert metrics.TCER_BASELINE == 123.45
        assert metrics.NCPI_BASELINE == 0.555
        assert metrics.CPE_BASELINE == 9.9
    finally:
        metrics._COMPOSITE_CONFIG_PATH = real_path
        metrics._load_composite_config.cache_clear()
        metrics.TCER_BASELINE, metrics.NCPI_BASELINE, metrics.CPE_BASELINE = orig
