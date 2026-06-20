"""Tests for metrics.py — formula correctness and divide-by-zero safety."""
from __future__ import annotations

from pathlib import Path

from tcer import metrics
from tcer.models import SessionMeta, TokenUsage

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
# Composite layer (L5): CTEI / TTAF / TA-TCER / PSAC / CAF / grade
# --------------------------------------------------------------------------- #
def test_ctei_reproduces_report_excellent_session():
    # Report §6.3, session 4.22/5.3-codex: TCER=111.04, NCPI=0.189, CPE=4.45,
    # CHR≈0 → published CTEI = 5.017. Validates the formula + baselines.
    c = metrics.ctei(111.04, 0.189, 4.45, 0.0)
    assert c == pytest_approx(5.017, rel=0.01)
    assert metrics.grade(c) == "优秀"


def test_ctei_reproduces_report_extreme_low_session():
    # Report §6.3, session 5.13/5.4: TCER=28.62, NCPI=0.051, CPE=28.40 → CTEI=0.055.
    c = metrics.ctei(28.62, 0.051, 28.40, 0.0)
    assert c == pytest_approx(0.055, rel=0.02)
    assert metrics.grade(c) == "极端低效"


def test_ctei_chr_factor_rewards_cache():
    # CHR factor = 1 + CHR*0.5: 40% CHR → +20% CTEI vs CHR=0.
    base = metrics.ctei(76.59, 0.101, 8.22, 0.0)
    with_chr = metrics.ctei(76.59, 0.101, 8.22, 0.40)
    assert base == pytest_approx(1.0, rel=0.01)  # all-baseline session scores ~1.0
    assert with_chr == pytest_approx(base * 1.20, rel=0.01)


def test_ctei_none_when_inputs_missing():
    assert metrics.ctei(None, 0.1, 8.0, 0.0) is None
    assert metrics.ctei(80.0, None, 8.0, 0.0) is None
    assert metrics.ctei(80.0, 0.1, 0, 0.0) is None  # CPE=0 → undefined


def test_ttaf_table_matches_report():
    # Report §6.4 authoritative values (differ from CLAUDE.md for refactor/review).
    assert metrics.TTAF["feature"] == 1.00
    assert metrics.TTAF["debug"] == 0.40
    assert metrics.TTAF["refactor"] == 0.50
    assert metrics.TTAF["review"] == 0.20


def test_ta_tcer_debug_example():
    # Report §6.4 worked example: debug TCER=35.0 → TA-TCER = 35.0/0.40 = 87.5.
    assert metrics.ta_tcer(35.0, "debug") == pytest_approx(87.5)
    assert metrics.ta_tcer(35.0, "feature") == pytest_approx(35.0)  # TTAF 1.0
    assert metrics.ta_tcer(35.0, "unknown") is None  # unknown task type


def test_psac_formula():
    # PSAC = 83.64 / (83.64 - 0.000866*LOC). At LOC=23694 → ~1.325.
    p = metrics.psac(23694)
    expected = 83.64 / (83.64 - 0.000866 * 23694)
    assert p == pytest_approx(expected)
    assert p == pytest_approx(1.325, rel=0.01)
    assert metrics.psac(None) is None


def test_caf_formula():
    # CAF = TotalInput / (input + cache_write). Heavy cache reads → CAF >> 1.
    u = _u(i=100, cw=100, cr=800)  # total_input=1000, denom=200
    assert metrics.caf(u) == pytest_approx(1000 / 200)
    assert metrics.caf(_u(o=10)) is None  # no input/cache_write → undefined


def test_grade_thresholds():
    assert metrics.grade(2.718) == "优秀"
    assert metrics.grade(1.490) == "良好"
    assert metrics.grade(0.925) == "中等"
    assert metrics.grade(0.426) == "低效"
    assert metrics.grade(0.044) == "极端低效"
    assert metrics.grade(None) is None


def test_compute_populates_composite_layer():
    # End-to-end: compute() fills NCPI / CAF / TA-TCER / PSAC / CTEI when given
    # loc_accumulated + task_type. total = 1Mt, net_loc=500 → TCER=500.
    u = _u(i=400_000, cw=100_000, o=500_000)  # total 1,000,000
    r = metrics.compute(META, u, net_loc=500, loc_accumulated=10_000, task_type="debug")
    assert r.tcer == pytest_approx(500.0)
    assert r.ncpi == pytest_approx(500 / 10_000)
    assert r.ta_tcer == pytest_approx(500.0 / 0.40)
    assert r.psac is not None and r.tcer_phase_adj == pytest_approx(r.tcer * r.psac)
    assert r.caf == pytest_approx(500_000 / 500_000)  # total_input / (input+cacheW)
    assert r.ctei is not None and r.grade is not None
    assert r.task_type == "debug"


def test_compute_composite_none_without_loc_accumulated():
    # No loc_accumulated → NCPI/PSAC/CTEI stay None, but CAF (token-only) still set.
    u = _u(i=400_000, cw=100_000, o=500_000)
    r = metrics.compute(META, u, net_loc=500, task_type="feature")
    assert r.ncpi is None
    assert r.psac is None
    assert r.ctei is None
    assert r.caf is not None  # CAF needs only token usage


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


