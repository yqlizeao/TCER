"""Diagnosis engine — manager-facing "what's wrong and what to do".

The Decision Lab (``insights.py``) answers a narrow question: *which config knob
(model / tool / effort) produces more code per token*. That's useful, but it is
NOT what a CTO / eng-leader / finance owner asks when they open a dashboard. They
ask:

- 钱花得值吗？成本是不是在失控？(cost / ROI)
- 交付质量在变好还是变差？(quality / defects)
- 谁是短板、该辅导谁？(people)
- 哪个项目不健康？(projects)
- 采用在爬坡还是退坡？有没有单点依赖？(adoption)

This module produces **grounded, thresholded findings** for exactly those
questions, each with: a domain, a severity, the actual numbers behind it
(period-over-period where relevant), and a concrete next action. Nothing is
invented — every number comes from ``db._agg_metrics`` over the same rows the
rest of the serverer sees, so the diagnosis always matches the dashboards.

The statistical coht comparison from ``insights.py`` is folded in as ONE
secondary domain ("配置") rather than being the whole page.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import personas  # noqa: E402
import insights as _insights  # noqa: E402

# --------------------------------------------------------------------------- #
# Thresholds — one place to tune. Chosen to fire on genuinely actionable
# conditions, not noise. All are "worse than this is worth a manager's time".
# --------------------------------------------------------------------------- #
TH = {
    "cost_spike_rel": 0.30,       # 成本环比涨幅超此值 → 关注
    "cpe_worsen_rel": 0.20,       # 单位成本环比恶化
    "chr_low": 0.40,              # 缓存命中率过低（钱花在重复输入）
    "churn_high": 0.35,           # 团队返工率过高
    "churn_worsen_rel": 0.15,     # 返工率环比恶化
    "tool_error_high": 0.10,      # 工具错误率过高
    "rbw_low": 0.50,              # 先读后写率过低（盲改风险）
    "adoption_drop_rel": 0.25,    # 会话量环比下滑
    "entity_worse_rel": 0.20,     # 个体比团队差多少才算短板
    "concentration": 0.60,        # 单人会话占比超此值 → 单点依赖
    "min_sessions": 5,            # 个体被点名的最小样本
    "bad_tiers": ("待改进", "低效"),
}

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "ok": 3}
_DOMAIN_ORDER = {"cost": 0, "quality": 1, "people": 2, "project": 3,
                 "adoption": 4, "config": 5}


def _finding(domain, severity, title, detail, action, subject=None, metric=None):
    """One diagnosis card. ``detail`` is a list of evidence strings."""
    return {"domain": domain, "severity": severity, "title": title,
            "detail": detail if isinstance(detail, list) else [detail],
            "action": action, "subject": subject, "metric": metric}


# --------------------------------------------------------------------------- #
# Formatting helpers (server-side so evidence reads the same everywhere)
# --------------------------------------------------------------------------- #
def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def _money(v):
    return "—" if v is None else f"${v:,.2f}"


def _rel(v):
    return "—" if v is None else f"{v * 100:+.0f}%"


# --------------------------------------------------------------------------- #
# Domain rules
# --------------------------------------------------------------------------- #
def _cost_rules(cur, prev, cur_rows, team) -> list[dict]:
    out = []
    c = _pp(team["cost_usd"], prev.get("cost_usd"))
    if c and c["rel"] is not None and c["rel"] >= TH["cost_spike_rel"]:
        loc = _pp(team["net_loc"], prev.get("net_loc"))
        loc_rel = loc["rel"] if loc else None
        # A spike only matters if output didn't keep p pace.
        if loc_rel is None or loc_rel < c["rel"] * 0.5:
            out.append(_finding(
                "cost", "high",
                f"成本环比 {_rel(c['rel'])}，产出未同步跟上",
                [f"本期成本 {_money(team['cost_usd'])} vs 上期 {_money(prev.get('cost_usd'))}"
                 f"（{_rel(c['rel'])}）；同期净增行仅 {_rel(loc_rel)}",
                 f"单位成本 {team['cpe']:.2f} $/千行" if team.get("cpe") else ""],
                "定位涨幅最大的项目/成员（见项目排名），确认是任务变重还是效率下降；"
                "若是效率问题，检查是否切了更贵的模型或放大了上下文。",
                metric="cost_usd"))
    cpe = _pp(team.get("cpe"), prev.get("cpe"))
    if cpe and cpe["rel"] is not None and cpe["rel"] >= TH["cpe_worsen_rel"]:
        out.append(_finding(
            "cost", "medium",
            f"单位成本恶化 {_rel(cpe['rel'])}",
            [f"每千行成本 {team['cpe']:.2f} vs 上期 {prev.get('cpe'):.2f} $/千行"],
            "同样的代码量花了更多钱：核对模型选型与缓存命中，高价模型应只用在难任务上。",
            metric="cpe"))
    chr_ = team.get("chr")
    if chr_ is not None and chr_ < TH["chr_low"]:
        out.append(_finding(
            "cost", "medium",
            f"缓存命中率偏低（{_pct(chr_)}）",
            [f"缓存命中率 {_pct(chr_)}，低于 {_pct(TH['chr_low'])}——"
             f"大量 Token 走全价输入而非低价缓存"],
            "缓存读的计费远低于全新输入。命中率低通常是频繁新开会话或上下文抖动，"
            "引导团队在同一任务内延续会话、复用系统提示。",
            metric="chr"))
    return out


def _quality_rules(cur, prev, team) -> list[dict]:
    out = []
    churn = team.get("churn_ratio")
    if churn is not None and churn >= TH["churn_high"]:
        out.append(_finding(
            "quality", "high",
            f"团队返工率偏高（{_pct(churn)}）",
            [f"自返工率 {_pct(churn)}，高于阈值 {_pct(TH['churn_high'])}——"
             f"写出的代码有相当比例又被自己删改"],
            "返工高通常是「方案没想清就动手」。推行小批量：每轮先确认方案再写、写完即验证。",
            metric="churn_ratio"))
    else:
        cw = _pp(churn, prev.get("churn_ratio"))
        if cw and cw["rel"] is not None and cw["rel"] >= TH["churn_worsen_rel"]:
            out.append(_finding(
                "quality", "medium",
                f"返工率环比上升 {_rel(cw['rel'])}",
                [f"本期 {_pct(churn)} vs 上期 {_pct(prev.get('churn_ratio'))}"],
                "质量在退坡。对照采用趋势看是否因为放量太快、或引入了不熟悉的技术栈。",
                metric="churn_ratio"))
    err = team.get("tool_error_rate")
    if err is not None and err >= TH["tool_error_high"]:
        out.append(_finding(
            "quality", "medium",
            f"工具错误率偏高（{_pct(err)}）",
            [f"工具调用失败率 {_pct(err)}，高于 {_pct(TH['tool_error_high'])}"],
            "高失败率拖慢每一步并浪费 Token。排查是权限/环境配置问题，还是模型频繁调错工具。",
            metric="tool_error_rate"))
    rbw = team.get("read_before_write")
    if rbw is not None and rbw < TH["rbw_low"]:
        out.append(_finding(
            "quality", "medium",
            f"先读后写率偏低（{_pct(rbw)}）",
            [f"改动前先读文件的比例仅 {_pct(rbw)}，低于 {_pct(TH['rbw_low'])}——盲改风险高"],
            "盲写容易覆盖既有逻辑。要求编辑前先读目标文件，或在 harness 里强制先读。",
            metric="read_before_write"))
    return out


def _entity_rules(rows, team, key, label_field, domain, noun) -> list[dict]:
    """People / project standing vs. the team.

    Two tiers of output, so the page is useful even when nobody crossed a red
    line — which is exactly when a manager still wants "who's relatively behind":

    1. **越绝对线 → 短板卡** (high/medium): worse than team by a margin AND on a
       bad tier / above the churn / error thresholds. Actionable, named.
    2. **相对靠后 → 提示卡** (low): the lowest-scoring qualified entity, even if
       everyone is technically fine. Framed as "相对靠后",", not a failure.
    """
    matrix = [m for m in personas._matrix(rows, key, label_field)
              if (m["sessions"] or 0) >= TH["min_sessions"]]
    if not matrix:
        return []
    t_churn = team.get("churn_ratio")
    t_err = team.get("tool_error_rate")
    out = []
    flagged = set()

    for m in matrix:
        name = m[label_field]
        reasons = []
        if m.get("tier") in TH["bad_tiers"] and m.get("score") is not None:
            reasons.append(f"综合分 {m['score']:.1f}（{m['tier']}）")
        if (t_churn and m.get("churn_ratio") is not None
                and m["churn_ratio"] > t_churn * (1 + TH["entity_worse_rel"])):
            reasons.append(f"返工率 {_pct(m['churn_ratio'])} vs 团队 {_pct(t_churn)}")
        if (t_err and m.get("tool_error_rate") is not None
                and m["tool_error_rate"] > t_err * (1 + TH["entity_worse_rel"])):
            reasons.append(f"工具错误率 {_pct(m['tool_error_rate'])} vs 团队 {_pct(t_err)}")
        if not reasons:
            continue
        sev = "high" if (m.get("tier") in TH["bad_tiers"] and len(reasons) >= 2) else "medium"
        if domain == "people":
            action = (f"和 {name} 一起看一个高返工会话，定位是方案习惯还是工具用法问题；"
                      f"把团队里综合分高的成员的做法沉淀成上手清单。")
        else:
            action = (f"复盘「{name}」的技术栈与任务类型：是项目本身难，还是缺少"
                      f"约定（测试、代码规范、上下文文档）导致反复返工。")
        out.append(_finding(
            domain, sev,
            f"{noun}「{name}」是当前短板",
            [" · ".join(reasons) + f"（{m['sessions']} 会话）"],
            action, subject=name, metric="score"))
        flagged.add(name)

    # No hard flag → surface the relatively weakest as a low-severity heads-up,
    # so a manager always knows where the bottom of the pack is.
    if not out:
        scored = [m for m in matrix if m.get("score") is not None]
        if len(scored) >= 2:
            worst = min(scored, key=lambda m: m["score"])
            best = max(scored, key=lambda m: m["score"])
            gap = best["score"] - worst["score"]
            name = worst[label_field]
            out.append(_finding(
                domain, "low",
                f"{noun}「{name}」相对靠后（非告警）",
                [f"综合分 {worst['score']:.1f}，队内最低；最高 {best[label_field]} "
                 f"{best['score']:.1f}，差 {gap:.1f} 分",
                 f"返工率 {_pct(worst.get('churn_ratio'))} · 错误率 "
                 f"{_pct(worst.get('tool_error_rate'))}（{worst['sessions']} 会话）"],
                (f"整体都在健康区间，无需干预；若想拉齐，可让 {best[label_field]} "
                 f"与 {name} 结对一次。" if gap < 5 else
                 f"差距已有 {gap:.1f} 分，值得关注 {name} 的返工与工具用法。"),
                subject=name, metric="score"))
    return out[:4]  # cap so the page stays a triage list, not a dump


def _adoption_rules(cur_rows, prev_rows, cur, prev, team) -> list[dict]:
    out = []
    s = _pp(team["sessions"], prev.get("sessions"))
    if s and s["rel"] is not None and s["rel"] <= -TH["adoption_drop_rel"]:
        out.append(_finding(
            "adoption", "medium",
            f"会话量环比下滑 {_rel(s['rel'])}",
            [f"本期 {team['sessions']} 个会话 vs 上期 {prev.get('sessions')}"],
            "采用在退坡。确认是项目节奏正常波动，还是工具体验/信任出了问题，找几个人聊聊。",
            metric="sessions"))
    # Concentration risk: one person doing most of the sessions.
    by_person: dict[str, int] = {}
    for r in cur_rows:
        by_person[r["c_person"]] = by_person.get(r["c_person"], 0) + 1
    total = sum(by_person.values())
    if total and len(by_person) > 1:
        top_name = max(by_person, key=by_person.get)
        share = by_person[top_name] / total
        if share >= TH["concentration"]:
            out.append(_finding(
                "adoption", "low",
                f"使用高度集中在 {top_name}（{_pct(share)} 会话）",
                [f"{top_name} 贡献了 {by_person[top_name]}/{total} 个会话"],
                "单点依赖：经验没有扩散到团队。让 TA 带一次分享，或结对把用法传出传出去。",
                subject=top_name, metric="sessions"))
    return out


def _healthy_card(domain, team) -> dict:
    """Green baseline verdict for a domain that fired no issue."""
    msg = {
        "cost": (f"成本处于正常区间——单位成本 {team.get('cpe'):.2f} $/千行、"
                 f"缓存命中率 {_pct(team.get('chr'))}，无环比异常。"
                 if team.get("cpe") else "成本无异常。"),
        "quality": (f"交付质量健康——返工率 {_pct(team.get('churn_ratio'))}、"
                    f"工具错误率 {_pct(team.get('tool_error_rate'))}、"
                    f"先读后写率 {_pct(team.get('read_before_write'))} 均在阈值内。"),
        "people": "无人员达到点名阈值，且样本量足够的成员表现接近。",
        "project": "各项目健康度接近，无明显掉队项目。",
        "adoption": f"采用平稳——本期 {team.get('sessions')} 个会话，无骤降或单点依赖。",
        "config": "暂无达到统计门槛的配置优化结论（样本或差异不足）。",
    }.get(domain, "无异常。")
    return _finding(domain, "ok", "健康", [msg], "", metric=None)


def _config_rules(rows) -> list[dict]:
    """Fold the statistical cohort comparison in as ONE secondary domain."""
    out = []
    rep = _insights.generate(rows)
    for f in rep.get("findings", [])[:4]:
        detail = list(f.get("evidence", []))
        if f.get("grade_label"):
            detail.append(f"证据等级：{f['grade_label']}")
        out.append(_finding(
            "config", f.get("severity", "low"),
            f["title"], detail, f.get("action", ""),
            subject=f.get("subject"), metric=None))
    return out


# --------------------------------------------------------------------------- #
# period-over-period helper
# --------------------------------------------------------------------------- #
def _pp(cur, prev):
    if cur is None or prev is None or prev == 0:
        return {"cur": cur, "prev": prev, "rel": None}
    return {"cur": cur, "prev": prev, "rel": (cur - prev) / abs(prev)}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
DOMAIN_LABELS = {
    "cost": "成本与 ROI", "quality": "交付质量", "people": "人员短板",
    "project": "项目短板", "adoption": "采用与风险", "config": "配置调优",
}


def diagnose(persons=None, projects=None, models=None,
             start_ts=None, end_ts=None) -> dict:
    """Run every domain rule over the selected window and return ranked cards."""
    rows_all = db.fetch_analysis_rows(persons, projects, models, None, None)
    start_ts, end_ts = personas._resolve_window(rows_all, start_ts, end_ts)
    span = max(end_ts - start_ts, personas._DAY)
    cur_rows = personas._slice(rows_all, start_ts, end_ts)
    prev_rows = personas._slice(rows_all, start_ts - span, start_ts - 1)

    team = db._agg_metrics(cur_rows)
    prev = db._agg_metrics(prev_rows)

    # Each domain runs its rules; if a domain fires nothing, it still gets a
    # green "健康" baseline card stating the number that cleared — so a manager
    # always sees a verdict per domain, never a blank page.
    domain_findings = {
        "cost": _cost_rules(cur_rows, prev, cur_rows, team),
        "quality": _quality_rules(cur_rows, prev, team),
        "people": _entity_rules(cur_rows, team, "c_person", "person", "people", "成员"),
        "project": _entity_rules(cur_rows, team, "c_project", "project", "project", "项目"),
        "adoption": _adoption_rules(cur_rows, prev_rows, team, prev, team),
        "config": _config_rules(cur_rows),
    }
    for dk, fs in domain_findings.items():
        if not fs:
            fs.append(_healthy_card(dk, team))

    findings: list[dict] = [f for fs in domain_findings.values() for f in fs]

    # Group by domain for the UI, severity-ordered within each.
    domains = []
    for k in sorted(domain_findings, key=lambda k: _DOMAIN_ORDER.get(k, 9)):
        fs = sorted(domain_findings[k], key=lambda f: _SEV_ORDER.get(f["severity"], 9))
        domains.append({"key": k, "label": DOMAIN_LABELS[k], "findings": fs})

    counts = {"high": sum(1 for f in findings if f["severity"] == "high"),
              "medium": sum(1 for f in findings if f["severity"] == "medium"),
              "low": sum(1 for f in findings if f["severity"] == "low"),
              "ok": sum(1 for f in findings if f["severity"] == "ok")}

    return {
        "window": {"start": start_ts, "end": end_ts, "span_days": span // personas._DAY},
        "sessions_analyzed": team["sessions"],
        "counts": counts,
        "domains": domains,
        "healthy": counts["high"] + counts["medium"] + counts["low"] == 0,
        "caveat": ("诊断基于本地上传的观测数据（非随机实验），是相关性线索而非因果定论；"
                   "阈值可在后端 diagnosis.TH 调整。个体短板仅在样本 ≥ "
                   f"{TH['min_sessions']} 个会话时点名。"),
    }