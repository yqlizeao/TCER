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
    # --- 新增高信号规则阈值 ---
    EXPLORE_LOW = 0.05        # 探索比过低：几乎不搜代码就动手（易改错地方）
    EXPLORE_HIGH = 0.55       # 探索比过高：大量搜索却少产出（可能卡在找不到）
    BASH_HIGH = 0.45          # Bash 占比过高：靠命令行试探而非读代码
    HIGH_CHURN_FILES = 2      # 反复重改同一批文件的文件数
    EDIT_VERIFY_LOW = 0.20    # 改完很少验证（改→跑）
    EDIT_VERIFY_HIGH = 0.70   # 改完基本都验证（好习惯）
    TEST_RATIO_LOW = 0.02     # 几乎没写测试（有一定产出规模时才提）
    TEST_RATIO_GOOD = 0.15    # 测试占比健康
    CTX_HIGH = 0.80           # 上下文窗口占用过高（接近塞满，易丢前文）
    CACHE_EFF_LOW = 1.0       # 缓存读 < 缓存写：缓存没回本
    CORRECTION_MSGS = 3       # 反复纠正 AI 的消息数（提示没说清）
    NET_LOC_MIN_FOR_TEST = 100  # 只有产出上规模才评测试覆盖
    # --- 金额类阈值 ---
    COST_HIGH_USD = 5.0       # 单次会话花费偏高（绝对金额，美元）
    CPE_HIGH_MULT = 1.5       # 每千行成本高于个人基准的倍数（1.5× 即偏贵）
    CHURN_COST_MIN = 0.30     # 返工到此比例即视为「在烧钱重写」


def _pct(x):
    return "-" if x is None else f"{x * 100:.0f}%"


# key -> (title, action). evidence built per-call with live numbers via .format().
# 文案原则（说人话）：title 一句话点出现象；action 给「照着做就行」的具体动作，
# 不堆术语、不空喊「优化」；亮点(good)不带 action（表扬无需下一步）。
_COPY: dict[str, tuple[str, str]] = {
    "unscored": ("这次会话没算出效率分",
                 "如果你希望它计分，确认这次确实有写或改代码（Write / Edit），且没开「跳过代码统计」。"
                 "纯问答、只看代码、查资料的会话本来就不计分，属正常。"),
    "unscored_ev": ("这次没有新增代码，也没有成本数据——看起来是纯调研或代码审查", ""),

    "churn_high": ("AI 在反复推翻自己刚写的代码",
                   "下次把需求一次说清楚，别中途改方向；让它先讲思路、你确认后再动笔；"
                   "小步改、一次只动一处，别整个文件重写。"),
    "churn_high_ev": ("有 {v} 的代码是写完又被自己删掉重来的", ""),
    "churn_low": ("基本一次写对，很少返工", ""),
    "churn_low_ev": ("只有 {v} 的代码被回炉重写", ""),

    "err_high": ("AI 敲的命令经常报错",
                 "先告诉它这个项目怎么跑（用什么命令、在哪个目录、依赖装没装）；"
                 "把常用命令写进项目说明，省得它一遍遍试错。"),
    "err_low": ("命令和工具调用几乎不出错", ""),
    "err_ev": ("{n} 次工具调用里有 {v} 失败了", ""),

    "rbw_low": ("经常没看文件内容就直接改",
                "要求它「先读再动手」——改一个文件前，先把这个文件读一遍，"
                "不然容易改错地方或漏掉上下文。"),
    "rbw_high": ("动手前会先读代码，很少盲改", ""),
    "rbw_ev": ("只有 {v} 的改动是先读后改的", ""),

    "unseen": ("有整个文件被直接覆盖写，可能盖掉原内容",
               "对已经存在的文件，让它用 Edit 局部改，别用 Write 整篇覆盖——"
               "覆盖既有丢内容风险，也会让「新增行数」虚高。"),
    "unseen_ev": ("有 {n} 个已存在的文件是没读过就被整篇重写的", ""),

    "chr_low": ("缓存没怎么用上，白花了钱",
                "让每次对话的开头（系统提示、贴的长文件）尽量保持不变——"
                "变动越少，越多内容能走便宜的缓存读，成本自然降。"),
    "chr_high": ("缓存用得很充分，省了成本", ""),
    "chr_ev": ("只有 {v} 的输入命中了缓存", ""),

    "edit_low": ("改代码时整篇重写多、局部修改少",
                 "改已有文件时优先用 Edit 只改那几行——比整篇 Write 更准，返工少，也不会误盖别的内容。"),
    "edit_ev": ("改动里只有 {v} 是局部修改，其余是整篇重写", ""),

    "neutral": ("各方面都中规中矩，没有明显长短板",
                "想再提分，先看「得分构成」里最低的那一轴，照它下面的建议改一处就行。"),
    "neutral_ev": ("综合效率分 {v:.0f}，三条轴都在中间水平", ""),

    "axis_output_good": ("产出效率很高：花的 token 换来了实打实的代码", ""),
    "axis_output_bad": ("产出效率偏低：花了不少 token，落地的代码却少",
                        "把任务拆小、目标说具体，减少来回确认和空转；让它专注写代码，别在长篇解释上耗 token。"),
    "axis_output_ev": ("产出效率 {v}（0.5 是及格线，越高越划算）", ""),
    "axis_cost_good": ("单位成本控制得好：写代码花得少", ""),
    "axis_cost_bad": ("单位成本偏高：写同样多代码，花得比基准多",
                      "两手抓——让缓存多命中（开头别老变）、少让它返工重写，成本就下来了。"),
    "axis_cost_ev": ("成本表现 {v}（0.5 是及格线，越高越省）", ""),
    "axis_quality_good": ("过程质量高：少返工、少报错、先读后改", ""),
    "axis_quality_bad": ("过程质量偏低：返工、报错或偏多",
                         "养成三个习惯就能明显改善：改前先读、小步改、改完跑一下验证。"),
    "axis_quality_ev": ("质量表现 {v}（看返工/报错/先读后改，0.5 是及格线）", ""),

    # --- 新增高信号规则 ---
    "explore_low": ("几乎没查代码就动手了",
                    "让它动手前先搜一下（grep 关键字 / 找相关文件），确认改的是对的地方，别凭猜改。"),
    "explore_low_ev": ("只有 {v} 的操作是在搜索/定位代码", ""),
    "explore_high": ("大量时间在翻找代码，真正产出偏少",
                     "可能是它没找到目标在打转——直接告诉它相关文件路径或函数名，省得反复搜。"),
    "explore_high_ev": ("{v} 的操作都花在搜索/翻找上", ""),

    "bash_high": ("过度靠敲命令摸索，而不是读代码",
                  "多让它直接读源码理解逻辑，少用一连串命令试探——读代码往往比试命令更快更准。"),
    "bash_high_ev": ("{v} 的工具调用是在跑命令行", ""),

    "highchurn_files": ("同一批文件被反复改了很多遍",
                        "文件反复回炉，通常是需求没定或设计没想清。先和 AI 把这几个文件要改成什么样谈定，再让它一次改到位。"),
    "highchurn_files_ev": ("有 {n} 个文件被改了 3 遍以上", ""),

    "verify_low": ("改完代码很少跑一下验证",
                   "让它「改完就验」——写完顺手跑个测试或命令确认没坏，问题早发现，省得后面返工。"),
    "verify_low_ev": ("改完后只有 {v} 的情况会跑命令验证", ""),
    "verify_high": ("改完基本都会跑验证，闭环好", ""),
    "verify_high_ev": ("{v} 的改动改完都验证了", ""),

    "test_low": ("写了不少代码，却几乎没配测试",
                 "让它给关键逻辑补上测试——哪怕几个用例，也能挡住以后的回归 bug。"),
    "test_low_ev": ("测试代码只占产出的 {v}", ""),
    "test_good": ("代码有配套测试，习惯好", ""),
    "test_good_ev": ("测试代码占产出的 {v}", ""),

    "ctx_high": ("上下文快塞满了，AI 容易忘掉前面说的",
                 "适时新开一轮对话或做一次总结压缩，别在一轮里堆太长——上下文越满，越容易丢前文、答偏。"),
    "ctx_high_ev": ("上下文窗口用到了 {v}", ""),

    "cache_eff_low": ("缓存写得多、读得少，没回本",
                      "缓存是「写一次、后面反复读才划算」。如果每轮上下文都在变，"
                      "写进去的缓存来不及被读就失效了——尽量让开头稳定、多轮复用。"),
    "cache_eff_low_ev": ("缓存读只有写的 {v} 倍（大于 1 才算回本）", ""),

    "correction_high": ("你反复在纠正 AI，说明它常没领会意图",
                        "开头一次性把背景、目标、约束、期望产出讲清楚，比事后一句句纠正省时间；"
                        "也可以先让它复述一遍，理解对了再干。"),
    "correction_high_ev": ("这次你发了 {n} 条纠正/返工的消息", ""),

    # --- 金额类（cost）：橙黄色单列。花钱不是错——解决了问题、学到了东西就值。
    # 语气俏皮，只做观察和「值不值」的提醒，不以省钱为目标。
    "cost_high": ("土豪！这一单挥金如土",
                  "花得多不叫浪费——只要问题解决了、东西学到了，这钱就花得值。"
                  "要是没换来什么，下次再考虑省着点。"),
    "cost_high_ev": ("本次会话花费约 ${v}，出手阔绰", ""),
    "cpe_high": ("每千行代码走的是「精装修」预算",
                 "单位成本比你平时高——如果是啃硬骨头、探新路，贵得有道理；"
                 "要只是日常搬砖，那可以回看下是不是返工或缓存拖了后腿。"),
    "cpe_high_ev": ("每千行成本 ${v}，约为你平常（${b}）的 {mult} 倍", ""),
    "churn_cost": ("有一笔钱是花在「反复横跳」上的",
                   "AI 推翻自己重写的那部分 token，是实打实没换来产出的开销。"
                   "下次先把需求/方案聊定再落笔，能少烧这一份。"),
    "churn_cost_ev": ("约 {v} 的代码是返工重写，这部分 token 基本是白花的", ""),
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
    cost: list[Insight] = []

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

    # --- 探索比：搜代码 vs 直接动手（需足够工具样本才有意义）---
    explore = report.exploration_ratio
    if explore is not None and total_tools >= _TH.TOOL_MIN_CALLS:
        if explore < _TH.EXPLORE_LOW and (report.net_loc or 0) > 0:
            t, a = _c("explore_low")
            drag.append(Insight("drag", t, _c("explore_low_ev")[0].format(v=_pct(explore)), a, "exploration_ratio"))
        elif explore > _TH.EXPLORE_HIGH:
            t, a = _c("explore_high")
            tip.append(Insight("tip", t, _c("explore_high_ev")[0].format(v=_pct(explore)), a, "exploration_ratio"))

    # --- Bash 占比过高：靠命令行试探而非读代码 ---
    bash_r = report.bash_ratio
    if (bash_r is not None and bash_r > _TH.BASH_HIGH
            and total_tools >= _TH.TOOL_MIN_CALLS):
        t, a = _c("bash_high")
        tip.append(Insight("tip", t, _c("bash_high_ev")[0].format(v=_pct(bash_r)), a, "bash_ratio"))

    # --- 反复重改同一批文件（设计/需求没定的信号）---
    if report.high_churn_file_count >= _TH.HIGH_CHURN_FILES:
        t, a = _c("highchurn_files")
        drag.append(Insight("drag", t, _c("highchurn_files_ev")[0].format(n=report.high_churn_file_count), a, "high_churn_file_count"))

    # --- 改完验证闭环（改→跑）---
    verify = report.edit_verify_ratio
    if verify is not None:
        if verify < _TH.EDIT_VERIFY_LOW and (report.net_loc or 0) > 0:
            t, a = _c("verify_low")
            drag.append(Insight("drag", t, _c("verify_low_ev")[0].format(v=_pct(verify)), a, "edit_verify_ratio"))
        elif verify > _TH.EDIT_VERIFY_HIGH:
            t, a = _c("verify_high")
            good.append(Insight("good", t, _c("verify_high_ev")[0].format(v=_pct(verify)), a, "edit_verify_ratio"))

    # --- 测试覆盖（仅在产出上规模时评）---
    test_r = report.test_loc_ratio
    if test_r is not None and (report.net_loc or 0) >= _TH.NET_LOC_MIN_FOR_TEST:
        if test_r < _TH.TEST_RATIO_LOW:
            t, a = _c("test_low")
            tip.append(Insight("tip", t, _c("test_low_ev")[0].format(v=_pct(test_r)), a, "test_loc_ratio"))
        elif test_r >= _TH.TEST_RATIO_GOOD:
            t, a = _c("test_good")
            good.append(Insight("good", t, _c("test_good_ev")[0].format(v=_pct(test_r)), a, "test_loc_ratio"))

    # --- 上下文窗口占用过高（易丢前文）---
    ctx = report.context_window_used_ratio
    if ctx is not None and ctx > _TH.CTX_HIGH:
        t, a = _c("ctx_high")
        tip.append(Insight("tip", t, _c("ctx_high_ev")[0].format(v=_pct(ctx)), a, "context_window_used_ratio"))

    # --- 缓存没回本（写多读少）---
    cache_eff = report.cache_efficiency
    if cache_eff is not None and cache_eff < _TH.CACHE_EFF_LOW and (report.usage.cache_creation_input_tokens or 0) > 0:
        t, a = _c("cache_eff_low")
        tip.append(Insight("tip", t, _c("cache_eff_low_ev")[0].format(v=f"{cache_eff:.2f}"), a, "cache_efficiency"))

    # --- 反复纠正 AI（提示没说清的信号）---
    corrections = report.usage.correction_msg_count
    if corrections is not None and corrections >= _TH.CORRECTION_MSGS:
        t, a = _c("correction_high")
        tip.append(Insight("tip", t, _c("correction_high_ev")[0].format(n=corrections), a, "correction_msg_count"))

    # --- 金额类（cost）：花钱相关的观察，俏皮语气、只谈值不值 ---
    _cost_insights(report, cost)

    ordered = good + drag + cost + tip
    if not ordered:
        t, a = _c("neutral")
        ordered = [Insight("tip", t, _c("neutral_ev")[0].format(v=report.score), a, "score")]
    return ordered


def _cost_insights(report: SessionReport, cost: list) -> None:
    """金额类洞察：单次花费、单位成本 vs 基准、返工烧钱。全部 grounding 到真实
    成本字段。与「成本轴」拖累项互补——那给相对基准的分值，这里给可感知的绝对
    金额。语气俏皮：花钱不是错，解决问题/学到东西就值，只做「值不值」的提醒。"""
    from tcer.core import metrics

    # 单次会话花费偏高（绝对美元）
    c = report.cost
    if c is not None and c >= _TH.COST_HIGH_USD:
        t, a = _c("cost_high")
        cost.append(Insight("cost", t, _c("cost_high_ev")[0].format(v=f"{c:.2f}"), a, "cost"))

    # 每千行成本高于个人基准（倍数）
    cpe = report.cpe
    base = metrics.CPE_BASELINE
    if (cpe is not None and cpe > 0 and base and base > 0
            and cpe >= base * _TH.CPE_HIGH_MULT):
        t, a = _c("cpe_high")
        ev = _c("cpe_high_ev")[0].format(v=f"{cpe:.1f}", b=f"{base:.1f}", mult=f"{cpe / base:.1f}")
        cost.append(Insight("cost", t, ev, a, "cpe"))

    # 返工在烧钱（高返工时，重写的 token 是白花的钱）
    churn = report.churn_ratio
    if churn is not None and churn >= _TH.CHURN_COST_MIN and (report.net_loc or 0) > 0:
        t, a = _c("churn_cost")
        cost.append(Insight("cost", t, _c("churn_cost_ev")[0].format(v=_pct(churn)), a, "cost_churn"))


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
