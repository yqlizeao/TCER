"""TCER core metric formulas and pricing.

Basic formulas follow CLAUDE.md. The composite layer (L5) — TTAF / TA-TCER /
PSAC / CAF / CTEI — follows the research report (§6.2–6.5), which is the
authoritative original framework.

Costs are priced per model via ``pricing`` (each model's tokens at its own
$/MTok rate), falling back to the Anthropic list-price ``default`` for unknown
or mixed-model usage; see ``cost_usd``.
"""
from __future__ import annotations

from . import pricing
from .models import SessionMeta, SessionReport, TokenUsage

# Fallback $/MTok rates for unknown / mixed-model usage. Mirrors the ``default``
# block of ``data/model_pricing.json`` (Anthropic generic list price; cache read
# is 1/10 of input — CHR matters a lot). Per-model rates come from that config
# via ``pricing.resolve``; see ``cost_usd`` below.
PRICING = pricing.default_pricing()

# --------------------------------------------------------------------------- #
# Composite-layer constants (authoritative source: research report §6)
# --------------------------------------------------------------------------- #
# CTEI baselines = medians (TCER, CPE) / mean (NCPI) of the report's 16-session
# reference dataset. Defaults keep CTEI on the same scale as the published
# report so scores are directly comparable; overridable (report §8.3 — build a
# personal baseline DB from your own accumulated sessions).
TCER_BASELINE = 76.59  # dataset median TCER (LOC/Mt)
NCPI_BASELINE = 0.101  # dataset expected NCPI (contribution density)
CPE_BASELINE = 8.22    # dataset median CPE ($/kLOC)

# Task Type Adjustment Factor (report §6.4, the authoritative source).
TTAF = {
    "feature": 1.00,      # 新功能开发 greenfield（基准）
    "feature-ext": 0.85,  # 功能扩展 existing codebase
    "debug": 0.40,        # Bug 调试 / fix
    "refactor": 0.50,     # 重构
    "review": 0.20,       # 代码审查
    "test": 0.90,         # 测试编写
}

# Project-stage regression (report §6.5): TCER ≈ -0.000866 * LOC_accum + 83.64
PSAC_INTERCEPT = 83.64
PSAC_SLOPE = 0.000866


def _cost_from(o, r: dict[str, float]) -> float:
    """USD cost of one token record ``o`` at rate map ``r`` (TokenUsage or ModelUsage)."""
    return (
        o.input_tokens * r["input"]
        + o.cache_creation_input_tokens * r["cache_write"]
        + o.cache_read_input_tokens * r["cache_read"]
        + o.output_tokens * r["output"]
    ) / 1_000_000


def _rates_for(u: TokenUsage, model: str | None) -> dict[str, float]:
    """Pick the $/MTok rate map for one usage record.

    Priority: explicit ``model`` arg -> the session's single model (when it used
    exactly one) -> ``default``. Only used as a fallback when per-model token
    buckets aren't available (e.g. synthetic usage); real sessions carry
    ``per_model`` and are priced model-by-model in ``cost_usd``.
    """
    if model:
        return pricing.resolve(model)
    if len(u.models) == 1:
        return pricing.resolve(next(iter(u.models)))
    return PRICING


def cost_by_model(u: TokenUsage) -> dict[str, float]:
    """USD cost broken down per model, each bucket priced at its own rate.

    Key is the model id (``""`` for turns with no model recorded, priced at
    ``default``). Empty when the usage carries no per-model buckets.
    """
    return {mid: _cost_from(mu, pricing.resolve(mid)) for mid, mu in u.per_model.items()}


def cost_usd(u: TokenUsage, model: str | None = None) -> float:
    """Estimate USD cost at vendor list price (not subscription billing).

    Each model's tokens are priced at that model's own rate and summed, so
    mixed-model sessions are exact. An explicit ``model`` forces every token onto
    that model's rate. Falls back to a single resolved rate only when no
    per-model buckets exist (synthetic usage) — unknown / mixed there default to
    Anthropic list price.
    """
    if model is None and u.per_model:
        return sum(cost_by_model(u).values())
    return _cost_from(u, _rates_for(u, model))


# --------------------------------------------------------------------------- #
# Composite-layer formulas
# --------------------------------------------------------------------------- #
def caf(u: TokenUsage) -> float | None:
    """Cache Adjustment Factor = TotalInput / (input + cache_write).

    >= 1; higher means more of the input was cheap cache reads. None if denom 0.
    """
    denom = u.input_tokens + u.cache_creation_input_tokens
    return (u.total_input / denom) if denom else None


def ncpi(net_loc: int | None, loc_accumulated: int | None) -> float | None:
    """Net Code Production Index = net_loc / accumulated codebase LOC."""
    if net_loc is None or not loc_accumulated:
        return None
    return net_loc / loc_accumulated


def ta_tcer(tcer: float | None, task_type: str | None) -> float | None:
    """Task-adjusted TCER = TCER / TTAF_task (report §6.4)."""
    factor = TTAF.get(task_type or "")
    if tcer is None or not factor:
        return None
    return tcer / factor


def psac(loc_accumulated: int | None) -> float | None:
    """Project-Stage Adjustment Coefficient (report §6.5).

    PSAC = intercept / (intercept - slope * LOC_current). Multiply TCER by this
    to neutralize the structural TCER decline of larger codebases.
    """
    if loc_accumulated is None:
        return None
    denom = PSAC_INTERCEPT - PSAC_SLOPE * loc_accumulated
    return (PSAC_INTERCEPT / denom) if denom else None


def chr_factor(chr_: float | None) -> float:
    """CHR reward factor = 1 + CHR*0.5 (report §6.3): +10% CHR → +5% CTEI."""
    return 1.0 + (chr_ or 0.0) * 0.5


def churn_ratio(added: int | None, deleted: int | None) -> float | None:
    """L3 code churn = deleted / added (fraction of written lines later removed).

    0 = pure additions (no rework); higher = more rework / lower-quality output.
    None if no lines were added. Report §6.1 lists churn as the first L3 signal,
    guarding against "high-LOC low-quality" pseudo-efficiency.
    """
    if not added:
        return None
    return (deleted or 0) / added


def ctei(
    tcer: float | None,
    ncpi_: float | None,
    cpe: float | None,
    chr_: float | None,
    *,
    tcer_baseline: float = TCER_BASELINE,
    ncpi_baseline: float = NCPI_BASELINE,
    cpe_baseline: float = CPE_BASELINE,
) -> float | None:
    """Composite Token Efficiency Index (report §6.3).

    CTEI = (TCER/baseline) × (NCPI/baseline) × (CPE_baseline/CPE) × (1+CHR*0.5)
    Reproduces the report's published per-session scores to <0.1%.
    """
    if tcer is None or ncpi_ is None or not cpe:
        return None
    return (
        (tcer / tcer_baseline)
        * (ncpi_ / ncpi_baseline)
        * (cpe_baseline / cpe)
        * chr_factor(chr_)
    )


def grade(ctei_: float | None) -> str | None:
    """CTEI rating (report §6.3 thresholds)."""
    if ctei_ is None:
        return None
    if ctei_ > 2.0:
        return "优秀"
    if ctei_ >= 1.0:
        return "良好"
    if ctei_ >= 0.5:
        return "中等"
    if ctei_ >= 0.1:
        return "低效"
    return "极端低效"


def compute(
    meta: SessionMeta,
    u: TokenUsage,
    net_loc: int | None,
    *,
    loc_accumulated: int | None = None,
    task_type: str | None = None,
    code_added: int | None = None,
    code_deleted: int | None = None,
    tcer_baseline: float = TCER_BASELINE,
    ncpi_baseline: float = NCPI_BASELINE,
    cpe_baseline: float = CPE_BASELINE,
) -> SessionReport:
    """Compute the full per-session report from accumulated usage + net LOC.

    Composite-layer fields (NCPI / CAF / TA-TCER / PSAC / CTEI / grade) and the L3
    churn ratio are filled in opportunistically: each is None unless its inputs
    are available.
    """
    total_input = u.total_input
    total = u.total

    chr_ = (u.cache_read_input_tokens / total_input) if total_input else None
    io_ratio = (total_input / u.output_tokens) if u.output_tokens else None
    cost = cost_usd(u)
    cost_per_mt = (cost / (total / 1_000_000)) if total else None

    tcer: float | None = None
    cpe: float | None = None
    if net_loc is not None and total:
        total_mt = total / 1_000_000
        tcer = net_loc / total_mt if total_mt else None
        cpe = (cost / net_loc * 1000) if net_loc > 0 else None

    # --- composite layer ---
    ncpi_ = ncpi(net_loc, loc_accumulated)
    caf_ = caf(u)
    ta = ta_tcer(tcer, task_type)
    psac_ = psac(loc_accumulated)
    tcer_phase = (tcer * psac_) if (tcer is not None and psac_ is not None) else None
    ctei_ = ctei(tcer, ncpi_, cpe, chr_, tcer_baseline=tcer_baseline,
                 ncpi_baseline=ncpi_baseline, cpe_baseline=cpe_baseline)

    return SessionReport(
        meta=meta,
        usage=u,
        chr=chr_,
        io_ratio=io_ratio,
        cost=cost,
        cost_per_mt=cost_per_mt,
        net_loc=net_loc,
        tcer=tcer,
        cpe=cpe,
        loc_accumulated=loc_accumulated,
        ncpi=ncpi_,
        caf=caf_,
        task_type=task_type,
        ta_tcer=ta,
        psac=psac_,
        tcer_phase_adj=tcer_phase,
        ctei=ctei_,
        grade=grade(ctei_),
        code_added=code_added,
        code_deleted=code_deleted,
        churn_ratio=churn_ratio(code_added, code_deleted),
    )
