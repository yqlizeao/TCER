"""Tests for metrics.compute_baselines / save_baselines (personal baselines)."""
from __future__ import annotations

import json
from pathlib import Path

from tcer.core import metrics
from tcer.core.models import SessionMeta, TokenUsage


def _report(net_loc: int):
    meta = SessionMeta(session_id="s", cwd="/tmp", title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=500_000, output_tokens=500_000)
    return metrics.compute(meta, u, net_loc=net_loc, task_type="feature")


def test_compute_baselines_uses_median_and_mean():
    # tcer values 80, 40, 120 → median 80; cpe varies
    reports = [_report(n) for n in (400, 200, 600)]
    # Unit test uses min_sessions=1 to check arithmetic only.
    out = metrics.compute_baselines(reports, min_sessions=1)
    assert out is not None
    assert set(out) == {"tcer", "cpe"}
    tcer_vals = sorted(r.tcer for r in reports)
    assert out["tcer"] == tcer_vals[len(tcer_vals) // 2]  # median


def test_compute_baselines_method_mean_vs_median():
    import statistics
    reports = [_report(n) for n in (200, 400, 1200)]  # skewed → mean ≠ median
    med = metrics.compute_baselines(reports, min_sessions=1, method="median")
    mean = metrics.compute_baselines(reports, min_sessions=1, method="mean")
    tcer_vals = [r.tcer for r in reports]
    assert med["tcer"] == statistics.median(tcer_vals)
    assert abs(mean["tcer"] - statistics.mean(tcer_vals)) < 1e-9
    assert med["tcer"] != mean["tcer"]  # skewed sample → different


def test_compute_baselines_none_when_no_complete_data():
    # net_loc=None → tcer/cpe all None → no valid session
    meta = SessionMeta(session_id="s", cwd=None, title=None,
                       path=Path("/tmp/s.jsonl"), is_subagent=False)
    r = metrics.compute(meta, TokenUsage(input_tokens=10, output_tokens=5), net_loc=None)
    assert metrics.compute_baselines([r], min_sessions=1) is None


def test_compute_baselines_requires_min_sessions_by_default():
    """Default MIN_BASELINE_SESSIONS guards against tiny, jumpy samples."""
    reports = [_report(n) for n in (400, 200, 600)]
    assert len(reports) < metrics.MIN_BASELINE_SESSIONS
    assert metrics.compute_baselines(reports) is None
    assert metrics.compute_baselines(reports, min_sessions=3) is not None


def test_baseline_filters_near_zero_output_outliers():
    """近零产出会话（net_loc < MIN_BASELINE_NET_LOC）的 CPE 被放大失真，
    不应参与基准样本。"""
    floor = metrics.MIN_BASELINE_NET_LOC
    tiny = _report(floor - 1)   # 低于下限 → 剔除
    ok = _report(400)           # 正常 → 保留
    elig = metrics.baseline_eligible_reports([tiny, ok])
    assert ok in elig and tiny not in elig
    # min_net_loc=0 关闭过滤（单元测试核对纯算术用）
    assert len(metrics.baseline_eligible_reports([tiny, ok], min_net_loc=0)) == 2


def test_baseline_outlier_does_not_skew_median():
    """离群会话被过滤后，基准中位数只由正常会话决定。"""
    reports = [_report(n) for n in (200, 400, 600)] + [_report(2)]  # 最后一个是离群
    out = metrics.compute_baselines(reports, min_sessions=1)
    normal = metrics.compute_baselines(reports[:3], min_sessions=1)
    assert out == normal  # 离群被剔除，结果与只用正常会话一致


def test_save_baselines_writes_and_refreshes_globals(tmp_path):
    real_path = metrics._COMPOSITE_CONFIG_PATH
    orig = (metrics.TCER_BASELINE, metrics.CPE_BASELINE)
    tmp = tmp_path / "composite_baselines.json"
    tmp.write_text(real_path.read_text(encoding="utf-8"), encoding="utf-8")
    metrics._COMPOSITE_CONFIG_PATH = tmp
    try:
        metrics._load_composite_config.cache_clear()
        metrics.save_baselines({"tcer": 123.45, "cpe": 9.9})
        cfg = json.loads(tmp.read_text(encoding="utf-8"))
        assert cfg["baselines"]["tcer"] == 123.45
        assert metrics.TCER_BASELINE == 123.45
        assert metrics.CPE_BASELINE == 9.9
    finally:
        metrics._COMPOSITE_CONFIG_PATH = real_path
        metrics._load_composite_config.cache_clear()
        metrics._refresh_composite_globals()


def test_resolve_baselines_falls_back_to_global():
    """无逐项目基准 → resolve_baselines 返回全局值。"""
    out = metrics.resolve_baselines("nonexistent-uid")
    assert out["tcer"] == metrics.TCER_BASELINE
    assert out["cpe"] == metrics.CPE_BASELINE
    # None uid 同样回退全局
    assert metrics.resolve_baselines(None)["tcer"] == metrics.TCER_BASELINE


def test_save_and_resolve_per_project_baseline(tmp_path):
    """逐项目基准：写入只影响该项目，全局不变；resolve 优先取逐项目。"""
    real_path = metrics._COMPOSITE_CONFIG_PATH
    tmp = tmp_path / "composite_baselines.json"
    tmp.write_text(real_path.read_text(encoding="utf-8"), encoding="utf-8")
    metrics._COMPOSITE_CONFIG_PATH = tmp
    try:
        metrics._load_composite_config.cache_clear()
        metrics._refresh_composite_globals()
        glob_tcer = metrics.TCER_BASELINE
        uid = "claude:.claude:c--GitHub-Demo"
        metrics.save_baselines({"tcer": 99.0, "cpe": 7.0}, project_uid=uid)
        # 该项目取逐项目值
        r = metrics.resolve_baselines(uid)
        assert r["tcer"] == 99.0 and r["cpe"] == 7.0
        # 其它项目仍回退全局（全局未被改动）
        assert metrics.resolve_baselines("other-uid")["tcer"] == glob_tcer
        assert metrics.TCER_BASELINE == glob_tcer  # 全局常量不受逐项目写入影响
        cfg = json.loads(tmp.read_text(encoding="utf-8"))
        assert cfg["baselines_per_project"][uid] == {"tcer": 99.0, "cpe": 7.0}
    finally:
        metrics._COMPOSITE_CONFIG_PATH = real_path
        metrics._load_composite_config.cache_clear()
        metrics._refresh_composite_globals()


def test_score_tracks_refreshed_baseline(tmp_path):
    """After a personal-baseline save rebinds the module globals, efficiency_score()
    with default args must use the NEW baseline — not the import-time value.
    Regression guard: frozen default args froze the old value, so the score stayed
    stale until process restart."""
    real_path = metrics._COMPOSITE_CONFIG_PATH
    tmp = tmp_path / "composite_baselines.json"
    tmp.write_text(real_path.read_text(encoding="utf-8"), encoding="utf-8")
    metrics._COMPOSITE_CONFIG_PATH = tmp
    try:
        metrics._load_composite_config.cache_clear()
        before = metrics.efficiency_score(80.0, 9.0, 0.1, 0.0, 0.8, net_loc=500)
        metrics.save_baselines({"tcer": 123.45, "cpe": 9.9})
        after = metrics.efficiency_score(80.0, 9.0, 0.1, 0.0, 0.8, net_loc=500)
        # default-arg score must now reflect the refreshed globals
        assert after == metrics.efficiency_score(
            80.0, 9.0, 0.1, 0.0, 0.8, net_loc=500,
            tcer_baseline=123.45, cpe_baseline=9.9)
        assert abs(after - before) > 1e-9, "score ignored the refreshed baseline"
    finally:
        metrics._COMPOSITE_CONFIG_PATH = real_path
        metrics._load_composite_config.cache_clear()
        metrics._refresh_composite_globals()
