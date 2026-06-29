"""TCER core metric formulas and pricing.

Basic formulas follow CLAUDE.md. The 综合评分 group (G6) — TTAF / NTCER /
PSAC / CAF / CTEI — follows the metric framework (§6.2–6.5), which is the
authoritative original framework.

Costs are priced per model via ``pricing`` (each model's tokens at its own
$/MTok rate), falling back to the Anthropic list-price ``default`` for unknown
or mixed-model usage; see ``cost_usd``.

Composite-layer constants (TTAF, CTEI baselines, PSAC regression, CHR weight)
are loaded from ``config/composite_baselines.json`` — a hand-editable config so
you can override the framework's reference-dataset defaults with your own
accumulated data.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tcer.core import pricing
from tcer.core.models import SessionMeta, SessionReport, TokenUsage

# Fallback $/MTok rates for unknown / mixed-model usage. Mirrors the ``default``
# block of ``data/model_pricing.json`` (Anthropic generic list price; cache read
# is 1/10 of input — CHR matters a lot). Per-model rates come from that config
# via ``pricing.resolve``; see ``cost_usd`` below.
PRICING = pricing.default_pricing()

_COMPOSITE_CONFIG_PATH = Path(__file__).parent.parent / "config" / "composite_baselines.json"


# ============================================================
# 任务类型体系（只保留 3 个大类）
# ============================================================

TASK_CATEGORIES = {
    "code_creation": {
        "name": "代码创作",
        "description": "产生新代码的任务（新功能开发、功能扩展、测试编写等）",
        "ttaf": 1.0,
        "typical_tcer_range": "60-120",
        "behavior_hints": ["高 net_loc", "低 exploration_ratio", "低 edit_ratio（多用 Write）"],
    },
    "code_maintenance": {
        "name": "代码维护",
        "description": "修改/优化现有代码（调试排查、代码重构等）",
        "ttaf": 0.45,
        "typical_tcer_range": "25-65",
        "behavior_hints": ["高 exploration_ratio", "高 edit_ratio", "低 net_loc"],
    },
    "non_coding": {
        "name": "非编码",
        "description": "不以代码产出为主要目标（代码审查、调研研究等）",
        "ttaf": 0.2,
        "typical_tcer_range": "0-30",
        "behavior_hints": ["极高 read_write_ratio", "极高 exploration_ratio", "极低或零 net_loc"],
    },
}


def get_task_category(task_type: str) -> str | None:
    """获取任务类型所属的大类（现在 task_type 本身就是大类）"""
    return task_type if task_type in TASK_CATEGORIES else None


def get_task_ttaf(task_type: str) -> float | None:
    """获取任务类型的 TTAF 系数"""
    category_info = TASK_CATEGORIES.get(task_type)
    return category_info["ttaf"] if category_info else None


# ============================================================
# Composite-layer config (backward compat)
# ============================================================

@lru_cache(maxsize=1)
def _load_composite_config() -> dict:
    """Load composite-layer config (TTAF / baselines / PSAC / CHR weight)."""
    with _COMPOSITE_CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# Composite-layer constants (loaded from config; expose module-level for backward compat).
def _get_ttaf() -> dict[str, float]:
    config = _load_composite_config()
    return {k: v["ttaf"] for k, v in config["task_categories"].items()
            if isinstance(v, dict) and "ttaf" in v}


def _get_baselines() -> dict[str, float]:
    return _load_composite_config()["ctei_baselines"]


def _get_psac_params() -> dict[str, float]:
    return _load_composite_config()["psac"]


def _get_chr_weight() -> float:
    return _load_composite_config()["chr_weight"]


# Module-level read-only views (for backward compat with existing callers that read these).
# Force reload from new config structure
_load_composite_config.cache_clear()
TTAF = _get_ttaf()
TCER_BASELINE = _get_baselines()["tcer"]
NCPI_BASELINE = _get_baselines()["ncpi"]
CPE_BASELINE = _get_baselines()["cpe"]
PSAC_INTERCEPT = _get_psac_params()["intercept"]
PSAC_SLOPE = _get_psac_params()["slope"]
CHR_WEIGHT = _get_chr_weight()


def compute_baselines(reports) -> dict | None:
    """Derive personal CTEI baselines (TCER/CPE median, NCPI mean) from sessions.

    Returns None if no session has complete TCER/NCPI/CPE data. Framework §8.3
    recommends building your own reference set once you have accumulated data.
    """
    import statistics
    valid = [r for r in reports
             if getattr(r, "tcer", None) is not None
             and getattr(r, "ncpi", None) is not None
             and getattr(r, "cpe", None) is not None]
    if not valid:
        return None
    return {
        "tcer": statistics.median(r.tcer for r in valid),
        "ncpi": statistics.mean(r.ncpi for r in valid),
        "cpe": statistics.median(r.cpe for r in valid),
    }


def save_baselines(values: dict) -> None:
    """Write personal CTEI baselines into ``composite_baselines.json`` and refresh.

    Merges into the existing ``ctei_baselines`` block, clears the config cache,
    and updates the module-level ``*_BASELINE`` constants so the next analysis
    picks them up. The caller (GUI) confirms before invoking.

    Writes atomically via a temp file + ``os.replace`` to avoid corruption on
    crash. Works on a shallow copy so the in-memory ``lru_cache`` is never
    mutated in-place.
    """
    # Shallow-copy the cached config so we don't mutate the lru_cache's dict.
    cfg = {**_load_composite_config()}
    cfg["ctei_baselines"] = {**cfg.get("ctei_baselines", {}), **values}
    # Atomic write: write to a sibling temp file, then replace.
    fd, tmp = tempfile.mkstemp(dir=_COMPOSITE_CONFIG_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, str(_COMPOSITE_CONFIG_PATH))
    except BaseException:
        # On any failure (incl. KeyboardInterrupt) remove the orphan temp file.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    _load_composite_config.cache_clear()

    global TCER_BASELINE, NCPI_BASELINE, CPE_BASELINE
    b = _get_baselines()
    TCER_BASELINE = b["tcer"]
    NCPI_BASELINE = b["ncpi"]
    CPE_BASELINE = b["cpe"]


# ============================================================
# 模型对比
# ============================================================

_SKIP_MODELS = {"<synthetic>", ""}


@dataclass
class ModelComparison:
    """Aggregated stats for one model across sessions."""
    model_id: str
    display_name: str
    # Token counts
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Cost
    cost: float = 0.0
    session_count: int = 0
    # Efficiency
    cache_hit_ratio: float | None = None
    tokens_per_dollar: float | None = None
    code_per_dollar: float | None = None  # net_loc / cost — 每美元换来多少行净代码
    token_share: float = 0.0
    cost_share: float = 0.0
    # 产出效率
    net_loc_per_session: float | None = None
    # 行为特征
    tool_error_rate: float | None = None
    exploration_ratio: float | None = None
    edit_ratio: float | None = None
    read_write_ratio: float | None = None
    # 代码质量
    churn_ratio: float | None = None
    read_before_write: float | None = None
    files_per_session: float | None = None
    # 内部累加器
    _primary_count: int = 0  # 主模型会话数（>50% token），作产出/行为/质量指标的分母
    _rbw_sum: float = 0.0
    _rbw_count: int = 0
    _tool_calls: dict = None
    _tool_errors: int = 0
    _code_added: int = 0
    _code_deleted: int = 0
    _code_reworked: int = 0
    _net_loc: int = 0
    _files_touched: int = 0

    def __post_init__(self):
        if self._tool_calls is None:
            self._tool_calls = {}

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_tokens + self.cache_read_tokens


def compare_models(reports: list[SessionReport]) -> list[ModelComparison]:
    """Aggregate and compare models across sessions."""
    from tcer.core.pricing import label as model_label

    buckets: dict[str, ModelComparison] = {}
    for r in reports:
        u = r.usage
        for model_id, mu in u.per_model.items():
            if model_id in _SKIP_MODELS:
                continue
            mc = buckets.get(model_id)
            if mc is None:
                mc = ModelComparison(model_id=model_id, display_name=model_label(model_id))
                buckets[model_id] = mc
            mc.input_tokens += mu.input_tokens
            mc.output_tokens += mu.output_tokens
            mc.cache_creation_tokens += mu.cache_creation_input_tokens
            mc.cache_read_tokens += mu.cache_read_input_tokens
            mc.session_count += 1
            # 主模型会话（该模型占该会话 >50% token）才统计产出/行为/质量
            mu_total = mu.input_tokens + mu.output_tokens + mu.cache_creation_input_tokens + mu.cache_read_input_tokens
            is_primary = u.total > 0 and mu_total / u.total > 0.5
            if is_primary:
                mc._primary_count += 1
                # 行为特征 (仅按主模型会话统计)
                for tool, cnt in u.tool_calls.items():
                    mc._tool_calls[tool] = mc._tool_calls.get(tool, 0) + cnt
                mc._tool_errors += u.tool_errors
                mc._code_added += r.code_added or 0
                mc._code_deleted += r.code_deleted or 0
                # Mirror compute(): self-rework count, falling back to gross
                # deletions when a session predates the code_reworked field.
                reworked = r.code_reworked if r.code_reworked is not None else r.code_deleted
                mc._code_reworked += reworked or 0
                mc._net_loc += r.net_loc or 0
                mc._files_touched += r.files_touched or 0
                if r.read_before_write is not None:
                    mc._rbw_sum += r.read_before_write
                    mc._rbw_count += 1

    # Compute derived metrics
    grand_tokens = sum(mc.total_tokens for mc in buckets.values())
    grand_cost = 0.0
    for mc in buckets.values():
        mc.cost = cost_usd(
            _FakeModelUsage(mc.input_tokens, mc.output_tokens,
                            mc.cache_creation_tokens, mc.cache_read_tokens),
            model=mc.model_id)
        grand_cost += mc.cost
        total_input = mc.input_tokens + mc.cache_creation_tokens + mc.cache_read_tokens
        mc.cache_hit_ratio = mc.cache_read_tokens / total_input if total_input > 0 else None
        mc.tokens_per_dollar = mc.total_tokens / mc.cost if mc.cost > 0 else None
        mc.code_per_dollar = mc._net_loc / mc.cost if mc.cost > 0 else None
        mc.token_share = mc.total_tokens / grand_tokens * 100 if grand_tokens else 0
        # 产出效率
        mc.net_loc_per_session = mc._net_loc / mc._primary_count if mc._primary_count > 0 else None
        # 行为特征
        total_tools = sum(mc._tool_calls.values())
        if total_tools > 0:
            grep_glob = mc._tool_calls.get("Grep", 0) + mc._tool_calls.get("Glob", 0)
            mc.exploration_ratio = grep_glob / total_tools
            edit_write = mc._tool_calls.get("Edit", 0) + mc._tool_calls.get("Write", 0)
            mc.edit_ratio = mc._tool_calls.get("Edit", 0) / edit_write if edit_write > 0 else None
            mc.read_write_ratio = mc._tool_calls.get("Read", 0) / edit_write if edit_write > 0 else None
            mc.tool_error_rate = mc._tool_errors / total_tools
        # 代码质量 (self-rework, consistent with compute()/SessionReport.churn_ratio)
        mc.churn_ratio = mc._code_reworked / mc._code_added if mc._code_added > 0 else None
        mc.read_before_write = mc._rbw_sum / mc._rbw_count if mc._rbw_count > 0 else None
        mc.files_per_session = mc._files_touched / mc._primary_count if mc._primary_count > 0 else None
    for mc in buckets.values():
        mc.cost_share = mc.cost / grand_cost * 100 if grand_cost else 0

    return sorted(buckets.values(), key=lambda mc: mc.total_tokens, reverse=True)


class _FakeModelUsage:
    """Lightweight stand-in for ModelUsage (avoids importing models.py)."""
    __slots__ = ("input_tokens", "output_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self, i, o, cw, cr):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = cw
        self.cache_read_input_tokens = cr



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
# New metrics: timing, tool usage, context efficiency
# --------------------------------------------------------------------------- #
def avg_turn_latency_sec(u: TokenUsage) -> float | None:
    """Average latency per effective assistant turn (seconds). Includes user pauses."""
    if u.started_at and u.ended_at and u.effective_turns:
        return (u.ended_at - u.started_at) / 1000 / u.effective_turns
    return None


def tool_usage_metrics(u: TokenUsage) -> dict[str, float | None]:
    """Read/Write ratio, Edit ratio, exploration density."""
    read = u.tool_calls.get("Read", 0)
    write = u.tool_calls.get("Write", 0)
    edit = u.tool_calls.get("Edit", 0)
    grep = u.tool_calls.get("Grep", 0)
    glob = u.tool_calls.get("Glob", 0)
    total_tools = sum(u.tool_calls.values())

    return {
        "read_write_ratio": read / (write + edit) if (write + edit) else None,
        "edit_ratio": edit / (edit + write) if (edit + write) else None,
        "exploration_ratio": (grep + glob) / total_tools if total_tools else None,
    }


def cache_efficiency(u: TokenUsage) -> float | None:
    """Cache read / write ratio (>1 means cache paid off)."""
    cw = u.cache_creation_input_tokens
    return (u.cache_read_input_tokens / cw) if cw else None


def file_quality_metrics(u: TokenUsage) -> dict[str, float | None]:
    """Temporal search-edit and read-before-write analysis.

    search_edit_ratio: fraction of Grep/Glob calls that are *followed* by a
    Write/Edit/MultiEdit within ``WINDOW`` assistant turns. This is turn-based,
    not file-based: real Grep/Glob carry a ``path`` that is usually a directory
    (or no path at all for a repo-wide search), so matching a search to the exact
    file later edited is unreliable. Measuring follow-through in *time* captures
    the intended workflow signal — "did searching lead to a change soon after, or
    was it dead-end exploration?" — and works on real Claude Code data.
    read_before_write: fraction of Write/Edit targets where the same file was
    Read in a previous turn.
    """
    from collections import defaultdict

    _WRITE_EDIT = {"Write", "Edit", "MultiEdit"}
    _SEARCH = {"Grep", "Glob"}
    WINDOW = 3

    # Group operations by file, preserving turn order
    file_ops: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for op in u.tool_ops:
        if op.path:
            file_ops[op.path].append((op.turn, op.tool))

    # Read-before-write: for each file, was there a Read before the first Write/Edit?
    write_edit_files = 0
    read_first_files = 0
    for ops in file_ops.values():
        first_write_turn = None
        has_prior_read = False
        for turn, tool in ops:
            if tool == "Read" and first_write_turn is None:
                has_prior_read = True
            elif tool in _WRITE_EDIT:
                if first_write_turn is None:
                    first_write_turn = turn
                    write_edit_files += 1
                    if has_prior_read:
                        read_first_files += 1
                    break
    rbw = (read_first_files / write_edit_files) if write_edit_files else None

    # Search-edit ratio: a search (Grep/Glob) is "productive" if any Write/Edit
    # happens within WINDOW turns after it. Path-agnostic (see docstring).
    edit_turns = sorted({op.turn for op in u.tool_ops if op.tool in _WRITE_EDIT})
    searches = 0
    searches_with_edit = 0
    for op in u.tool_ops:
        if op.tool not in _SEARCH:
            continue
        searches += 1
        if any(op.turn < et <= op.turn + WINDOW for et in edit_turns):
            searches_with_edit += 1
    ste = (searches_with_edit / searches) if searches else None

    return {
        "search_edit_ratio": ste,
        "read_before_write": rbw,
    }


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


def normalized_tcer(tcer: float | None, task_type: str | None) -> float | None:
    """Normalized TCER (NTCER) = TCER / TTAF_task.

    Removes the task-type bias so different task types can be compared fairly.
    For example: debug TCER=30, TTAF=0.4, NTCER=75 — showing the efficiency
    is actually good for a debugging task.
    """
    if tcer is None or not task_type:
        return None
    factor = TASK_CATEGORIES.get(task_type, {}).get("ttaf")
    if not factor:
        return None
    return tcer / factor


def psac(loc_accumulated: int | None) -> float | None:
    """Project-Stage Adjustment Coefficient (framework §6.5).

    PSAC = intercept / (intercept - slope * LOC_current). Multiply TCER by this
    to neutralize the structural TCER decline of larger codebases.
    """
    if loc_accumulated is None:
        return None
    denom = PSAC_INTERCEPT - PSAC_SLOPE * loc_accumulated
    return (PSAC_INTERCEPT / denom) if denom else None


def chr_factor(chr_: float | None) -> float:
    """CHR reward factor = 1 + CHR*weight (framework §6.3): +10% CHR → +5% CTEI (default weight)."""
    return 1.0 + (chr_ or 0.0) * CHR_WEIGHT


def churn_ratio(added: int | None, reworked: int | None) -> float | None:
    """G4 self-rework rate = reworked / added.

    ``reworked`` is the count of written lines the model later deleted *within the
    same session* — i.e. it wrote them and then removed/replaced them. Deleting
    pre-existing code (a normal edit) is NOT rework and is excluded by the caller
    (see ``loc.session_loc_full``'s ``rework_deleted``). 0 = wrote it right the
    first time; higher = more churning on its own output.

    None if no lines were added. Report §6.1 lists churn as the first quality signal,
    guarding against "high-LOC low-quality" pseudo-efficiency.
    """
    if not added:
        return None
    if reworked is None:
        return None
    return reworked / added


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
    """Composite Token Efficiency Index (framework §6.3).

    CTEI = (TCER/baseline) × (NCPI/baseline) × (CPE_baseline/CPE) × (1+CHR*0.5)
    Reproduces the framework's published per-session scores to <0.1%.
    """
    if tcer is None or ncpi_ is None or not cpe:
        return None
    return (
        (tcer / tcer_baseline)
        * (ncpi_ / ncpi_baseline)
        * (cpe_baseline / cpe)
        * chr_factor(chr_)
    )


# CTEI rating bands (framework §6.3), best → worst: ``(label, lower_bound)``.
# The top band is strictly greater-than its bound; the rest are ≥. Single source
# for the rating taxonomy — ``grade()`` and the GUI's ranking bar / trend bands
# all derive their names + thresholds from here.
GRADE_BANDS: list[tuple[str, float]] = [
    ("优秀", 2.0),
    ("良好", 1.0),
    ("中等", 0.5),
    ("低效", 0.1),
    ("极端低效", 0.0),
]


def grade(ctei_: float | None) -> str | None:
    """CTEI rating (framework §6.3 thresholds), derived from GRADE_BANDS."""
    if ctei_ is None:
        return None
    top_label, top_lo = GRADE_BANDS[0]
    if ctei_ > top_lo:
        return top_label
    for label, lo in GRADE_BANDS[1:]:
        if ctei_ >= lo:
            return label
    return GRADE_BANDS[-1][0]


def compute(
    meta: SessionMeta,
    u: TokenUsage,
    net_loc: int | None,
    *,
    loc_accumulated: int | None = None,
    task_type: str | None = None,
    code_added: int | None = None,
    code_deleted: int | None = None,
    code_reworked: int | None = None,
    high_churn_files: int = 0,
    test_net_loc: int | None = None,
    doc_net_loc: int | None = None,
    tcer_baseline: float = TCER_BASELINE,
    ncpi_baseline: float = NCPI_BASELINE,
    cpe_baseline: float = CPE_BASELINE,
) -> SessionReport:
    """Compute the full per-session report from accumulated usage + net LOC.

    Composite fields (NCPI / CAF / NTCER / PSAC / CTEI / grade) and the
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
    ta = normalized_tcer(tcer, task_type)
    psac_ = psac(loc_accumulated)
    tcer_phase = (tcer * psac_) if (tcer is not None and psac_ is not None) else None
    ctei_ = ctei(tcer, ncpi_, cpe, chr_, tcer_baseline=tcer_baseline,
                 ncpi_baseline=ncpi_baseline, cpe_baseline=cpe_baseline)

    # --- task type info ---
    task_category = get_task_category(task_type) if task_type else None
    ttaf_value = get_task_ttaf(task_type) if task_type else None

    # --- timing metrics ---
    avg_turn_lat = avg_turn_latency_sec(u)
    session_dur_min = (u.session_duration_ms / 60000) if u.session_duration_ms else None

    # --- tool usage pattern ---
    tool_m = tool_usage_metrics(u)
    subagent_dens = None  # Will be filled by caller when subagent_count is available

    # --- context efficiency ---
    cache_eff = cache_efficiency(u)
    cache_wr = u.cache_creation_input_tokens / total_input if total_input else None
    non_cached = u.input_tokens / total_input if total_input else None

    # --- file-level quality ---
    test_ratio = test_net_loc / net_loc if (net_loc and net_loc > 0 and test_net_loc is not None) else None
    doc_ratio = doc_net_loc / net_loc if (net_loc and net_loc > 0 and doc_net_loc is not None) else None

    # --- new quality metrics ---
    total_tools = sum(u.tool_calls.values())
    tool_err_rate = u.tool_errors / total_tools if total_tools else None
    ttft_sec = (u.time_to_first_token_ms / 1000) if u.time_to_first_token_ms else None
    task_completion = (
        u.completed_task_count / u.task_count
        if u.task_count else None
    )
    patch_success = (
        u.patch_apply_success_count / u.patch_apply_count
        if u.patch_apply_count else None
    )
    context_window_ratio = (
        u.total_input / u.model_context_window
        if u.model_context_window else None
    )
    reasoning_ratio = (
        u.reasoning_output_tokens / u.output_tokens
        if u.output_tokens else None
    )
    # Derive files_touched from tool_ops
    touched: set[str] = set()
    ftd: dict[str, int] = {}
    for op in u.tool_ops:
        if op.path:
            touched.add(op.path)
            ftd[op.path] = ftd.get(op.path, 0) + 1
    fq = file_quality_metrics(u)

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
        task_category=task_category,
        ttaf=ttaf_value,
        ntcer=ta,
        ta_tcer=ta,  # backward compat
        psac=psac_,
        tcer_phase_adj=tcer_phase,
        ctei=ctei_,
        grade=grade(ctei_),
        code_added=code_added,
        code_deleted=code_deleted,
        code_reworked=code_reworked,
        churn_ratio=churn_ratio(
            code_added,
            code_reworked if code_reworked is not None else code_deleted,
        ),
        # --- timing ---
        avg_turn_latency_sec=avg_turn_lat,
        session_duration_minutes=session_dur_min,
        # --- tool usage ---
        read_write_ratio=tool_m["read_write_ratio"],
        edit_ratio=tool_m["edit_ratio"],
        exploration_ratio=tool_m["exploration_ratio"],
        subagent_density=subagent_dens,
        # --- context efficiency ---
        cache_efficiency=cache_eff,
        cache_write_ratio=cache_wr,
        non_cached_input_ratio=non_cached,
        # --- file-level quality ---
        high_churn_file_count=high_churn_files,
        test_net_loc=test_net_loc,
        doc_net_loc=doc_net_loc,
        test_loc_ratio=test_ratio,
        doc_loc_ratio=doc_ratio,
        # --- new quality metrics ---
        tool_error_rate=tool_err_rate,
        files_touched=len(touched),
        files_touched_details=ftd if ftd else None,
        thinking_count=u.thinking_count,
        search_edit_ratio=fq["search_edit_ratio"],
        read_before_write=fq["read_before_write"],
        time_to_first_token_sec=ttft_sec,
        task_completion_rate=task_completion,
        patch_apply_success_rate=patch_success,
        context_window_used_ratio=context_window_ratio,
        reasoning_output_ratio=reasoning_ratio,
    )
