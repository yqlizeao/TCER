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

# 向后兼容：TASK_TYPES 映射到 TASK_CATEGORIES
TASK_TYPES = TASK_CATEGORIES


def get_task_category(task_type: str) -> str | None:
    """获取任务类型所属的大类（现在 task_type 本身就是大类）"""
    return task_type if task_type in TASK_CATEGORIES else None


def get_task_ttaf(task_type: str) -> float | None:
    """获取任务类型的 TTAF 系数"""
    category_info = TASK_CATEGORIES.get(task_type)
    return category_info["ttaf"] if category_info else None


# ============================================================
# 智能推荐算法（只推荐 3 个大类）
# ============================================================

RECOMMENDATION_RULES = [
    # 规则 1: 非编码任务（高读写比 + 低净增行）
    {
        "condition": lambda f: f["read_write_ratio"] > 5 and f["net_loc"] < 10,
        "task_type": "non_coding",
        "confidence": 0.85,
        "reason": "读写比 > 5 且净增行 < 10",
    },
    # 规则 2: 代码维护（高探索率 + 低净增行 + 高编辑占比）
    {
        "condition": lambda f: (
            f["exploration_ratio"] > 0.4 and
            f["net_loc"] < 50 and
            f["edit_ratio"] > 0.5
        ),
        "task_type": "code_maintenance",
        "confidence": 0.8,
        "reason": "高探索率 + 低净增行 + 高编辑占比",
    },
    # 规则 3: 代码创作（高净增行）
    {
        "condition": lambda f: f["net_loc"] > 100,
        "task_type": "code_creation",
        "confidence": 0.7,
        "reason": "高净增行 > 100",
    },
]


def extract_behavior_features(report: SessionReport) -> dict:
    """提取会话的行为特征用于智能推荐"""
    usage = report.usage

    # 计算关键比率
    total_tools = sum(usage.tool_calls.values())
    exploration_ratio = (
        (usage.tool_calls.get("Grep", 0) + usage.tool_calls.get("Glob", 0)) / total_tools
        if total_tools > 0
        else 0.0
    )

    edit_count = usage.tool_calls.get("Edit", 0) + usage.tool_calls.get("MultiEdit", 0)
    write_count = usage.tool_calls.get("Write", 0)
    read_count = usage.tool_calls.get("Read", 0)

    edit_ratio = edit_count / (edit_count + write_count) if (edit_count + write_count) > 0 else 0.0
    read_write_ratio = read_count / (edit_count + write_count) if (edit_count + write_count) > 0 else 0.0

    # 计算净增行相关
    net_loc = (report.code_added or 0) - (report.code_deleted or 0)
    deletion_ratio = report.code_deleted / report.code_added if report.code_added and report.code_added > 0 else 0.0

    # 计算测试代码占比
    test_loc_ratio = report.test_net_loc / net_loc if net_loc and net_loc > 0 and report.test_net_loc else 0.0

    return {
        "exploration_ratio": exploration_ratio,
        "edit_ratio": edit_ratio,
        "read_write_ratio": read_write_ratio,
        "net_loc": net_loc,
        "deletion_ratio": deletion_ratio,
        "test_loc_ratio": test_loc_ratio,
        "turns": usage.effective_turns,
    }


def recommend_task_type(features: dict) -> dict:
    """推荐任务类型（返回最佳匹配）"""
    best_match = None

    for rule in RECOMMENDATION_RULES:
        try:
            if rule["condition"](features):
                if best_match is None or rule["confidence"] > best_match["confidence"]:
                    best_match = {
                        "task_type": rule["task_type"],
                        "confidence": rule["confidence"],
                        "reason": rule["reason"],
                    }
        except (KeyError, ZeroDivisionError):
            continue

    # 如果没有匹配，默认推荐 code_creation
    if best_match is None:
        best_match = {
            "task_type": "code_creation",
            "confidence": 0.5,
            "reason": "无明显特征，默认推荐代码创作",
        }

    return best_match


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
    # 兼容旧配置（直接读 ttaf）和新配置（读 task_categories 中的 ttaf）
    config = _load_composite_config()
    if "ttaf" in config:
        # 旧格式
        return {k: v for k, v in config["ttaf"].items() if not k.startswith("_")}
    elif "task_types" in config:
        # 中间格式
        return {k: v["ttaf"] for k, v in config["task_types"].items()}
    elif "task_categories" in config:
        # 新格式（3 个大类）
        return {k: v["ttaf"] for k, v in config["task_categories"].items() if isinstance(v, dict) and "ttaf" in v}
    return {}


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

    search_edit_ratio: fraction of Grep/Glob calls (with file_path) that are
    followed by an Edit/Write to the same file within 3 assistant turns.
    Pure exploration (searches with no file_path) is excluded from the count.
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

    # Search-edit ratio: searches with file_path that lead to edit within WINDOW turns
    searches_with_path = 0
    searches_with_edit = 0
    for op in u.tool_ops:
        if op.tool not in _SEARCH or not op.path:
            continue
        searches_with_path += 1
        edit_deadline = op.turn + WINDOW
        for other_turn, other_tool in file_ops.get(op.path, []):
            if other_tool in _WRITE_EDIT and op.turn < other_turn <= edit_deadline:
                searches_with_edit += 1
                break
    ste = (searches_with_edit / searches_with_path) if searches_with_path else None

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
    factor = TASK_TYPES.get(task_type, {}).get("ttaf")
    if not factor:
        return None
    return tcer / factor


# Backward compat alias
ta_tcer = normalized_tcer


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


def churn_ratio(added: int | None, deleted: int | None) -> float | None:
    """G4 code churn = deleted / added (fraction of written lines later removed).

    0 = pure additions (no rework); higher = more rework / lower-quality output.
    None if no lines were added. Report §6.1 lists churn as the first quality signal,
    guarding against "high-LOC low-quality" pseudo-efficiency.
    """
    if not added:
        return None
    if deleted is None:
        return None
    return deleted / added


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


def grade(ctei_: float | None) -> str | None:
    """CTEI rating (framework §6.3 thresholds)."""
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
        churn_ratio=churn_ratio(code_added, code_deleted),
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
    )
