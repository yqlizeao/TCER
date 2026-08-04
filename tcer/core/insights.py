"""Session insight engine: turn computed metrics into actionable diagnostics.

Offline, deterministic rule engine (no LLM). Reads a SessionReport and emits
structured Insight objects, modelled on Claude Code's /insights (What's Working /
What's Hindering / Quick Wins) and /doctor (finding -> concrete fix). Every value
comes from session-data replay, same source as the grid/ranking, so each insight
is grounded, reproducible, and testable.
"""
from __future__ import annotations

from dataclasses import dataclass

from tcer.core.models import SessionReport


@dataclass(frozen=True)
class Insight:
    kind: str          # good | drag | tip
    title: str
    evidence: str
    action: str = ""
    metric: str = ""


class _TH:
    CHURN_HIGH = 0.30
    CHURN_LOW = 0.05
    TOOL_ERR_HIGH = 0.15
    TOOL_ERR_LOW = 0.02
    TOOL_MIN_CALLS = 10
    RBW_LOW = 0.40
    RBW_HIGH = 0.80
    CHR_LOW = 0.50
    CHR_HIGH = 0.85
    AXIS_STRONG = 0.65
    AXIS_WEAK = 0.35
    UNSEEN_WRITES = 3
    EDIT_RATIO_LOW = 0.30


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


# key -> (title, action). evidence built per-call with live numbers via .format().
_COPY: dict[str, tuple[str, str]] = {
    "unscored": ("本会话暂不可评分",
                 "若期望计分：确认会话确有 Write/Edit 产出，且未开启「跳过 LOC」。"),
    "unscored_ev": ("无净增代码行或成本数据（可能是纯调研/审查，或已跳过 LOC）", ""),
    "churn_high": ("返工偏多：AI 反复重写自己刚写的代码",
                   "把大改拆成小步；先让 AI 说方案再落笔；用 Edit 局部改而非整文件重写。"),
    "churn_high_ev": ("返工率 {v}（写入后又被自己删改的行占比）", ""),
    "churn_low": ("一次写对率高，几乎不返工", ""),
    "churn_low_ev": ("返工率仅 {v}", ""),
    "err_high": ("工具调用错误率偏高",
                 "检查命令/路径是否常失败；给 AI 更明确的运行环境与前置条件。"),
    "err_low": ("工具调用几乎不出错", ""),
    "err_ev": ("工具错误率 {v}（共 {n} 次调用）", ""),
    "rbw_low": ("常在没读文件的情况下就动手改",
                "让 AI 改文件前先 Read/Grep 目标；提示里要求「先看再改」。"),
    "rbw_high": ("改动前先读，盲改少", ""),
    "rbw_ev": ("先读后写率 {v}", ""),
    "unseen": ("多次「盲写」整文件（可能覆盖已有内容）",
               "对已存在的文件优先用 Edit 增量修改，避免 Write 覆盖导致净增行虚高。"),
    "unseen_ev": ("{n} 个文件在未先读取的情况下被整体 Write", ""),
    "chr_low": ("缓存命中率低，成本有下降空间",
                "保持提示词/文件开头稳定，少动上下文前缀，让更多 token 走缓存读。"),
    "chr_high": ("缓存利用充分，省成本", ""),
    "chr_ev": ("缓存命中率 {v}", ""),
    "edit_low": ("多用 Edit 而非整文件 Write",
                 "改已有文件时优先 Edit：更精确、返工更少、也少触发覆盖写风险。"),
    "edit_ev": ("Edit 占改动操作的 {v}", ""),
    "neutral": ("各维度均处于中等区间",
                "想进一步提分：优先看得分构成里最低的一轴，对照其改进建议。"),
    "neutral_ev": ("综合效率分 {v:.0f}，三轴无明显短板或亮点", ""),
    "axis_output_good": ("产出效率轴表现突出", ""),
    "axis_output_bad": ("产出效率轴明显偏低，是主要失分项",
                        "让提示更聚焦、减少来回；避免让 AI 做无产出的空转。"),
    "axis_output_ev": ("产出效率 {v}（每百万 token 净产出，0.5=与基准持平）", ""),
    "axis_cost_good": ("成本轴表现突出", ""),
    "axis_cost_bad": ("成本轴明显偏低，是主要失分项",
                      "提高缓存命中、减少重写，降低每千行成本。"),
    "axis_cost_ev": ("成本 {v}（每千行花费，0.5=与基准持平）", ""),
    "axis_quality_good": ("质量轴表现突出", ""),
    "axis_quality_bad": ("质量轴明显偏低，是主要失分项",
                         "改前先读、小步提交、用 Edit 增量改。"),
    "axis_quality_ev": ("质量 {v}（少返工/少报错/先读后写，0.5=与基准持平）", ""),
}


def _c(key: str) -> tuple[str, str]:
    return _COPY.get(key, (key, ""))


_AXES = (
    ("score_output_axis", "axis_output"),
    ("score_cost_axis", "axis_cost"),
    ("score_quality_axis", "axis_quality"),
)


def _axis_insights(report, good, drag):
    """Translate the three axis scores (0-1) into good/drag findings."""
    for attr, key in _AXES:
        val = getattr(report, attr, None)
        if val is None:
            continue
        ev = _c(key + "_ev")[0].format(v=f"{val:.2f}")
        if val >= _TH.AXIS_STRONG:
            good.append(Insight("good", _c(key + "_good")[0], ev, "", attr))
        elif val < _TH.AXIS_WEAK:
            t, a = _c(key + "_bad")
            drag.append(Insight("drag", t, ev, a, attr))


def session_insights(report: SessionReport) -> list[Insight]:
    """Actionable insights for one session: good first, drags, then tips."""
    if report.score is None:
        t, a = _c("unscored")
        return [Insight("tip", t, _c("unscored_ev")[0], a, "score")]

    good: list[Insight] = []
    drag: list[Insight] = []
    tip: list[Insight] = []

    _axis_insights(report, good, drag)

    churn = report.churn_ratio
    if churn is not None:
        if churn > _TH.CHURN_HIGH:
            t, a = _c("churn_high")
            drag.append(Insight("drag", t, _c("churn_high_ev")[0].format(v=_pct(churn)), a, "churn"))
        elif churn < _TH.CHURN_LOW and (report.net_loc or 0) > 0:
            t, a = _c("churn_low")
            good.append(Insight("good", t, _c("churn_low_ev")[0].format(v=_pct(churn)), a, "churn"))

    total_tools = sum(report.usage.tool_calls.values()) if report.usage.tool_calls else 0
    err = report.tool_error_rate
    if err is not None and total_tools >= _TH.TOOL_MIN_CALLS:
        if err > _TH.TOOL_ERR_HIGH:
            t, a = _c("err_high")
            drag.append(Insight("drag", t, _c("err_ev")[0].format(v=_pct(err), n=total_tools), a, "tool_error_rate"))
        elif err < _TH.TOOL_ERR_LOW:
            t, a = _c("err_low")
            good.append(Insight("good", t, _c("err_ev")[0].format(v=_pct(err), n=total_tools), a, "tool_error_rate"))

    rbw = report.read_before_write
    if rbw is not None:
        if rbw < _TH.RBW_LOW:
            t, a = _c("rbw_low")
            drag.append(Insight("drag", t, _c("rbw_ev")[0].format(v=_pct(rbw)), a, "read_before_write"))
        elif rbw > _TH.RBW_HIGH:
            t, a = _c("rbw_high")
            good.append(Insight("good", t, _c("rbw_ev")[0].format(v=_pct(rbw)), a, "read_before_write"))

    if report.unseen_writes > _TH.UNSEEN_WRITES:
        t, a = _c("unseen")
        drag.append(Insight("drag", t, _c("unseen_ev")[0].format(n=report.unseen_writes), a, "unseen_writes"))

    chr_ = report.chr
    if chr_ is not None:
        if chr_ < _TH.CHR_LOW:
            t, a = _c("chr_low")
            tip.append(Insight("tip", t, _c("chr_ev")[0].format(v=_pct(chr_)), a, "chr"))
        elif chr_ > _TH.CHR_HIGH:
            t, a = _c("chr_high")
            good.append(Insight("good", t, _c("chr_ev")[0].format(v=_pct(chr_)), a, "chr"))

    edit_ratio = report.edit_ratio
    if (edit_ratio is not None and edit_ratio < _TH.EDIT_RATIO_LOW
            and (report.net_loc or 0) > 0 and total_tools >= _TH.TOOL_MIN_CALLS):
        t, a = _c("edit_low")
        tip.append(Insight("tip", t, _c("edit_ev")[0].format(v=_pct(edit_ratio)), a, "edit_ratio"))

    ordered = good + drag + tip
    if not ordered:
        t, a = _c("neutral")
        ordered = [Insight("tip", t, _c("neutral_ev")[0].format(v=report.score), a, "score")]
    return ordered


# ============================================================
# 跨会话（项目级）洞察：把每会话诊断聚合成「系统性」结论。
# 参考 Claude Code /insights「多次重复出现 → 优先处理」：一个 drag 只在 1 个会话
# 出现是偶发，在 8/12 个会话反复出现才是该动手的系统性问题。纯确定性聚合——
# 复用 session_insights 的规则，只统计**普遍度**（prevalence），不做 LLM 归因。
# ============================================================

# prevalence 阈值：拖累项在 >=40% 已评分会话出现才算系统性；亮点需 >=60% 才算稳定强项。
_DRAG_PREVALENCE = 0.40
_GOOD_PREVALENCE = 0.60
_MIN_SESSIONS = 2          # 少于 2 个已评分会话无跨会话信号
_MIN_COUNT = 2            # 至 2 个会话出现才纳入


def project_insights(reports: list[SessionReport]) -> list[Insight]:
    """跨会话聚合洞察：返回按普遍度排序的系统性 drag + 稳定 good。

    对每个已评分会话跑 session_insights，按 (kind, metric) 去重计数（每会话每类
    最多计一次），普遍度达阈值的升为项目级 Insight。evidence 换成「N/M 会话出现」，
    action 沿用该类的可执行建议。无跨会话信号（< 2 会话）返回 []。
    """
    scored = [r for r in reports if r.score is not None]
    n = len(scored)
    if n < _MIN_SESSIONS:
        return []

    # (kind, metric) -> [count, 代表 title, 代表 action]
    tally: dict[tuple[str, str], list] = {}
    for r in scored:
        seen: set[tuple[str, str]] = set()
        for it in session_insights(r):
            key = (it.kind, it.metric)
            if key in seen:
                continue          # 每会话每 (kind,metric) 只计一次
            seen.add(key)
            slot = tally.get(key)
            if slot is None:
                tally[key] = [1, it.title, it.action]
            else:
                slot[0] += 1

    drags: list[tuple[int, Insight]] = []
    goods: list[tuple[int, Insight]] = []
    for (kind, metric), (count, title, action) in tally.items():
        if count < _MIN_COUNT:
            continue
        prev = count / n
        ev = f"在 {count}/{n} 个会话出现（占比 {prev * 100:.0f}%）"
        if kind == "drag" and prev >= _DRAG_PREVALENCE:
            drags.append((count, Insight("drag", f"系统性：{title}", ev, action, metric)))
        elif kind == "good" and prev >= _GOOD_PREVALENCE:
            goods.append((count, Insight("good", f"稳定优势：{title}", ev, "", metric)))

    drags.sort(key=lambda t: t[0], reverse=True)
    goods.sort(key=lambda t: t[0], reverse=True)
    out = [i for _, i in drags] + [i for _, i in goods]
    if not out:
        out = [Insight(
            "tip", "暂无跨会话的系统性模式",
            f"已分析 {n} 个已评分会话，没有在多数会话反复出现的短板或亮点",
            "点选左侧单个会话，查看该会话的具体洞察与改进建议。",
            "score",
        )]
    return out
