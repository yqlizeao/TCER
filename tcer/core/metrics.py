"""TCER core metric formulas and pricing.

Basic formulas follow CLAUDE.md. The 综合评分 group (G6) — TTAF / NTCER /
CAF / 综合效率分 v2 (three orthogonal axes) — follows the metric framework
(§6.2–6.5) plus the v2 scoring model (see efficiency_score).

Costs are priced per model via ``pricing`` (each model's tokens at its own
$/MTok rate), falling back to the Anthropic list-price ``default`` for unknown
or mixed-model usage; see ``cost_usd``.

Composite-layer constants (TTAF, TCER/CPE baselines, score weights)
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
# Composite-layer config (SSOT: config/composite_baselines.json)
# ============================================================

@lru_cache(maxsize=1)
def _load_composite_config() -> dict:
    """Load composite-layer config (task categories / baselines / CHR weight)."""
    with _COMPOSITE_CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _get_task_categories() -> dict[str, dict]:
    """Task categories from config — single source of truth for names / TTAF / hints."""
    raw = _load_composite_config().get("task_categories") or {}
    out: dict[str, dict] = {}
    for key, val in raw.items():
        if isinstance(val, dict) and "ttaf" in val:
            out[key] = val
    return out


def _get_ttaf() -> dict[str, float]:
    return {k: float(v["ttaf"]) for k, v in _get_task_categories().items()}


def _get_baselines() -> dict[str, float]:
    return _load_composite_config()["baselines"]


def _get_baselines_per_project() -> dict[str, dict]:
    """逐项目个人基准（可选）。键为项目 uid（views.ref_uid），值 {tcer,cpe}。
    缺失时返回空 dict——回退到全局 baselines。"""
    return _load_composite_config().get("baselines_per_project", {}) or {}


def resolve_baselines(project_uid: str | None = None) -> dict[str, float]:
    """解析某项目实际生效的 TCER/CPE 基准：优先逐项目基准，否则回退全局。

    project_uid=None 或该项目无逐项目基准 → 全局 baselines。供 GUI reanalyze
    按当前项目取 override，让「逐项目基准」在打分时真正生效。
    """
    glob = _get_baselines()
    out = {"tcer": glob["tcer"], "cpe": glob["cpe"]}
    if project_uid:
        pp = _get_baselines_per_project().get(project_uid)
        if isinstance(pp, dict):
            if pp.get("tcer") is not None:
                out["tcer"] = float(pp["tcer"])
            if pp.get("cpe") is not None:
                out["cpe"] = float(pp["cpe"])
    return out


def _get_chr_weight() -> float:
    return _load_composite_config()["chr_weight"]


def _get_score_model() -> dict:
    """综合效率分 v2 配置块（基准/权重/收缩常数）。SSOT: composite_baselines.json。"""
    return _load_composite_config()["score_model"]


def _get_score_tier_bands() -> list[tuple[str, float]]:
    """评级带 (名称, 下界)，best→worst，从 config 派生。"""
    return [(name, float(lo)) for name, lo in _load_composite_config()["score_tiers"]["bands"]]


def _refresh_composite_globals() -> None:
    """Reload module-level constants from config (after cache clear / save)."""
    global TASK_CATEGORIES, TTAF, TCER_BASELINE, CPE_BASELINE, CHR_WEIGHT
    global SCORE_OUTPUT_BASELINE, SCORE_COST_BASELINE, SCORE_WEIGHTS
    global SCORE_SHRINK_K, SCORE_QUALITY_WEIGHTS, SCORE_TIER_BANDS
    TASK_CATEGORIES = _get_task_categories()
    TTAF = _get_ttaf()
    b = _get_baselines()
    TCER_BASELINE = b["tcer"]
    CPE_BASELINE = b["cpe"]
    CHR_WEIGHT = _get_chr_weight()
    sm = _get_score_model()
    # 两轴基准共用 baselines 块（TCER/CPE），个人基准一改即生效。
    SCORE_OUTPUT_BASELINE = TCER_BASELINE
    SCORE_COST_BASELINE = CPE_BASELINE
    SCORE_WEIGHTS = {k: float(v) for k, v in sm["weights"].items()}
    SCORE_SHRINK_K = float(sm["shrink_k"])
    SCORE_QUALITY_WEIGHTS = {k: float(v) for k, v in sm["quality_weights"].items()}
    # 评级带一并热重载（score_tiers.bands 可手工编辑）——否则 tier() 与已按
    # 新配置重算的分数脱节（规则 11 冻结陷阱的同族遗漏）。
    SCORE_TIER_BANDS = _get_score_tier_bands()


# Module-level views (backward compat). Always rebuild after cache clear via
# ``_refresh_composite_globals`` so TASK_CATEGORIES / TTAF stay in lockstep.
_load_composite_config.cache_clear()
TASK_CATEGORIES: dict[str, dict] = {}
TTAF: dict[str, float] = {}
TCER_BASELINE = 0.0
CPE_BASELINE = 0.0
CHR_WEIGHT = 0.0
# 综合效率分 v2 全局（随 config 重载；save_baselines 后重绑，勿用默认参数冻结）。
SCORE_OUTPUT_BASELINE = 0.0
SCORE_COST_BASELINE = 0.0
SCORE_WEIGHTS: dict[str, float] = {}
SCORE_SHRINK_K = 0.0
SCORE_QUALITY_WEIGHTS: dict[str, float] = {}
_refresh_composite_globals()

# Default task type for analysis when none / unknown is supplied.
DEFAULT_TASK_TYPE = "code_creation"

# Sentinel: analyze infers a category per session from tool / LOC signals.
AUTO_TASK_TYPE = "auto"

# Personal baselines need enough complete sessions to be stable.
MIN_BASELINE_SESSIONS = 5

# 基准离群过滤：净增行过少的会话，CPE = cost/net_loc*1000 会被放大到失真
# （实测 10 行改动 $17.6 → CPE 1763，正常区间 3–50）。这类近零产出会话不代表
# 「典型水平」，参与基准会污染成本轴中性点。低于此行数的会话不计入基准样本。
MIN_BASELINE_NET_LOC = 20

# Pre-v2 task type names still seen in tests / old callers → current keys.
_TASK_TYPE_ALIASES = {
    "feature": "code_creation",
}


def is_auto_task_type(task_type: str | None) -> bool:
    """True when the caller asked for per-session task-type inference."""
    if not task_type:
        return False
    t = task_type.strip().lower()
    return t in (AUTO_TASK_TYPE, "自动", "auto")


def resolve_task_type(task_type: str | None) -> str:
    """Return a valid ``TASK_CATEGORIES`` key for analysis orchestration.

    Empty / unknown values fall back to ``DEFAULT_TASK_TYPE`` so NTCER/TTAF
    never silently go missing when the GUI or a script omits the param.
    Legacy aliases (e.g. ``feature`` → ``code_creation``) are remapped.
    """
    if not task_type:
        return DEFAULT_TASK_TYPE if DEFAULT_TASK_TYPE in TASK_CATEGORIES else next(iter(TASK_CATEGORIES), "code_creation")
    if task_type in TASK_CATEGORIES:
        return task_type
    aliased = _TASK_TYPE_ALIASES.get(task_type)
    if aliased and aliased in TASK_CATEGORIES:
        return aliased
    return DEFAULT_TASK_TYPE if DEFAULT_TASK_TYPE in TASK_CATEGORIES else next(iter(TASK_CATEGORIES), "code_creation")


def coerce_task_type(task_type: str | None) -> str | None:
    """Normalize a task type for metric formulas without inventing a default.

    ``None`` stays ``None``. Known keys and legacy aliases map to a category key.
    Unknown non-empty strings stay invalid (``None``) so ``normalized_tcer`` can
    return ``None`` rather than silently applying the creation TTAF.
    """
    if task_type is None:
        return None
    if task_type in TASK_CATEGORIES:
        return task_type
    aliased = _TASK_TYPE_ALIASES.get(task_type)
    if aliased and aliased in TASK_CATEGORIES:
        return aliased
    return None


def get_task_category(task_type: str) -> str | None:
    """获取任务类型所属的大类（现在 task_type 本身就是大类）"""
    return coerce_task_type(task_type)


def get_task_ttaf(task_type: str) -> float | None:
    """获取任务类型的 TTAF 系数"""
    key = coerce_task_type(task_type)
    if key is None:
        return None
    category_info = TASK_CATEGORIES.get(key)
    return float(category_info["ttaf"]) if category_info else None


# Default thresholds for :func:`infer_task_type` (overridable via
# ``composite_baselines.json`` → ``task_inference``).
_INFER_DEFAULTS = {
    "tcer_low": 20.0,       # below → lean non_coding / maintenance
    "tcer_creation": 60.0,  # at/above → strong creation signal
    "exp_mid": 0.15,
    "exp_high": 0.40,
    "edit_write_heavy": 0.30,  # edit_ratio ≤ this → Write-heavy → creation
    "edit_maint": 0.60,        # edit_ratio ≥ this → maintenance
    "rwr_maint": 2.0,
    "rwr_noncoding": 5.0,
    # 扩展信号（均为可选输入，缺失时不参与打分）
    "doc_share_noncoding": 0.8,   # 文档行占净增 ≥ → 文档/调研类
    "test_share_maint": 0.6,      # 测试行占净增 ≥ → 测试补充（维护）
    "err_rate_maint": 0.15,       # 工具错误率 ≥ → 调试特征
    "bash_maint": 0.5,            # Bash 占比 ≥ → 运维/调试重 shell
    "web_noncoding": 3.0,         # 网页搜索次数 ≥ → 调研
}


def _infer_thresholds() -> dict[str, float]:
    """Merge config ``task_inference`` over hard-coded defaults."""
    cfg = _load_composite_config().get("task_inference") or {}
    out = dict(_INFER_DEFAULTS)
    for k, default in _INFER_DEFAULTS.items():
        if k in cfg:
            try:
                out[k] = float(cfg[k])
            except (TypeError, ValueError):
                out[k] = default
    return out


def infer_task_type(
    *,
    net_loc: int | None = None,
    total_tokens: int = 0,
    exploration_ratio: float | None = None,
    edit_ratio: float | None = None,
    read_write_ratio: float | None = None,
    test_net_loc: int | None = None,
    doc_net_loc: int | None = None,
    tool_error_rate: float | None = None,
    bash_ratio: float | None = None,
    web_search_count: int | None = None,
) -> str:
    """Heuristic task category from LOC + tool-behavior signals.

    Aligns with the three-way taxonomy in ``TASK_CATEGORIES``:

    - **code_creation** — material net LOC, low exploration, more Write than Edit
    - **code_maintenance** — modest net LOC, high exploration / Edit share
    - **non_coding** — little or no code output, heavy search/read

    Thresholds default to :data:`_INFER_DEFAULTS` and may be overridden in
    ``config/composite_baselines.json`` under ``task_inference``.

    ``net_loc=None`` means LOC was not measured (``no_loc``, or source without
    patch/summary signal) — **not** the same as zero output. Volume scoring is
    skipped so ``task_type=auto`` does not collapse everything to non_coding.

    Returns a key present in ``TASK_CATEGORIES`` (defaults to
    ``DEFAULT_TASK_TYPE`` if the table is empty). Not a classifier — a
    transparent scoring rule so NTCER is less wrong when the user picks「自动」.
    """
    th = _infer_thresholds()
    total = max(int(total_tokens or 0), 0)
    exp = float(exploration_ratio) if exploration_ratio is not None else None
    edit = float(edit_ratio) if edit_ratio is not None else None
    rwr = float(read_write_ratio) if read_write_ratio is not None else None

    scores = {
        "code_creation": 0.0,
        "code_maintenance": 0.0,
        "non_coding": 0.0,
    }

    # --- output volume (only when LOC is known) ---
    if net_loc is not None:
        net = int(net_loc)
        # Pseudo-TCER (net lines per MTok) — same scale as the metric.
        tcer_like = (
            (net / (total / 1_000_000.0)) if total > 0
            else (float("inf") if net > 0 else 0.0)
        )
        if net <= 0:
            scores["non_coding"] += 3.0
        elif tcer_like < th["tcer_low"]:
            scores["non_coding"] += 1.0
            scores["code_maintenance"] += 2.0
        elif tcer_like < th["tcer_creation"]:
            scores["code_maintenance"] += 1.5
            scores["code_creation"] += 1.0
        else:
            scores["code_creation"] += 3.0

    # --- exploration (Grep+Glob share) ---
    if exp is not None:
        if exp >= th["exp_high"]:
            scores["non_coding"] += 2.0
            scores["code_maintenance"] += 1.0
        elif exp >= th["exp_mid"]:
            scores["code_maintenance"] += 2.0
        else:
            scores["code_creation"] += 1.0

    # --- edit vs write ---
    if edit is not None:
        if edit >= th["edit_maint"]:
            scores["code_maintenance"] += 2.0
        elif edit <= th["edit_write_heavy"]:
            scores["code_creation"] += 1.5

    # --- read/write ratio ---
    if rwr is not None:
        if rwr >= th["rwr_noncoding"]:
            scores["non_coding"] += 2.0
        elif rwr >= th["rwr_maint"]:
            scores["code_maintenance"] += 1.0
        elif rwr < 1.0:
            scores["code_creation"] += 0.5

    # --- 扩展信号（保守低权重，只做倾斜不做定性） ---
    if net_loc is not None and net_loc > 0:
        if doc_net_loc is not None and doc_net_loc / net_loc >= th["doc_share_noncoding"]:
            scores["non_coding"] += 2.5   # 产出几乎全是文档 → 文档/调研
        if test_net_loc is not None and test_net_loc / net_loc >= th["test_share_maint"]:
            scores["code_maintenance"] += 1.0  # 测试补充多归维护
    if tool_error_rate is not None and tool_error_rate >= th["err_rate_maint"]:
        scores["code_maintenance"] += 1.0     # 高错误率 = 调试/试错特征
    if bash_ratio is not None and bash_ratio >= th["bash_maint"]:
        scores["code_maintenance"] += 1.0     # 重 shell = 运维/调试
    if web_search_count is not None and web_search_count >= th["web_noncoding"]:
        scores["non_coding"] += 1.0           # 频繁联网搜索 = 调研

    # Prefer keys that exist in the live config table.
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    for key, _ in ranked:
        if key in TASK_CATEGORIES:
            return key
    return DEFAULT_TASK_TYPE if DEFAULT_TASK_TYPE in TASK_CATEGORIES else next(
        iter(TASK_CATEGORIES), DEFAULT_TASK_TYPE
    )


def infer_task_type_from_usage(
    u: TokenUsage,
    *,
    net_loc: int | None,
    test_net_loc: int | None = None,
    doc_net_loc: int | None = None,
) -> str:
    """Infer task type from a ``TokenUsage`` (+ optional net LOC / test / doc split)."""
    tool_m = tool_usage_metrics(u)
    total_tools = sum(u.tool_calls.values())
    return infer_task_type(
        net_loc=net_loc,
        total_tokens=u.total,
        exploration_ratio=tool_m.get("exploration_ratio"),
        edit_ratio=tool_m.get("edit_ratio"),
        read_write_ratio=tool_m.get("read_write_ratio"),
        test_net_loc=test_net_loc,
        doc_net_loc=doc_net_loc,
        tool_error_rate=(u.tool_errors / total_tools) if total_tools else None,
        bash_ratio=tool_m.get("bash_ratio"),
        web_search_count=u.web_search_count,
    )


def majority_task_type(types: list[str | None]) -> str:
    """Most common valid task type; ties broken by taxonomy order (creation first)."""
    from collections import Counter

    order = ("code_creation", "code_maintenance", "non_coding")
    counts: Counter[str] = Counter()
    for t in types:
        key = coerce_task_type(t)
        if key:
            counts[key] += 1
    if not counts:
        return DEFAULT_TASK_TYPE
    best_n = max(counts.values())
    for key in order:
        if counts.get(key, 0) == best_n and key in TASK_CATEGORIES:
            return key
    return counts.most_common(1)[0][0]


def baseline_eligible_reports(reports, *, min_net_loc: int | None = None) -> list:
    """Sessions with complete TCER / CPE (required for personal baselines).

    ``min_net_loc`` 过滤近零产出会话——那些 net_loc 极小、CPE 被放大到失真的
    会话（见 MIN_BASELINE_NET_LOC）。None 时用默认下限；传 0 可关闭过滤（单元
    测试核对纯算术时用）。
    """
    floor = MIN_BASELINE_NET_LOC if min_net_loc is None else max(0, int(min_net_loc))
    out = []
    for r in reports:
        if getattr(r, "tcer", None) is None or getattr(r, "cpe", None) is None:
            continue
        if (getattr(r, "net_loc", None) or 0) < floor:
            continue
        out.append(r)
    return out


def compute_baselines(
    reports,
    *,
    min_sessions: int | None = None,
    min_net_loc: int | None = None,
    method: str = "median",
) -> dict | None:
    """Derive personal baselines (TCER/CPE) from sessions.

    Returns None if fewer than ``min_sessions`` (default
    :data:`MIN_BASELINE_SESSIONS`) sessions have complete TCER/CPE data.
    Small samples make median/mean jump wildly; Framework §8.3 expects a real
    reference set. Pass ``min_sessions=1`` in unit tests that only check the
    arithmetic.

    ``min_net_loc`` 剔除近零产出的 CPE 失真会话（默认 MIN_BASELINE_NET_LOC）；
    传 0 关闭过滤。个人基准应基于**跨所有项目**的会话汇总——调用方（GUI）负责
    把多项目的 reports 合并后传入，本函数只做过滤 + 聚合。

    ``method``：``"median"``（默认，抗离群）或 ``"mean"``（对全体样本敏感）。
    """
    import statistics

    need = MIN_BASELINE_SESSIONS if min_sessions is None else max(0, int(min_sessions))
    valid = baseline_eligible_reports(reports, min_net_loc=min_net_loc)
    if len(valid) < need:
        return None
    agg = statistics.mean if method == "mean" else statistics.median
    return {
        "tcer": agg([r.tcer for r in valid]),
        "cpe": agg([r.cpe for r in valid]),
    }


def save_baselines(values: dict, *, project_uid: str | None = None) -> None:
    """Write personal baselines into ``composite_baselines.json`` and refresh.

    project_uid=None → 写全局 ``baselines`` 块（影响所有无逐项目基准的项目）。
    project_uid 给定 → 写 ``baselines_per_project[uid]``（只影响该项目，全局不变）。

    Merges into the target block, clears the config cache, and refreshes the
    module-level constants. Writes atomically (temp file + ``os.replace``) on a
    shallow copy so the lru_cache dict is never mutated in place.
    """
    # Shallow-copy the cached config so we don't mutate the lru_cache's dict.
    cfg = {**_load_composite_config()}
    if project_uid:
        pp = {**cfg.get("baselines_per_project", {})}
        pp[project_uid] = {**pp.get(project_uid, {}), **values}
        cfg["baselines_per_project"] = pp
    else:
        cfg["baselines"] = {**cfg.get("baselines", {}), **values}
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
    _refresh_composite_globals()


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
    cache_write_1h_tokens: int = 0  # 1h 档缓存写子集（计价加溢价，与会话级口径一致）
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
    # 内部累加器（按 token 权重分摊：单模型会话权重 1.0，混合会话按占比拆分，
    # 不再把混合会话的行为数据整段丢弃或全额记给主模型）
    _weight_sum: float = 0.0  # Σ 会话权重，作产出/行为/质量指标的分母
    _rbw_sum: float = 0.0
    _rbw_weight: float = 0.0
    _tool_calls: dict = None
    _tool_errors: float = 0.0
    _code_added: float = 0.0
    _code_deleted: float = 0.0
    _code_reworked: float = 0.0
    _net_loc: float = 0.0
    _files_touched: float = 0.0

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
            mc.cache_write_1h_tokens += getattr(mu, "cache_write_1h_tokens", 0)
            mc.session_count += 1
            # 产出/行为/质量按该模型在会话内的 token 占比分摊。单模型会话
            # 权重恰为 1.0（与旧的「主模型全额归因」结果一致）；混合会话按
            # 占比拆分（旧逻辑要么整段丢弃、要么全额记给 >50% 的主模型）。
            mu_total = mu.input_tokens + mu.output_tokens + mu.cache_creation_input_tokens + mu.cache_read_input_tokens
            w = (mu_total / u.total) if u.total > 0 else 0.0
            if w > 0:
                mc._weight_sum += w
                for tool, cnt in u.tool_calls.items():
                    mc._tool_calls[tool] = mc._tool_calls.get(tool, 0) + cnt * w
                mc._tool_errors += u.tool_errors * w
                mc._code_added += (r.code_added or 0) * w
                mc._code_deleted += (r.code_deleted or 0) * w
                # Mirror compute(): self-rework count, falling back to gross
                # deletions when a session predates the code_reworked field.
                reworked = r.code_reworked if r.code_reworked is not None else r.code_deleted
                mc._code_reworked += (reworked or 0) * w
                mc._net_loc += (r.net_loc or 0) * w
                mc._files_touched += (r.files_touched or 0) * w
                if r.read_before_write is not None:
                    mc._rbw_sum += r.read_before_write * w
                    mc._rbw_weight += w

    # Compute derived metrics
    grand_tokens = sum(mc.total_tokens for mc in buckets.values())
    grand_cost = 0.0
    for mc in buckets.values():
        mc.cost = cost_usd(
            _FakeModelUsage(mc.input_tokens, mc.output_tokens,
                            mc.cache_creation_tokens, mc.cache_read_tokens,
                            mc.cache_write_1h_tokens),
            model=mc.model_id)
        grand_cost += mc.cost
        total_input = mc.input_tokens + mc.cache_creation_tokens + mc.cache_read_tokens
        mc.cache_hit_ratio = mc.cache_read_tokens / total_input if total_input > 0 else None
        mc.tokens_per_dollar = mc.total_tokens / mc.cost if mc.cost > 0 else None
        mc.code_per_dollar = mc._net_loc / mc.cost if mc.cost > 0 else None
        mc.token_share = mc.total_tokens / grand_tokens * 100 if grand_tokens else 0
        # 产出效率
        mc.net_loc_per_session = mc._net_loc / mc._weight_sum if mc._weight_sum > 0 else None
        # 行为特征
        total_tools = sum(mc._tool_calls.values())
        if total_tools > 0:
            # Align with tool_usage_metrics (Grep/Glob/Web + MCP search aliases).
            fake = TokenUsage()
            fake.tool_calls = dict(mc._tool_calls)
            tm = tool_usage_metrics(fake)
            mc.exploration_ratio = tm.get("exploration_ratio")
            edit_write = (
                mc._tool_calls.get("Edit", 0)
                + mc._tool_calls.get("MultiEdit", 0)
                + mc._tool_calls.get("Write", 0)
            )
            edit = mc._tool_calls.get("Edit", 0) + mc._tool_calls.get("MultiEdit", 0)
            mc.edit_ratio = edit / edit_write if edit_write > 0 else None
            # Prefer the r/w ratio that includes MCP read/scrape aliases.
            mc.read_write_ratio = tm.get("read_write_ratio")
            mc.tool_error_rate = mc._tool_errors / total_tools
        # 代码质量 (self-rework, consistent with compute()/SessionReport.churn_ratio)
        mc.churn_ratio = mc._code_reworked / mc._code_added if mc._code_added > 0 else None
        mc.read_before_write = mc._rbw_sum / mc._rbw_weight if mc._rbw_weight > 0 else None
        mc.files_per_session = mc._files_touched / mc._weight_sum if mc._weight_sum > 0 else None
    for mc in buckets.values():
        mc.cost_share = mc.cost / grand_cost * 100 if grand_cost else 0

    return sorted(buckets.values(), key=lambda mc: mc.total_tokens, reverse=True)


class _FakeModelUsage:
    """Lightweight stand-in for ModelUsage (avoids importing models.py)."""
    __slots__ = ("input_tokens", "output_tokens",
                 "cache_creation_input_tokens", "cache_read_input_tokens",
                 "cache_write_1h_tokens")

    def __init__(self, i, o, cw, cr, cw1h=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_creation_input_tokens = cw
        self.cache_read_input_tokens = cr
        self.cache_write_1h_tokens = cw1h



# Anthropic 缓存写分档：价表 cache_write 是 5m 率（1.25×input）；1h 率为 2×input，
# 即在 5m 率上加 (2/1.25 − 1) = 0.6 倍溢价。无分档信息的源 cache_write_1h_tokens=0。
CACHE_1H_PREMIUM = 0.6


def _cost_from(o, r: dict[str, float]) -> float:
    """USD cost of one token record ``o`` at rate map ``r`` (TokenUsage or ModelUsage)."""
    cw1h = getattr(o, "cache_write_1h_tokens", 0)
    return (
        o.input_tokens * r["input"]
        + o.cache_creation_input_tokens * r["cache_write"]
        + cw1h * r["cache_write"] * CACHE_1H_PREMIUM
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


def unmatched_pricing_models(u: TokenUsage) -> list[str]:
    """per_model keys priced via Anthropic default fallback (not in the table).

    Useful for GUI banners / status so users know costs may be approximate.
    """
    return pricing.unmatched_models(u.per_model.keys())


def cost_usd(u: TokenUsage, model: str | None = None) -> float:
    """Estimate USD cost at vendor list price (not subscription billing).

    Each model's tokens are priced at that model's own rate and summed, so
    mixed-model sessions are exact. An explicit ``model`` forces every token onto
    that model's rate. Falls back to a single resolved rate only when no
    per-model buckets exist (synthetic usage) — unknown / mixed there default to
    Anthropic list price.

    ``model`` may be a non-empty id, or empty/None. Empty string is treated like
    None so callers can pass raw ``per_model`` keys (including the ``""``
    bucket). Works for both ``TokenUsage`` and ``ModelUsage``.
    """
    model_key = model if model else None
    # TokenUsage with per-model buckets: sum each bucket at its own rate.
    per_model = getattr(u, "per_model", None)
    if model_key is None and per_model:
        return sum(cost_by_model(u).values())
    if model_key:
        return _cost_from(u, pricing.resolve(model_key))
    # No explicit id: TokenUsage may carry a single session model; ModelUsage
    # (no ``models`` attr) falls through to default list price.
    models = getattr(u, "models", None)
    if models is not None and len(models) == 1:
        return _cost_from(u, pricing.resolve(next(iter(models))))
    return _cost_from(u, pricing.default_pricing())


# --------------------------------------------------------------------------- #
# New metrics: timing, tool usage, context efficiency
# --------------------------------------------------------------------------- #
def avg_request_latency_ms(u: TokenUsage) -> float | None:
    """平均请求延迟（毫秒）——与 cc-switch 的「平均延迟」同口径（每次 API 请求耗时）。

    Grok：Σ(apiDurationMs) ÷ Σ(modelCalls)——apiDurationMs 是回合内多次调用
    的总和，按调用数均摊才是每请求延迟（此前按回合均值虚高 ~5.7×）。
    omp：mean(每响应 duration)（每响应即一次补全，精确）。
    Claude（仅整轮墙钟）/ Codex（仅任务级墙钟）→ None，由 compute 按源门控。
    """
    durations = [t.duration_ms for t in u.turn_stats if t.duration_ms]
    if not durations:
        return None
    if u.api_calls > 0:
        return sum(durations) / u.api_calls
    return sum(durations) / len(durations)


def _tool_leaf(name: str) -> str:
    """Normalize a tool id for alias matching.

    ``mcp__server__tool`` → last segment; otherwise the lowercased full name.
    Keeps raw ``tool_calls`` keys intact for the tools popup.
    """
    if not name:
        return ""
    if name.startswith("mcp__"):
        return name.rsplit("__", 1)[-1].lower()
    return name.lower()


def _leaf_has_keyword(leaf: str, key: str) -> bool:
    """True if *key* is a path segment of *leaf*, not a mid-word substring.

    Substring matching mis-classifies live tools: ``GetTaskOutput`` matched
    ``get`` (read), ``ReportFindings`` matched ``find`` (explore). Segments are
    split on ``_`` / ``-`` after lowercasing (MCP leaves already use underscores).
    """
    if not leaf or not key:
        return False
    if leaf == key:
        return True
    segs = leaf.replace("-", "_").split("_")
    return key in segs


# Canonical TCER / built-in tools — never re-classify via leaf heuristics.
# Includes Grok-build meta tools so GetTaskOutput/SearchTool never look like
# Read/Grep via the ``get`` / ``search`` substring trap.
_CANONICAL_TOOL_NAMES = frozenset({
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Grep", "Glob", "Bash", "PowerShell", "Task", "Agent",
    "WebSearch", "WebFetch", "TodoWrite", "TodoRead",
    "AskUserQuestion", "ToolSearch", "ExitPlanMode", "EnterPlanMode",
    "GetTaskOutput", "KillTask", "SearchTool", "UseTool",
    "SchedulerCreate", "SchedulerDelete", "ImageGen", "ImageEdit",
    "MemorySearch", "MemoryGet", "LSP", "Thinking",
})


def retry_loop_metrics(u: TokenUsage, *, min_run: int = 3) -> dict:
    """工具重试循环（卡死信号）：同工具+同路径在 tool_ops 时序上连续重复 ≥min_run 次。

    tool_error_rate 只给比例不给结构——15% 错误率可能是分散的小错，也可能
    是同一个死循环烧掉几十个回合（Edit 反复匹配失败、Read 反复读同一大文
    件）。这里按相邻比较找连续 run（O(n)）。只统计 path 非空的调用：Bash
    等无路径工具不参与，避免把任意 3 连 Bash 误判成循环。

    Returns: {"count": 循环次数, "max_len": 最长循环长度,
              "details": {"Tool:path": 最长 run} 或 None}
    """
    loops = 0
    max_len = 0
    details: dict[str, int] = {}
    run_key: tuple[str, str] | None = None
    run_len = 0

    def _flush() -> None:
        nonlocal loops, max_len
        if run_key is not None and run_len >= min_run:
            loops += 1
            label = f"{run_key[0]}:{run_key[1]}"
            details[label] = max(details.get(label, 0), run_len)
            max_len = max(max_len, run_len)

    for op in u.tool_ops:
        key = (op.tool, op.path) if op.path else None
        if key is not None and key == run_key:
            run_len += 1
            continue
        _flush()
        run_key = key
        run_len = 1 if key is not None else 0
    _flush()
    return {"count": loops, "max_len": max_len, "details": details or None}


def turn_cost_analysis(u: TokenUsage) -> dict:
    """逐回合成本近似 + 缓存失效尖峰（消费 turn_stats，无新增 IO）。

    - ``max_turn_cost`` / ``max_turn_share`` / ``spike_turn``：最贵回合的近似
      成本、占全会话回合成本合计的份额、回合号。「大户回合」阈值（30%）在
      insights._TH.TURN_COST_SPIKE 判定，本函数只产出数值。
    - ``cache_invalidation_events``：cache_write 环比翻倍且 cache_read 回落
      的回合数（前缀被改动作废缓存）——比整体 CHR 更能定位哪个回合破坏了
      缓存。首轮 cache 建立不计（正常冷启动）。

    近似口径：逐回合计价不含 1h 缓存写分档（TurnStat 无该子集）；回合成本
      合计与 ``cost_usd`` 可能略有出入，仅用于回合间的相对比较。
    """
    empty = {"max_turn_cost": None, "max_turn_share": None, "spike_turn": None,
             "cache_invalidation_events": 0}
    stats = u.turn_stats
    if not stats:
        return empty
    rate_cache: dict[str, dict[str, float]] = {}
    costs: list[float] = []
    for t in stats:
        key = t.model or ""
        r = rate_cache.get(key)
        if r is None:
            r = pricing.resolve(key) if key else pricing.default_pricing()
            rate_cache[key] = r
        costs.append(
            t.input_tokens * r["input"] / 1e6
            + t.cache_write * r["cache_write"] / 1e6
            + t.cache_read * r["cache_read"] / 1e6
            + t.output_tokens * r["output"] / 1e6
        )
    total = sum(costs)
    if total <= 0:
        return empty
    idx = max(range(len(costs)), key=lambda i: costs[i])
    events = 0
    for i in range(1, len(stats)):
        prev, cur = stats[i - 1], stats[i]
        if (cur.cache_write > prev.cache_write * 2 and cur.cache_write >= 2000
                and cur.cache_read < prev.cache_read):
            events += 1
    return {
        "max_turn_cost": costs[idx],
        "max_turn_share": costs[idx] / total,
        "spike_turn": stats[idx].turn,
        "cache_invalidation_events": events,
    }


def activity_metrics(u: TokenUsage) -> dict:
    """人机时间结构：AI 活跃占比 + 用户响应间隔中位数。

    - ``ai_active_ratio``：Σ 逐回合 duration ÷ 会话墙钟（0..1，封顶 1）。
      回答「时间花在等 AI 还是 AI 在等我」。Grok 扣除审批等待（人在卡 AI
      的显式时间）；Claude 的 duration 是整轮墙钟（含工具执行），此值为
      上界估计（tooltip 注明）。
    - ``user_gap_median_min``：相邻回合时间戳间隔的中位数（分钟），只统计
      1–30 分钟的间隔（<1 分钟是连发，>30 分钟视作离开，都不算「响应」）。
    """
    out = {"ai_active_ratio": None, "user_gap_median_min": None}
    total_ms = u.session_duration_ms or 0
    dur = sum(t.duration_ms for t in u.turn_stats if t.duration_ms)
    if dur > 0 and total_ms > 0:
        wait = getattr(u, "permission_wait_ms_total", 0) or 0
        out["ai_active_ratio"] = min(1.0, max(0.0, dur - wait) / total_ms)
    gaps: list[float] = []
    ts_prev = None
    for t in u.turn_stats:
        if t.ts is None:
            continue
        if ts_prev is not None:
            g = t.ts - ts_prev
            if 60_000 <= g <= 30 * 60_000:
                gaps.append(g)
        ts_prev = t.ts
    if gaps:
        gaps.sort()
        mid = len(gaps) // 2
        med = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
        out["user_gap_median_min"] = med / 60_000
    return out


def segment_metrics(u: TokenUsage, *, n_segments: int = 3) -> dict:
    """分段效率：把回合均分为 n 段，逐段算「每百万 Token 净增行」（分段 TCER）。

    回答「长会话什么时候开始空转、该不该收口新开」。``decay_ratio`` = 末段
    TCER ÷ 首段 TCER（<0.5 即明显衰减）。段内 token 来自 turn_stats、净增行
    来自 turn_net_locs（当前仅 Claude 填充）——后者为空时各段 TCER/衰减比为
    None，但段 token 曲线仍可用。压缩位置（compaction_turns）随段返回，供
    恢复分析定位。
    """
    stats = u.turn_stats
    segs = [{
        "tokens": 0, "added": 0, "deleted": 0,
        "compactions": 0, "tcer": None,
    } for _ in range(n_segments)]
    if not stats:
        return {"segments": segs, "decay_ratio": None}
    n = len(stats)
    bounds = [(i * n // n_segments, (i + 1) * n // n_segments) for i in range(n_segments)]
    # 回合号 → 段号的映射：turn_stats 的 turn 字段才是权威回合号——零 usage
    # 桩消耗回合号但不产生 TurnStat，列表下标与回合号有空洞错位；此前按下标
    # 查 LOC/压缩位置会把尾段 LOC 静默丢弃、段配对错位（衰减比被压低）。
    turn_to_seg: dict[int, int] = {}
    for i, (lo, hi) in enumerate(bounds):
        for t in stats[lo:hi]:
            turn_to_seg[t.turn] = i
    seg_first_turn = [stats[lo].turn for lo, _ in bounds]

    def _seg_of_turn(turn: int) -> int:
        seg = turn_to_seg.get(turn)
        if seg is not None:
            return seg
        # 不在 stats 里的回合号（截断尾部/极端空洞）：就近归段。
        for i in range(n_segments - 1, -1, -1):
            if turn >= seg_first_turn[i]:
                return i
        return 0

    loc_by_turn: dict[int, tuple[int, int]] = {}
    for turn, a, d in u.turn_net_locs:
        prev = loc_by_turn.get(turn, (0, 0))
        loc_by_turn[turn] = (prev[0] + a, prev[1] + d)
    for i, (lo, hi) in enumerate(bounds):
        for t in stats[lo:hi]:
            segs[i]["tokens"] += (t.input_tokens + t.cache_write
                                  + t.cache_read + t.output_tokens)
    for turn, (a, d) in loc_by_turn.items():
        seg = segs[_seg_of_turn(turn)]
        seg["added"] += a
        seg["deleted"] += d
    for i in range(n_segments):
        mt = segs[i]["tokens"] / 1e6
        net = segs[i]["added"] - segs[i]["deleted"]
        segs[i]["tcer"] = net / mt if (mt > 0 and u.turn_net_locs) else None
    for ct in u.compaction_turns:
        segs[_seg_of_turn(ct)]["compactions"] += 1
    decay = None
    first, last = segs[0]["tcer"], segs[-1]["tcer"]
    if first is not None and last is not None and first > 0:
        decay = last / first
    return {"segments": segs, "decay_ratio": decay}


def tool_usage_metrics(u: TokenUsage) -> dict[str, float | None]:
    """Read/Write ratio, Edit ratio, exploration density.

    Counts are TCER-canonical tools plus light MCP / third-party aliases so
    sessions that search via Tavily/Firecrawl (``mcp__…`` *or* bare
    ``firecrawl_search``) still get a fair exploration_ratio. Raw names stay
    in ``tool_calls`` for the tools popup.
    """
    read = u.tool_calls.get("Read", 0)
    write = u.tool_calls.get("Write", 0)
    edit = u.tool_calls.get("Edit", 0) + u.tool_calls.get("MultiEdit", 0)
    # Shell variants
    bash = u.tool_calls.get("Bash", 0) + u.tool_calls.get("PowerShell", 0)
    grep = u.tool_calls.get("Grep", 0)
    glob = u.tool_calls.get("Glob", 0)
    web = u.tool_calls.get("WebSearch", 0) + u.tool_calls.get("WebFetch", 0)

    for name, cnt in u.tool_calls.items():
        if name in _CANONICAL_TOOL_NAMES:
            continue
        leaf = _tool_leaf(name)
        if not leaf:
            continue
        # Search-like MCP / bare tools → exploration (alongside Grep/Glob).
        if any(_leaf_has_keyword(leaf, k) for k in ("search", "grep", "find", "query", "map")):
            grep += cnt
        # Read/scrape/extract → read-side signal for r/w ratio.
        elif any(_leaf_has_keyword(leaf, k) for k in ("scrape", "fetch", "extract", "read", "get", "crawl")):
            read += cnt

    total_tools = sum(u.tool_calls.values())
    explore = grep + glob + web

    return {
        "read_write_ratio": read / (write + edit) if (write + edit) else None,
        "edit_ratio": edit / (edit + write) if (edit + write) else None,
        "exploration_ratio": explore / total_tools if total_tools else None,
        # Bash 占比：量化「探索/阅读经 Bash 完成」的盲区暴露面（cat/rg/find 不计入
        # read/exploration，占比越高，上面两个比率越失真）。
        "bash_ratio": bash / total_tools if total_tools else None,
        # exposed for debugging / future metrics (not required by callers)
        "_bash_like": bash,
        "_explore_count": explore,
    }


def cache_efficiency(u: TokenUsage) -> float | None:
    """Cache read / write ratio (>1 means cache paid off)."""
    cw = u.cache_creation_input_tokens
    return (u.cache_read_input_tokens / cw) if cw else None


def output_tps(u: TokenUsage) -> float | None:
    """Output generation throughput (tokens/sec) over timed turns.

    Σ output_tokens ÷ Σ duration_ms (in seconds). Grok ``apiDurationMs`` 是回合内
    全部调用的 API 总时长、omp/Pi ``duration`` 是每响应时长——分子分母同为回合
    总量，比值正确。Codex 的 ``task_complete.duration_ms`` 实测是任务级墙钟
    （含工具执行，比单补全高一个数量级）→ Codex 由 compute 门控为 None。

    NOTE: Claude is deliberately excluded (see ``metric_defs._SOURCE_SUPPORT``).
    Its JSONL has no per-completion duration; the only timing is ``turn_duration``,
    a whole-user-turn wall clock that bundles multiple API calls, tool execution
    and approval waits (messageCount up to 400+). Using it as the denominator
    understates throughput 3–10×, so the reader does not backfill Claude
    ``turn_stats.duration_ms`` for this metric's purpose and Claude reports
    「不适用」. Sources without per-completion timing (OpenCode multi-turn) yield
    None rather than a wall-clock figure.
    """
    out = 0
    ms = 0
    for t in u.turn_stats:
        if t.duration_ms and t.duration_ms > 0:
            out += t.output_tokens
            ms += t.duration_ms
    if ms <= 0 or out <= 0:
        return None
    return out / (ms / 1000.0)


def _is_code_search_tool(name: str) -> bool:
    """True if *name* is a code/repo search tool for search_edit_ratio.

    Built-in Grep/Glob plus the same MCP / bare search aliases used by
    ``tool_usage_metrics`` (``firecrawl_search``, ``mcp__…__*_query``, …).
    Canonical meta tools stay out: ``ToolSearch`` / ``WebSearch`` are not
    repo-search follow-through signals.
    """
    if name in ("Grep", "Glob"):
        return True
    if name in _CANONICAL_TOOL_NAMES:
        return False
    leaf = _tool_leaf(name)
    return any(_leaf_has_keyword(leaf, k) for k in ("search", "grep", "find", "query", "map"))


def file_quality_metrics(u: TokenUsage) -> dict[str, float | None]:
    """Temporal search-edit and read-before-write analysis.

    search_edit_ratio: fraction of code-search calls (Grep/Glob **and** search-like
    MCP/bare tools) that are *followed* by a Write/Edit/MultiEdit within
    ``WINDOW`` assistant turns. This is turn-based, not file-based: real
    Grep/Glob carry a ``path`` that is usually a directory (or no path at all
    for a repo-wide search), so matching a search to the exact file later edited
    is unreliable. Measuring follow-through in *time* captures the intended
    workflow signal — "did searching lead to a change soon after, or was it
    dead-end exploration?" — and works on real Claude Code data (including
    sessions that only search via Firecrawl/Tavily).
    read_before_write: fraction of Write/Edit targets where the same file was
    Read in a previous turn.
    """
    from collections import defaultdict

    _WRITE_EDIT = {"Write", "Edit", "MultiEdit"}
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

    # Search-edit ratio: a code-search is "productive" if any Write/Edit
    # happens within WINDOW turns after it. Path-agnostic (see docstring).
    # 跟进判定用 bisect 在已排序回合集合上二分——线性扫整个列表是
    # O(搜索数×编辑数)，大会话几十万次比较（实测 48 会话 2 百万次 genexpr）。
    from bisect import bisect_right

    def _followed_within(turn: int, follow_turns: list[int]) -> bool:
        i = bisect_right(follow_turns, turn)
        return i < len(follow_turns) and follow_turns[i] <= turn + WINDOW

    edit_turns = sorted({op.turn for op in u.tool_ops if op.tool in _WRITE_EDIT})
    searches = 0
    searches_with_edit = 0
    for op in u.tool_ops:
        if not _is_code_search_tool(op.tool):
            continue
        searches += 1
        if _followed_within(op.turn, edit_turns):
            searches_with_edit += 1
    ste = (searches_with_edit / searches) if searches else None

    # 改→验闭环率：Write/Edit 后 WINDOW 回合内出现 Bash/PowerShell（跑测试/
    # 编译/lint 的唯一通道）。低值 = 改完不验证就继续。
    verify_turns = sorted({op.turn for op in u.tool_ops
                           if op.tool in ("Bash", "PowerShell")})
    edits = edits_with_verify = 0
    for op in u.tool_ops:
        if op.tool not in _WRITE_EDIT:
            continue
        edits += 1
        if _followed_within(op.turn, verify_turns):
            edits_with_verify += 1
    vae = (edits_with_verify / edits) if edits else None

    # 首次编辑回合（1-based）：动手前的探索/热身长度。None = 纯阅读会话。
    first_edit = min((op.turn for op in u.tool_ops if op.tool in _WRITE_EDIT),
                     default=None)

    return {
        "search_edit_ratio": ste,
        "read_before_write": rbw,
        "edit_verify_ratio": vae,
        "first_edit_turn": (first_edit + 1) if first_edit is not None else None,
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


def normalized_tcer(tcer: float | None, task_type: str | None) -> float | None:
    """Normalized TCER (NTCER) = TCER / TTAF_task.

    Removes the task-type bias so different task types can be compared fairly.
    For example: debug TCER=30, TTAF=0.4, NTCER=75 — showing the efficiency
    is actually good for a debugging task.
    """
    if tcer is None:
        return None
    key = coerce_task_type(task_type)
    if not key:
        return None
    factor = TASK_CATEGORIES.get(key, {}).get("ttaf")
    if not factor:
        return None
    return tcer / float(factor)


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


# ============================================================
# 综合效率分 v2 — three orthogonal axes, half-saturation, evidence shrinkage.
#
# 设计动机（替代 CTEI 的统计缺陷）：
#   • CTEI = (TCER/Bt)×(Bc/CPE)×(1+CHR·w) 把两个共线比率（都由 loc/tok 驱动）
#     相乘 ≈ 对 loc/tok 平方 → 方差放大、重尾（故 HTML 报告要 P90 截尾），
#     且缓存被数 2–3 次，还完全忽略质量。
#   • v2 用三条正交轴：产出(ntcer) · 成本(cpe，已含缓存收益，故不再单列缓存) ·
#     质量(返工/工具错误/先读后写)。每轴经半饱和变换 Φ(x)=x/(x+b)∈[0,1)——
#     baseline b 处恰 =0.5，单调、有界、自动饱和重尾（无需截尾）。
#   • 小会话按证据量(net_loc)向 0.5 收缩，噪声无法登顶。
#   • 加权算术平均合成 0–100：有界、可分解展示、单轴缺失可重分权优雅降级。
# ============================================================

def _half_sat(x: float | None, baseline: float) -> float | None:
    """半饱和变换 Φ(x)=x/(x+b)∈[0,1)。x=b→0.5（与基准持平），单调递增、有界。

    None / 负基准 → None。x<0 夹到 0（净删代码等病态输入不给正分）。
    """
    if x is None or baseline <= 0:
        return None
    x = max(0.0, x)
    return x / (x + baseline)


def output_axis(ntcer: float | None, baseline: float | None = None) -> float | None:
    """产出轴 ∈[0,1)：每百万 token 的任务归一净产出，半饱和到基准。

    baseline 未给时读实时全局（个人基准 save 后重绑，勿用默认参数冻结）。
    """
    return _half_sat(ntcer, SCORE_OUTPUT_BASELINE if baseline is None else baseline)


def cost_axis(cpe: float | None, baseline: float | None = None) -> float | None:
    """成本轴 ∈(0,1]：每千行成本越低越好，故对 CPE 取反向半饱和 b/(x+b)。

    CPE 的 cost 已按缓存读低价计入，缓存收益天然体现在这里——不再单列缓存因子
    （消除 CTEI 的双重计数）。CPE=基准→0.5；CPE→0（极省）→1；CPE→∞→0。
    baseline 未给时读实时全局。
    """
    b = SCORE_COST_BASELINE if baseline is None else baseline
    if cpe is None or cpe < 0 or b <= 0:
        return None
    return b / (cpe + b)


def quality_axis(
    churn_ratio_: float | None,
    tool_error_rate: float | None,
    read_before_write: float | None,
) -> float | None:
    """质量轴 ∈[0,1]：返工率↓、工具错误率↓、先读后写↑ 的加权合成，与体量无关。

    每个子信号折算到「越大越好」的 [0,1]：低返工=1−churn、低错误=1−err、
    先读后写直接用其比率。缺失的子信号把权重重分配给其余项（优雅降级）；
    三者全缺 → None（该会话无质量信号，合成时把质量权重转给产出/成本轴）。
    """
    parts: list[tuple[float, float]] = []  # (weight, value)
    w = SCORE_QUALITY_WEIGHTS
    if churn_ratio_ is not None:
        parts.append((w["low_rework"], 1.0 - min(1.0, max(0.0, churn_ratio_))))
    if tool_error_rate is not None:
        parts.append((w["low_tool_error"], 1.0 - min(1.0, max(0.0, tool_error_rate))))
    if read_before_write is not None:
        parts.append((w["read_before_write"], min(1.0, max(0.0, read_before_write))))
    if not parts:
        return None
    wsum = sum(wt for wt, _ in parts)
    return sum(wt * v for wt, v in parts) / wsum if wsum else None


def _shrink(axis: float | None, evidence: float | None) -> float | None:
    """按证据量向中性 0.5 收缩：w=m/(m+k)，ẽ=0.5+w·(e−0.5)。

    m=证据量(net_loc)，k=SCORE_SHRINK_K。小会话被拉回中位，无法靠噪声冲顶。
    """
    if axis is None:
        return None
    m = max(0.0, evidence or 0.0)
    w = m / (m + SCORE_SHRINK_K) if (m + SCORE_SHRINK_K) > 0 else 0.0
    return 0.5 + w * (axis - 0.5)


def efficiency_score(
    ntcer: float | None,
    cpe: float | None,
    churn_ratio_: float | None,
    tool_error_rate: float | None,
    read_before_write: float | None,
    *,
    net_loc: int | None,
    tcer_baseline: float | None = None,
    cpe_baseline: float | None = None,
) -> float | None:
    """综合效率分 ∈[0,100]：三正交轴半饱和 → 证据收缩 → 加权合成。

    产出轴缺失（无 ntcer/loc）→ None（无从评效率）。成本或质量轴缺失时，把其
    权重重分配给可用轴（优雅降级）。返回 None 表示该会话不参与效率排名。
    tcer_baseline/cpe_baseline 未给时读实时全局（个人基准 save 后即生效）。
    """
    a = score_axes(ntcer, cpe, churn_ratio_, tool_error_rate, read_before_write,
                   net_loc=net_loc, tcer_baseline=tcer_baseline,
                   cpe_baseline=cpe_baseline)
    avail = {k: v for k, v in a.items() if v is not None}
    if "output" not in avail:
        return None  # 产出轴是效率分的必要条件
    wsum = sum(SCORE_WEIGHTS[k] for k in avail)
    if wsum <= 0:
        return None
    blended = sum(SCORE_WEIGHTS[k] * v for k, v in avail.items()) / wsum
    return round(100.0 * blended, 2)


def score_axes(
    ntcer: float | None,
    cpe: float | None,
    churn_ratio_: float | None,
    tool_error_rate: float | None,
    read_before_write: float | None,
    *,
    net_loc: int | None,
    tcer_baseline: float | None = None,
    cpe_baseline: float | None = None,
) -> dict[str, float | None]:
    """三轴收缩后的分值（0–1），供 GUI 分解展示 / audit 重算校验。"""
    return {
        "output": _shrink(output_axis(ntcer, tcer_baseline), net_loc),
        "cost": _shrink(cost_axis(cpe, cpe_baseline), net_loc),
        "quality": _shrink(quality_axis(churn_ratio_, tool_error_rate, read_before_write), net_loc),
    }


# 综合效率分评级带（0–100），best→worst：(名称, 下界)，从 config 派生（SSOT）。
# 顶档严格大于其下界，其余 ≥。tier() 与 GUI 排名条/趋势带均从此取。
SCORE_TIER_BANDS: list[tuple[str, float]] = _get_score_tier_bands()


def tier(score: float | None) -> str | None:
    """综合效率分评级标签，从 SCORE_TIER_BANDS 派生。"""
    if score is None:
        return None
    top_label, top_lo = SCORE_TIER_BANDS[0]
    if score > top_lo:
        return top_label
    for label, lo in SCORE_TIER_BANDS[1:]:
        if score >= lo:
            return label
    return SCORE_TIER_BANDS[-1][0]


def compute(
    meta: SessionMeta,
    u: TokenUsage,
    net_loc: int | None,
    *,
    task_type: str | None = None,
    code_added: int | None = None,
    code_deleted: int | None = None,
    code_reworked: int | None = None,
    high_churn_files: int = 0,
    test_net_loc: int | None = None,
    doc_net_loc: int | None = None,
    tcer_baseline: float | None = None,
    cpe_baseline: float | None = None,
) -> SessionReport:
    """Compute the full per-session report from accumulated usage + net LOC.

    Composite fields (CAF / NTCER / 综合效率分 + tier + axes) and the churn ratio
    are filled in opportunistically: each is None unless its inputs are available.
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

    # --- task type (coerce aliases; unknown → None so NTCER stays unset) ---
    task_type = coerce_task_type(task_type)
    task_category = get_task_category(task_type) if task_type else None
    ttaf_value = get_task_ttaf(task_type) if task_type else None

    # --- composite layer ---
    caf_ = caf(u)
    ta = normalized_tcer(tcer, task_type)

    # --- timing metrics ---
    # 源门控（与 _SOURCE_SUPPORT 同判定）：latency 需要 Grok(api_calls 均摊)/
    # omp(每响应 duration)；output_tps 需要 Grok/omp(总 output÷总 API 时长)。
    # Claude(整轮墙钟)/Codex(任务级墙钟) 的 duration 语义不符，产出会在
    # GUI「不适用」而 CSV/上传照发的口径间分裂——直接置 None 三通道一致。
    _dur_ok = meta.source in ("grok", "omp", "pi")
    avg_turn_lat = avg_request_latency_ms(u) if _dur_ok else None
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
    if u.ttft_ms_samples:
        ordered = sorted(u.ttft_ms_samples)
        p95_idx = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1) + 0.5))
        ttft_p95 = ordered[p95_idx] / 1000
    else:
        ttft_p95 = None
    task_completion = (
        u.completed_task_count / u.task_count
        if u.task_count else None
    )
    patch_success = (
        u.patch_apply_success_count / u.patch_apply_count
        if u.patch_apply_count else None
    )
    # Peak single-turn input ÷ window (not session-summed total_input, which
    # inflates multi-turn Codex sessions to 50–200× and is not a utilization rate).
    peak_in = u.peak_input_tokens or 0
    context_window_ratio = (
        peak_in / u.model_context_window
        if u.model_context_window and peak_in > 0
        else None
    )
    reasoning_ratio = (
        u.reasoning_output_tokens / u.output_tokens
        if u.output_tokens else None
    )
    # Derive files_touched from tool_ops. "涉及文件" means files the session
    # actually read/wrote/edited — so a path that appears ONLY via a search tool
    # (Grep/Glob and search-like aliases) is excluded: those tools' ``path`` is a
    # search *scope*, frequently a directory (e.g. ``.../tcer/core``), not a file.
    # Counting it inflates the count and pollutes the 涉及文件 popup with dirs.
    # (Claude records no path for Grep/Glob; Grok/omp do — hence the filter.)
    # 搜索足迹：搜索工具（Grep/Glob 及别名）扫过的路径 → 次数（含目录；供弹窗
    # 展示 AI 的探索范围）。真实读/写/改的文件另计入 files_touched。
    searched: dict[str, int] = {}
    for op in u.tool_ops:
        if op.path and _is_code_search_tool(op.tool):
            searched[op.path] = searched.get(op.path, 0) + 1
    _non_search = {op.path for op in u.tool_ops
                   if op.path and not _is_code_search_tool(op.tool)}
    # 仅被搜索碰过、从未被读/写/改的路径不算「涉及文件」；也被 Read/Write/Edit
    # 碰过的路径仍是真实文件，保留。
    _search_only = set(searched) - _non_search
    touched: set[str] = set()
    ftd: dict[str, int] = {}
    for op in u.tool_ops:
        if op.path and op.path not in _search_only:
            touched.add(op.path)
            ftd[op.path] = ftd.get(op.path, 0) + 1
    fq = file_quality_metrics(u)
    rl = retry_loop_metrics(u)
    # C2 自报成本偏差：价表计价 ÷ 源自报成本（opencode 会话总额 / omp·pi 逐响应
    # 累加）。1.0=口径一致；偏离提示价表错价/漏 1h 档/默认回退失真。
    cost_reported_ratio = (
        cost / u.reported_cost_usd
        if (u.reported_cost_usd and u.reported_cost_usd > 0 and cost is not None)
        else None
    )
    # C3 LOC 可信度：回放 added/deleted vs Claude 自算 structuredPatch 的吻合率。
    patch_a = getattr(u, "patch_diff_added", 0)
    patch_d = getattr(u, "patch_diff_deleted", 0)
    loc_patch_agreement = None
    if patch_a or patch_d:
        diff = abs((code_added or 0) - patch_a) + abs((code_deleted or 0) - patch_d)
        loc_patch_agreement = max(0.0, 1.0 - diff / max(1, patch_a + patch_d))
    tc = turn_cost_analysis(u)
    act = activity_metrics(u)

    # --- 综合效率分 v2：三正交轴半饱和 + 证据收缩 + 加权（见 efficiency_score）---
    churn_ = churn_ratio(
        code_added,
        code_reworked if code_reworked is not None else code_deleted,
    )
    _axes = score_axes(ta, cpe, churn_, tool_err_rate, fq["read_before_write"],
                       net_loc=net_loc, tcer_baseline=tcer_baseline,
                       cpe_baseline=cpe_baseline)
    score_ = efficiency_score(ta, cpe, churn_, tool_err_rate, fq["read_before_write"],
                              net_loc=net_loc, tcer_baseline=tcer_baseline,
                              cpe_baseline=cpe_baseline)

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
        caf=caf_,
        task_type=task_type,
        task_category=task_category,
        ttaf=ttaf_value,
        ntcer=ta,
        ta_tcer=ta,  # backward compat
        score=score_,
        tier=tier(score_),
        score_output_axis=_axes["output"],
        score_cost_axis=_axes["cost"],
        score_quality_axis=_axes["quality"],
        code_added=code_added,
        code_deleted=code_deleted,
        code_reworked=code_reworked,
        churn_ratio=churn_,
        # --- timing ---
        avg_request_latency_ms=avg_turn_lat,
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
        searched_paths_details=searched if searched else None,
        thinking_count=u.thinking_count,
        search_edit_ratio=fq["search_edit_ratio"],
        read_before_write=fq["read_before_write"],
        edit_verify_ratio=fq["edit_verify_ratio"],
        first_edit_turn=fq["first_edit_turn"],
        bash_ratio=tool_m["bash_ratio"],
        retry_loop_count=rl["count"],
        retry_loop_max_len=rl["max_len"],
        retry_loop_details=rl["details"],
        turn_cost_max_share=tc["max_turn_share"],
        turn_cost_spike_turn=tc["spike_turn"],
        cache_invalidation_events=tc["cache_invalidation_events"],
        ai_active_ratio=act["ai_active_ratio"],
        user_gap_median_min=act["user_gap_median_min"],
        efficiency_decay_ratio=segment_metrics(u)["decay_ratio"],
        time_to_first_token_sec=ttft_sec,
        ttft_p95_sec=ttft_p95,
        task_completion_rate=task_completion,
        patch_apply_success_rate=patch_success,
        context_window_used_ratio=context_window_ratio,
        reasoning_output_ratio=reasoning_ratio,
        output_tps=output_tps(u) if _dur_ok else None,
        cost_reported_ratio=cost_reported_ratio,
        loc_patch_agreement=loc_patch_agreement,
    )
