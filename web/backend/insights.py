"""Recommendation engine — turns cohort comparisons into actions.

``analysis`` says *what differs*; this module says *what to do about it*, and
refuses to say anything when the evidence doesn't support it.

Design rules, each learned from a way these dashboards go wrong:

- **Every recommendation names one changeable thing.** "Model A is better" is
  not actionable; "在『代码创作』任务上默认切到 A" is.
- **Nothing ships below ``moderate`` evidence.** Cohorts that are too small, or
  whose interval spans zero, produce a *coverage note* ("样本不足") instead of a
  fake finding. On a fresh instance almost everything lands here — that is the
  honest state, not a bug.
- **A win with a guardrail regression is downgraded, never hidden.** If the
  faster cohort also reworks more code, the recommendation says so in the same
  breath (DX 2026: high adoption regularly coexists with rising change-failure).
- **Cost and output are separate findings.** A cohort can be worth its price or
  not; collapsing that into one score is what makes composite indices useless.

All output is Chinese, matching the rest of the UI.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Same flat-import convention as server.py / db.py: runnable as a script, as a
# module, and importable from tests without a package install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as an  # noqa: E402

# Dimensions worth auto-scanning, in the order the answers matter to a team.
SCAN_DIMENSIONS = ("model", "source", "reasoning_effort", "permission_profile",
                   "skill", "mcp", "subagent")

# A relative effect below this is statistically real but practically noise.
MIN_RELEVANT_REL = 0.10
# Cost premium that demands a matching output gain to be justified.
COST_PREMIUM_REL = 0.20

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.0f}%"


def _fmt_abs(v: float | None, fmt: str) -> str:
    if v is None:
        return "—"
    if fmt == "pct":
        return f"{v * 100:.1f}%"
    if fmt == "money":
        return f"${v:,.2f}"
    if fmt == "int":
        return f"{v:,.0f}"
    if fmt == "float1":
        return f"{v:,.1f}"
    return f"{v:,.2f}"


def _evidence(c: dict, metric_label: str, fmt: str) -> str:
    """One line of evidence.

    The **stratified** effect is the headline number; the raw medians are shown
    separately and labelled 未分层. Mixing them — quoting raw medians next to a
    stratified percentage — reads as an arithmetic error and is the fastest way
    to lose the reader's trust, because the two can point opposite ways
    (that divergence is the confounding, and it is worth showing on purpose).
    """
    ci = ""
    if c["ci_low"] is not None and c["ci_high"] is not None:
        ci = f"，95%区间 [{_fmt_abs(c['ci_low'], fmt)}, {_fmt_abs(c['ci_high'], fmt)}]"
    if c["stratified"]:
        head = (f"{metric_label} 分层后效应 {_fmt_abs(c['diff'], fmt)}"
                f"（相对基准 {_fmt_pct(c['rel_diff'])}{ci}）"
                f"；已按任务类型×会话规模分 {c['strata_used']} 层")
    else:
        head = (f"{metric_label} 整体差异 {_fmt_abs(c['diff'], fmt)}"
                f"（相对 {_fmt_pct(c['rel_diff'])}{ci}）；样本不足以分层")
    raw = (f"未分层中位数 {_fmt_abs(c['stats']['median'], fmt)} vs 其余 "
           f"{_fmt_abs(c['contrast_stats']['median'], fmt)}"
           f"（{_fmt_pct(c['naive_rel_diff'])}）")
    return f"{head}；{raw}；n={c['stats']['n']} vs {c['contrast_stats']['n']}"


def _guardrail_warnings(c: dict) -> list[str]:
    """Guardrail metrics that got significantly *worse* in this cohort."""
    out = []
    for g in c["guardrails"].values():
        if g["grade"] in ("strong", "moderate") and (g["diff_oriented"] or 0) < 0:
            out.append(f"{g['label']} {_fmt_abs(g['median'], g['fmt'])}"
                       f"（其余 {_fmt_abs(g['contrast_median'], g['fmt'])}）明显更差")
    return out


def _finding(*, severity: str, kind: str, dimension: str, dim_label: str,
             subject: str, title: str, action: str, evidence: list[str],
             grade: str, caveats: list[str] | None = None) -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "dimension": dimension,
        "dimension_label": dim_label,
        "subject": subject,
        "title": title,
        "action": action,
        "evidence": evidence,
        "grade": grade,
        "grade_label": an.GRADE_LABEL.get(grade, grade),
        "caveats": caveats or [],
    }


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def _rule_best_choice(rows: list[dict], dimension: str) -> list[dict]:
    """Single-valued knobs: is one setting measurably more productive?"""
    rep = an.compare(rows, dimension, an.PRIMARY_METRIC)
    dim = an.DIMENSIONS[dimension]
    if dim.multi or len(rep["cohorts"]) < 2:
        return []
    out = []
    for c in rep["cohorts"]:
        if c["grade"] not in ("strong", "moderate"):
            continue
        rel = c["rel_diff"] or 0.0
        if abs(rel) < MIN_RELEVANT_REL:
            continue
        better = (c["diff_oriented"] or 0) > 0
        warns = _guardrail_warnings(c)
        if better:
            title = f"{dim.label}「{c['label']}」产出效率更高"
            action = (f"在当前筛选范围内，优先把 {dim.label} 设为「{c['label']}」；"
                      f"若要推广到全团队，建议先在一个项目上按同样口径复核一轮。")
            sev = "medium" if warns else "high"
        else:
            title = f"{dim.label}「{c['label']}」产出效率明显偏低"
            action = f"复核为什么会用到「{c['label']}」，考虑收敛到表现更好的取值。"
            sev = "medium"
        out.append(_finding(
            severity=sev, kind="choice", dimension=dimension, dim_label=dim.label,
            subject=c["label"], title=title, action=action, grade=c["grade"],
            evidence=[_evidence(c, rep["metric_label"], rep["fmt"])],
            caveats=([f"注意：{w}——效率提升可能是以质量为代价换来的。" for w in warns]
                     if better else []),
        ))
    return out


def _rule_cost_not_worth_it(rows: list[dict], dimension: str) -> list[dict]:
    """A setting that costs significantly more without a matching output gain.

    Measured on **CPE** (cost per 1000 net lines), not raw per-session cost.
    Per-session cost is dominated by how big the session was — a 2000-line
    session costs more than a 20-line one no matter what model ran it, which
    buries a 40% price premium under session-size noise. CPE normalizes that
    away, so "更贵" means "花更多钱拿到同样的产出".
    """
    out_rep = an.compare(rows, dimension, an.PRIMARY_METRIC)
    cost_rep = an.compare(rows, dimension, "cpe")
    dim = an.DIMENSIONS[dimension]
    out_by = {c["label"]: c for c in out_rep["cohorts"]}
    findings = []
    for c in cost_rep["cohorts"]:
        if c["grade"] not in ("strong", "moderate"):
            continue
        # diff_oriented < 0 means "worse for a lower-is-better metric" = pricier.
        rel = c["rel_diff"] or 0.0
        if (c["diff_oriented"] or 0) >= 0 or rel < COST_PREMIUM_REL:
            continue
        peer = out_by.get(c["label"])
        if peer is None:
            continue
        gained = peer["grade"] in ("strong", "moderate") and (peer["diff_oriented"] or 0) > 0
        if gained:
            continue  # pays more and delivers more — that's a trade, not a leak
        findings.append(_finding(
            severity="high", kind="cost", dimension=dimension, dim_label=dim.label,
            subject=c["label"],
            title=f"{dim.label}「{c['label']}」更贵，但没换来可测的产出提升",
            action=(f"把 {dim.label}=「{c['label']}」降档或限定到确实需要它的任务上；"
                    f"先在低风险任务上试一周，对比同口径的 TCER 与返工率。"),
            grade=c["grade"],
            evidence=[
                _evidence(c, cost_rep["metric_label"], cost_rep["fmt"]),
                f"同期产出侧：{an.GRADE_LABEL.get(peer['grade'], peer['grade'])}"
                f"（{_evidence(peer, out_rep['metric_label'], out_rep['fmt'])}）",
            ],
        ))
    return findings


def _rule_addon_value(rows: list[dict], dimension: str) -> list[dict]:
    """Multi-valued add-ons (Skill / MCP / subagent): used vs. not-used."""
    dim = an.DIMENSIONS[dimension]
    if not dim.multi:
        return []
    rep = an.compare(rows, dimension, an.PRIMARY_METRIC)
    tok_rep = an.compare(rows, dimension, "total_tokens")
    tok_by = {c["label"]: c for c in tok_rep["cohorts"]}
    noun = {"skill": "Skill", "mcp": "MCP 插件", "subagent": "子代理"}.get(dimension, dim.label)
    out = []
    for c in rep["cohorts"]:
        if c["grade"] not in ("strong", "moderate"):
            continue
        rel = c["rel_diff"] or 0.0
        if abs(rel) < MIN_RELEVANT_REL:
            continue
        if (c["diff_oriented"] or 0) > 0:
            out.append(_finding(
                severity="high", kind="addon_keep", dimension=dimension,
                dim_label=dim.label, subject=c["label"],
                title=f"{noun}「{c['label']}」参与的会话产出效率更高",
                action=f"把「{c['label']}」纳入默认配置并写进团队上手文档。",
                grade=c["grade"],
                evidence=[_evidence(c, rep["metric_label"], rep["fmt"])],
                caveats=["用了它的会话本身可能就是更适合它的任务，差异含自选择成分。"],
            ))
        else:
            ev = [_evidence(c, rep["metric_label"], rep["fmt"])]
            tc = tok_by.get(c["label"])
            if tc and tc["grade"] in ("strong", "moderate") and (tc["diff_oriented"] or 0) < 0:
                ev.append("并且这些会话的 token 消耗显著更高："
                          + _evidence(tc, tok_rep["metric_label"], tok_rep["fmt"]))
            out.append(_finding(
                severity="medium", kind="addon_drop", dimension=dimension,
                dim_label=dim.label, subject=c["label"],
                title=f"{noun}「{c['label']}」未见收益",
                action=(f"复核「{c['label']}」的触发条件：是被无差别加载、还是描述过宽"
                        f"导致误触发。确认无收益后从默认配置里摘掉，再看一周对比。"),
                grade=c["grade"], evidence=ev,
            ))
    return out


def _rule_batch_size(rows: list[dict]) -> list[dict]:
    """DORA's 'work in small batches', measured on session size.

    Large sessions are the AI-era equivalent of a big-bang change: more context
    to lose, more written before anything is verified. If the large band reworks
    significantly more, that's a workflow fix, not a tooling one.
    """
    bands = an.size_bands(rows)
    m = an.METRICS["churn_ratio"]
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(bands.get(r.get("id"), "M"), []).append(r)
    large, small = grouped.get("L", []), grouped.get("S", [])
    if len(large) < an.MIN_COHORT_SESSIONS or len(small) < an.MIN_COHORT_SESSIONS:
        return []
    # Size *is* the exposure here, so stratify on task type only.
    def by_task(rs):
        out: dict[str, list[float]] = {}
        for r in rs:
            v = m.get(r)
            if v is not None:
                out.setdefault(r.get("task_type") or "未标注", []).append(v)
        return out

    cmp_ = an.compare_groups(by_task(large), by_task(small))
    lv = [v for v in (m.get(r) for r in large) if v is not None]
    sv = [v for v in (m.get(r) for r in small) if v is not None]
    g = an.grade(len(lv), len(sv), cmp_)
    if g not in ("strong", "moderate") or (cmp_["diff"] or 0) <= 0:
        return []
    return [_finding(
        severity="medium", kind="workflow", dimension="session_size",
        dim_label="会话规模", subject="大会话（Token 量前 1/3）",
        title="大会话的自返工率显著更高",
        action=("把大任务拆成多轮小会话：每轮先确认方案再动手，写完即验证。"
                "对应 DORA 2025「小批量工作」能力。"),
        grade=g,
        evidence=[f"自返工率中位数 {_fmt_abs(an.median(lv), 'pct')}"
                  f" vs 小会话 {_fmt_abs(an.median(sv), 'pct')}"
                  f"（差 {_fmt_abs(cmp_['diff'], 'pct')}，"
                  f"95%区间 [{_fmt_abs(cmp_['ci_low'], 'pct')}, "
                  f"{_fmt_abs(cmp_['ci_high'], 'pct')}]；"
                  f"n={len(lv)} vs {len(sv)}）"],
    )]


def _coverage_notes(rows: list[dict]) -> list[dict]:
    """Why a dimension produced nothing — so an empty result isn't a mystery."""
    notes = []
    for key in SCAN_DIMENSIONS:
        dim = an.DIMENSIONS[key]
        members = {}
        for r in rows:
            for label in dim.get(r):
                members[label] = members.get(label, 0) + 1
        if not members:
            notes.append({"dimension": key, "dimension_label": dim.label,
                          "reason": "上传数据里没有这个维度的取值",
                          "detail": dim.hint})
            continue
        ok = [k for k, v in members.items() if v >= an.MIN_COHORT_SESSIONS]
        if dim.multi:
            # Multi-valued add-ons compare used vs. not-used, so a single value
            # is perfectly comparable; only the sample size can block them.
            if not ok:
                notes.append({"dimension": key, "dimension_label": dim.label,
                              "reason": f"所有取值都不足 {an.MIN_COHORT_SESSIONS} 次会话",
                              "detail": "继续上传即可解锁对比"})
        elif len(members) < 2:
            notes.append({"dimension": key, "dimension_label": dim.label,
                          "reason": f"只有 1 个取值（{next(iter(members))}），无从对比",
                          "detail": ""})
        elif len(ok) < 2:
            notes.append({"dimension": key, "dimension_label": dim.label,
                          "reason": f"{len(members)} 个取值中只有 {len(ok)} 个达到"
                                    f" {an.MIN_COHORT_SESSIONS} 次会话的最小样本量",
                          "detail": "继续上传即可解锁对比"})
        elif not ok:
            notes.append({"dimension": key, "dimension_label": dim.label,
                          "reason": f"所有取值都不足 {an.MIN_COHORT_SESSIONS} 次会话",
                          "detail": "继续上传即可解锁对比"})
    return notes


def generate(rows: list[dict]) -> dict:
    """Run every rule and return ranked, de-duplicated findings.

    Returning an empty ``findings`` list with populated ``coverage`` is a normal,
    correct outcome for a small dataset.
    """
    findings: list[dict] = []
    if len(rows) >= an.MIN_COHORT_SESSIONS * 2:
        for key in SCAN_DIMENSIONS:
            dim = an.DIMENSIONS[key]
            try:
                if dim.multi:
                    findings += _rule_addon_value(rows, key)
                else:
                    findings += _rule_best_choice(rows, key)
                    findings += _rule_cost_not_worth_it(rows, key)
            except ValueError:
                continue
        findings += _rule_batch_size(rows)

    # One finding per (dimension, subject, kind); keep the most severe.
    seen: dict[tuple, dict] = {}
    for f in findings:
        k = (f["dimension"], f["subject"], f["kind"])
        cur = seen.get(k)
        if cur is None or SEVERITY_ORDER[f["severity"]] < SEVERITY_ORDER[cur["severity"]]:
            seen[k] = f
    ranked = sorted(seen.values(),
                    key=lambda f: (SEVERITY_ORDER[f["severity"]],
                                   0 if f["grade"] == "strong" else 1,
                                   f["dimension"]))
    return {
        "findings": ranked,
        "coverage": _coverage_notes(rows),
        "sessions_analyzed": len(rows),
        "min_sessions": an.MIN_COHORT_SESSIONS,
        "caveat": (
            "全部结论基于本地上传的观测数据，非随机对照实验，因此是相关性而非因果。"
            "分层仅控制了任务类型与会话规模；「难任务更常派给某个模型/工具」这类"
            "选择偏差无法排除。请把它当作值得验证的线索，而不是定论。"
        ),
    }
