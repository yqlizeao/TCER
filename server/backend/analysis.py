"""Decision Lab — stratified cohort comparison over uploaded TCER sessions.

The dashboard endpoints answer *"how much"*. This module answers the question a
team actually has to act on: **"does changing this knob make things better, and
do I have enough evidence to believe it?"**

A *cohort* is the set of sessions that share one value of a **configuration
dimension** — something the user can actually change:

    model · source (which agent CLI) · reasoning_effort · permission_profile ·
    approval_policy · collaboration_mode · cli_version · skill · mcp · subagent

Comparing cohorts naively is worse than not comparing at all, because uploaded
sessions are observational: the strong model gets pointed at the hard problems,
the "creation" sessions produce far more net LOC than the debugging ones. Three
defenses, in order of importance:

1. **Stratification.** Every comparison happens *within* a stratum of
   (task_type × session-size band) and the per-stratum differences are then
   pooled, precision-weighted. A cohort can no longer win just by having drawn
   an easier mix of work.
2. **Robust statistics.** Session metrics are heavy-tailed — one 2M-token
   session dominates any mean. Center is the **median**; the winsorized mean is
   reported alongside as a sanity check, never as the headline.
3. **Bootstrap intervals + honest grading.** 1000 stratified resamples give a
   95% interval on the difference. **An interval that spans 0 produces no
   ranking**, only "no measurable difference". Cohorts below the minimum sample
   size are reported as ``insufficient`` and excluded from recommendations.

What this module deliberately does **not** do: claim causation. Stratification
controls the confounders we can see (task type, session size); it cannot control
selection effects like "hard tickets get the expensive model". Every wording it
emits is associational, and the top grade is ``strong``, never ``proven``.

Pure stdlib, fully deterministic (fixed bootstrap seed) so refreshing the page
never changes a confidence interval.
"""
from __future__ import annotations

import json
import math
import random
from typing import Callable, Iterable

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
MIN_COHORT_SESSIONS = 5      # below this a cohort is "insufficient evidence"
MIN_STRATUM_PER_SIDE = 2     # a stratum needs this many on each side to count
BOOTSTRAP_ITERS = 1000
BOOTSTRAP_SEED = 20260729    # fixed: the same data must give the same interval
WINSOR_FRACTION = 0.10
CI_ALPHA = 0.05
SIZE_BANDS = ("S", "M", "L")  # session-size terciles by total_tokens


# --------------------------------------------------------------------------- #
# Metrics: value extraction + which direction is "better"
# --------------------------------------------------------------------------- #
class Metric:
    """One comparable per-session quantity.

    ``higher_is_better`` drives every ranking and recommendation; ``fmt`` tells
    the UI how to print it. ``guardrail`` metrics are always reported next to
    the headline one — a speed gain that quietly costs quality is the single
    most common way AI-productivity dashboards mislead (see DX / GitClear 2026).
    """

    def __init__(self, key: str, label: str, get: Callable[[dict], float | None],
                 higher_is_better: bool, fmt: str, guardrail: bool = False,
                 hint: str = ""):
        self.key = key
        self.label = label
        self.get = get
        self.higher_is_better = higher_is_better
        self.fmt = fmt
        self.guardrail = guardrail
        self.hint = hint


def _f(row: dict, key: str) -> float | None:
    try:
        v = row[key]
    except (KeyError, IndexError):
        return None
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _cpe(row: dict) -> float | None:
    """Cost per 1000 net lines. Falls back to cost/net_loc for pre-migration rows."""
    v = _f(row, "cpe")
    if v is not None:
        return v
    cost, net = _f(row, "cost_usd"), _f(row, "net_loc")
    if cost is None or not net or net <= 0:
        return None
    return cost / (net / 1000.0)


METRICS: dict[str, Metric] = {m.key: m for m in (
    Metric("tcer", "TCER（净增行/百万Token）", lambda r: _f(r, "tcer"), True, "float2",
           hint="产出效率：每百万 token 换来多少净增代码行"),
    Metric("score", "综合效率分（0–100）", lambda r: _f(r, "score"), True, "float2",
           hint="三正交轴（产出效率·成本·质量）各比参考线、按会话规模收缩后加权的 0–100 分"),
    Metric("net_loc", "净增代码行/会话", lambda r: _f(r, "net_loc"), True, "int",
           hint="单次会话的绝对产出规模"),
    Metric("cpe", "成本/千行（$）", _cpe, False, "money",
           hint="金钱效率：每千行净增代码的花费"),
    Metric("cost_usd", "成本/会话（$）", lambda r: _f(r, "cost_usd"), False, "money"),
    Metric("total_tokens", "Token/会话", lambda r: _f(r, "total_tokens"), False, "int"),
    Metric("churn_ratio", "自返工率", lambda r: _f(r, "churn_ratio"), False, "pct",
           guardrail=True,
           hint="本会话写出后又被自己删改的比例；GitClear 2026 显示 AI 代码两周内 churn 上升 15%"),
    Metric("tool_error_rate", "工具错误率", lambda r: _f(r, "tool_error_rate"), False, "pct",
           guardrail=True, hint="工具调用失败占比，反映 harness / 权限配置是否顺手"),
    Metric("chr", "缓存命中率", lambda r: _f(r, "chr"), True, "pct",
           hint="缓存读占总输入的比例，直接决定单位成本"),
    Metric("read_before_write", "先读后写率", lambda r: _f(r, "read_before_write"), True, "pct",
           guardrail=True, hint="修改文件前是否先读过；低值意味着盲写"),
    Metric("session_duration_minutes", "会话时长（分钟）",
           lambda r: _f(r, "session_duration_minutes"), False, "float1"),
)}

PRIMARY_METRIC = "tcer"
GUARDRAILS = tuple(k for k, m in METRICS.items() if m.guardrail)


# --------------------------------------------------------------------------- #
# Dimensions: how a row maps to cohort label(s)
# --------------------------------------------------------------------------- #
def _single(col: str) -> Callable[[dict], list[str]]:
    def get(row: dict) -> list[str]:
        try:
            v = row[col]
        except (KeyError, IndexError):
            return []
        if v is None:
            return []
        s = str(v).strip()
        return [s] if s else []
    return get


def _loads(row: dict, col: str) -> dict:
    try:
        raw = row[col]
    except (KeyError, IndexError):
        return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _mcp_servers(row: dict) -> list[str]:
    """MCP servers used in a session, from ``mcp__<server>__<tool>`` tool keys.

    Works for every source — MCP tool ids carry the server name, so this needs
    no extra client-side capture.
    """
    out = set()
    for name in _loads(row, "tool_calls_json"):
        if isinstance(name, str) and name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 3 and parts[1]:
                out.add(parts[1])
    return sorted(out)


def _variant_prefix(prefix: str) -> Callable[[dict], list[str]]:
    def get(row: dict) -> list[str]:
        out = set()
        for name in _loads(row, "tool_variants_json"):
            if isinstance(name, str) and name.startswith(prefix):
                val = name[len(prefix):].strip()
                if val:
                    out.add(val)
        return sorted(out)
    return get


def _subagents(row: dict) -> list[str]:
    return sorted(set(_variant_prefix("Agent:")(row)) | set(_variant_prefix("Task:")(row)))


class Dimension:
    """A knob to compare on.

    ``multi=True`` means a session can belong to several cohorts at once (it
    used three skills). Those are compared as **used vs. not-used** rather than
    value-vs-value, which is the only framing that answers "is this skill
    pulling its weight".
    """

    def __init__(self, key: str, label: str, get: Callable[[dict], list[str]],
                 multi: bool = False, hint: str = ""):
        self.key = key
        self.label = label
        self.get = get
        self.multi = multi
        self.hint = hint


DIMENSIONS: dict[str, Dimension] = {d.key: d for d in (
    Dimension("model", "模型", _single("c_model"), hint="按归一后的模型 id 分组"),
    Dimension("source", "Agent 工具", _single("source"),
              hint="Claude Code / Codex / Grok / OpenCode / omp / Pi"),
    Dimension("reasoning_effort", "推理档位", _single("reasoning_effort")),
    Dimension("permission_profile", "权限档", _single("permission_profile")),
    Dimension("approval_policy", "审批策略", _single("approval_policy")),
    Dimension("collaboration_mode", "协作模式", _single("collaboration_mode")),
    Dimension("cli_version", "CLI 版本", _single("cli_version")),
    Dimension("task_type", "任务类型", _single("task_type")),
    Dimension("person", "人员", _single("c_person")),
    Dimension("project", "项目", _single("c_project")),
    Dimension("skill", "Skill", _variant_prefix("Skill:"), multi=True,
              hint="会话内调用过的 Skill（目前仅 Claude 源可解析）"),
    Dimension("subagent", "子代理", _subagents, multi=True,
              hint="会话内派发过的 subagent 类型（目前仅 Claude 源可解析）"),
    Dimension("mcp", "MCP / 插件", _mcp_servers, multi=True,
              hint="从 mcp__<server>__<tool> 工具名派生，四个数据源通用"),
)}


# --------------------------------------------------------------------------- #
# Robust statistics
# --------------------------------------------------------------------------- #
def median(vals: Iterable[float]) -> float | None:
    s = sorted(vals)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def winsorized_mean(vals: list[float], frac: float = WINSOR_FRACTION) -> float | None:
    """Mean after clipping the extreme ``frac`` tails to the tail quantiles.

    Keeps every session in the average (unlike trimming) while denying any one
    outlier unbounded leverage.
    """
    if not vals:
        return None
    s = sorted(vals)
    lo, hi = quantile(s, frac), quantile(s, 1 - frac)
    return sum(min(max(v, lo), hi) for v in s) / len(s)


def describe(vals: list[float]) -> dict:
    """Robust summary of one cohort's per-session values."""
    if not vals:
        return {"n": 0, "median": None, "winsor_mean": None,
                "p25": None, "p75": None, "min": None, "max": None}
    s = sorted(vals)
    return {
        "n": len(s),
        "median": median(s),
        "winsor_mean": winsorized_mean(s),
        "p25": quantile(s, 0.25),
        "p75": quantile(s, 0.75),
        "min": s[0],
        "max": s[-1],
    }


# --------------------------------------------------------------------------- #
# Stratification
# --------------------------------------------------------------------------- #
def size_bands(rows: list[dict]) -> dict[int, str]:
    """Map row id → S/M/L size band by ``total_tokens`` terciles.

    Session size is the strongest nuisance variable in this data: a 2M-token
    session and a 20k-token one are different kinds of work, not different
    levels of skill. Banding lets comparisons happen between like and like.
    """
    sized = [(r.get("id"), _f(r, "total_tokens") or 0.0) for r in rows]
    vals = sorted(v for _, v in sized)
    if len(vals) < 3:
        return {rid: "M" for rid, _ in sized}
    t1, t2 = quantile(vals, 1 / 3), quantile(vals, 2 / 3)
    out = {}
    for rid, v in sized:
        out[rid] = "S" if v <= t1 else ("M" if v <= t2 else "L")
    return out


def stratum_key(row: dict, bands: dict[int, str]) -> str:
    task = (row.get("task_type") or "未标注")
    return f"{task}|{bands.get(row.get('id'), 'M')}"


# --------------------------------------------------------------------------- #
# Stratified difference + bootstrap interval
# --------------------------------------------------------------------------- #
def _pooled_diff(strata: list[tuple[list[float], list[float]]]) -> float | None:
    """Precision-weighted pooling of per-stratum median differences.

    Weight ``n_a·n_b/(n_a+n_b)`` is the standard precision proxy for a
    two-sample difference: a stratum contributes in proportion to how much it
    can actually resolve, so a 40-vs-2 stratum doesn't outvote a 20-vs-20 one.
    """
    num = 0.0
    den = 0.0
    for a, b in strata:
        ma, mb = median(a), median(b)
        if ma is None or mb is None:
            continue
        w = (len(a) * len(b)) / float(len(a) + len(b))
        num += w * (ma - mb)
        den += w
    return num / den if den else None


def compare_groups(a_vals_by_stratum: dict[str, list[float]],
                   b_vals_by_stratum: dict[str, list[float]],
                   iters: int = BOOTSTRAP_ITERS) -> dict:
    """Stratified median difference (a − b) with a bootstrap 95% interval.

    Strata where either side has fewer than ``MIN_STRATUM_PER_SIDE`` sessions
    can't support a within-stratum comparison and are dropped. If that leaves
    nothing, we fall back to the pooled (unstratified) difference and say so via
    ``stratified: False`` — a weaker claim, reported as such, beats silence.
    """
    usable = [
        (a_vals_by_stratum[k], b_vals_by_stratum[k])
        for k in sorted(set(a_vals_by_stratum) & set(b_vals_by_stratum))
        if len(a_vals_by_stratum[k]) >= MIN_STRATUM_PER_SIDE
        and len(b_vals_by_stratum[k]) >= MIN_STRATUM_PER_SIDE
    ]
    stratified = bool(usable)
    if not stratified:
        flat_a = [v for vs in a_vals_by_stratum.values() for v in vs]
        flat_b = [v for vs in b_vals_by_stratum.values() for v in vs]
        if not flat_a or not flat_b:
            return {"diff": None, "ci_low": None, "ci_high": None,
                    "stratified": False, "strata_used": 0}
        usable = [(flat_a, flat_b)]

    point = _pooled_diff(usable)
    if point is None:
        return {"diff": None, "ci_low": None, "ci_high": None,
                "stratified": stratified, "strata_used": len(usable)}

    rng = random.Random(BOOTSTRAP_SEED)
    draws: list[float] = []
    for _ in range(iters):
        resampled = [
            ([a[rng.randrange(len(a))] for _ in a],
             [b[rng.randrange(len(b))] for _ in b])
            for a, b in usable
        ]
        d = _pooled_diff(resampled)
        if d is not None:
            draws.append(d)
    if not draws:
        return {"diff": point, "ci_low": None, "ci_high": None,
                "stratified": stratified, "strata_used": len(usable)}
    draws.sort()
    return {
        "diff": point,
        "ci_low": quantile(draws, CI_ALPHA / 2),
        "ci_high": quantile(draws, 1 - CI_ALPHA / 2),
        "stratified": stratified,
        "strata_used": len(usable),
    }


def grade(n_a: int, n_b: int, cmp_: dict) -> str:
    """Evidence label. Deliberately conservative — see module docstring."""
    if n_a < MIN_COHORT_SESSIONS or n_b < MIN_COHORT_SESSIONS:
        return "insufficient"
    lo, hi = cmp_.get("ci_low"), cmp_.get("ci_high")
    if lo is None or hi is None:
        return "weak"
    if lo <= 0 <= hi:
        return "none"          # interval spans zero → no measurable difference
    if not cmp_.get("stratified") or min(n_a, n_b) < 10:
        return "moderate"
    return "strong"


GRADE_LABEL = {
    "strong": "证据较强",
    "moderate": "证据中等",
    "weak": "证据薄弱",
    "none": "无显著差异",
    "insufficient": "样本不足",
}


# --------------------------------------------------------------------------- #
# Cohort comparison
# --------------------------------------------------------------------------- #
def _cohort_membership(rows: list[dict], dim: Dimension) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        for label in dim.get(r):
            out.setdefault(label, []).append(r)
    return out


def _by_stratum(rows: list[dict], metric: Metric,
                bands: dict[int, str]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        v = metric.get(r)
        if v is None:
            continue
        out.setdefault(stratum_key(r, bands), []).append(v)
    return out


def compare(rows: list[dict], dimension: str, metric_key: str = PRIMARY_METRIC,
            min_sessions: int = MIN_COHORT_SESSIONS) -> dict:
    """Compare every cohort of ``dimension`` on ``metric_key``.

    Each cohort is contrasted against **the rest of the filtered rows** (for
    multi-valued dimensions: the sessions that did *not* use it), stratified by
    task type × session size.
    """
    dim = DIMENSIONS.get(dimension)
    metric = METRICS.get(metric_key)
    if dim is None:
        raise ValueError(f"unknown dimension: {dimension}")
    if metric is None:
        raise ValueError(f"unknown metric: {metric_key}")

    bands = size_bands(rows)
    members = _cohort_membership(rows, dim)
    ids_by_label = {k: {id(r) for r in v} for k, v in members.items()}

    cohorts = []
    for label, grp in members.items():
        rest = [r for r in rows if id(r) not in ids_by_label[label]]
        vals = [v for v in (metric.get(r) for r in grp) if v is not None]
        rest_vals = [v for v in (metric.get(r) for r in rest) if v is not None]
        cmp_ = compare_groups(_by_stratum(grp, metric, bands),
                              _by_stratum(rest, metric, bands))
        g = grade(len(vals), len(rest_vals), cmp_)
        base = median(rest_vals)
        rel = None
        if cmp_["diff"] is not None and base:
            rel = cmp_["diff"] / abs(base)
        # The raw difference of medians, i.e. what a normal dashboard would
        # show. Keeping it next to the stratified estimate makes the size of the
        # confounding visible instead of asking the reader to trust the method.
        med_a, med_b = median(vals), median(rest_vals)
        naive = (med_a - med_b) if (med_a is not None and med_b is not None) else None
        naive_rel = (naive / abs(base)) if (naive is not None and base) else None
        # "Better" always means better *for this metric's direction*, so the UI
        # never has to know whether up or down is good.
        signed = cmp_["diff"]
        if signed is not None and not metric.higher_is_better:
            signed = -signed
        cohorts.append({
            "label": label,
            "sessions": len(grp),
            "stats": describe(vals),
            "contrast_stats": describe(rest_vals),
            "diff": cmp_["diff"],
            "diff_oriented": signed,
            "rel_diff": rel,
            "naive_diff": naive,
            "naive_rel_diff": naive_rel,
            "ci_low": cmp_["ci_low"],
            "ci_high": cmp_["ci_high"],
            "stratified": cmp_["stratified"],
            "strata_used": cmp_["strata_used"],
            "grade": g,
            "grade_label": GRADE_LABEL[g],
            "guardrails": _guardrail_summary(grp, rows, ids_by_label[label], bands),
        })

    # Rank: conclusive cohorts first (by oriented effect), then the rest by
    # median. Cohorts we can't speak about must not appear to be "worst".
    def sort_key(c):
        conclusive = c["grade"] in ("strong", "moderate")
        eff = c["diff_oriented"] if c["diff_oriented"] is not None else float("-inf")
        med = c["stats"]["median"]
        med = (med if metric.higher_is_better else -med) if med is not None else float("-inf")
        return (0 if conclusive else 1, -eff if conclusive else -med)

    cohorts.sort(key=sort_key)
    return {
        "dimension": dimension,
        "dimension_label": dim.label,
        "multi": dim.multi,
        "metric": metric_key,
        "metric_label": metric.label,
        "higher_is_better": metric.higher_is_better,
        "fmt": metric.fmt,
        "min_sessions": min_sessions,
        "total_sessions": len(rows),
        "cohorts": cohorts,
        "caveat": (
            "观测数据、非随机分配：分层只能控住任务类型与会话规模两个混杂因素，"
            "无法排除「难题更常派给某个模型」这类选择偏差。结论为相关性，不是因果。"
        ),
    }


def _guardrail_summary(grp: list[dict], rows: list[dict], grp_ids: set,
                       bands: dict[int, str]) -> dict:
    """Guardrail metrics for a cohort vs. the rest — always shown alongside.

    A cohort that ships more lines per token while quietly doubling rework has
    not won. Reporting speed without quality is the failure mode DX's 2026 data
    and GitClear's maintainability research both point at.
    """
    rest = [r for r in rows if id(r) not in grp_ids]
    out = {}
    for key in GUARDRAILS:
        m = METRICS[key]
        vals = [v for v in (m.get(r) for r in grp) if v is not None]
        rest_vals = [v for v in (m.get(r) for r in rest) if v is not None]
        cmp_ = compare_groups(_by_stratum(grp, m, bands), _by_stratum(rest, m, bands))
        g = grade(len(vals), len(rest_vals), cmp_)
        signed = cmp_["diff"]
        if signed is not None and not m.higher_is_better:
            signed = -signed
        out[key] = {
            "label": m.label,
            "median": median(vals),
            "contrast_median": median(rest_vals),
            "diff_oriented": signed,
            "grade": g,
            "fmt": m.fmt,
        }
    return out
