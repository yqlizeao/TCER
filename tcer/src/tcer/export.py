"""Report serialization: per-session row dict + JSON / CSV / Markdown export.

Split out of the former ``report.py`` (whose terminal table / aggregate block /
ANSI CTEI chart were CLI-only and are gone). The shared ranking helper
``ctei_ranking`` feeds both the Markdown ASCII chart and the GUI's Canvas bar
chart, so data prep stays separate from presentation.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import format as fmt
from . import metrics
from .models import SessionReport, TokenUsage


# --------------------------------------------------------------------------- #
# Shared chart data
# --------------------------------------------------------------------------- #
def _chart_label(r: SessionReport) -> str:
    base = (r.meta.session_id or r.meta.path.stem)[:12]
    return ("↳" + base[:11]) if r.meta.is_subagent else base


def ctei_ranking(reports: list[SessionReport]) -> list[tuple[str, float, str]]:
    """``(label, ctei, grade)`` per scored session, sorted by CTEI descending."""
    scored = [r for r in reports if r.ctei is not None]
    scored.sort(key=lambda r: r.ctei, reverse=True)
    return [(_chart_label(r), r.ctei, r.grade or "") for r in scored]


def text_ctei_chart(reports: list[SessionReport], width: int = 40) -> str:
    """Plain-ASCII CTEI bar chart (no ANSI) for embedding in Markdown exports."""
    ranking = ctei_ranking(reports)
    if not ranking:
        return (
            "CTEI chart: no per-session score available\n"
            "  (sessions produced no measurable net code, or LOC is disabled)"
        )
    top = max(c for _, c, _ in ranking)
    scale = top if top > 0 else 1.0
    label_w = max(len(label) for label, _, _ in ranking)
    out = [
        "CTEI per session  (优秀>2.0  良好1–2  中等0.5–1  低效0.1–0.5  极端低效<0.1)",
        "-" * (label_w + width + 20),
    ]
    for label, ctei, grade in ranking:
        n = max(1, round(ctei / scale * width))
        bar = "█" * n
        pad = " " * (width - n)
        out.append(f"{label.ljust(label_w)}  {bar}{pad}  {ctei:6.3f}  {grade}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Row serialization
# --------------------------------------------------------------------------- #
def report_row_dict(r: SessionReport) -> dict:
    u = r.usage
    return {
        "session_id": r.meta.session_id,
        "title": r.meta.title,
        "path": str(r.meta.path),
        "is_subagent": r.meta.is_subagent,
        "subagent_count": r.subagent_count,
        "cwd": r.meta.cwd,
        "assistant_turns": u.assistant_msgs,
        "input_tokens": u.input_tokens,
        "cache_write_tokens": u.cache_creation_input_tokens,
        "cache_read_tokens": u.cache_read_input_tokens,
        "output_tokens": u.output_tokens,
        "total_tokens": u.total,
        "chr": r.chr,
        "io_ratio": r.io_ratio,
        "cost_usd": r.cost,
        "cost_per_mt": r.cost_per_mt,
        "tcer": r.tcer,
        "cpe": r.cpe,
        "net_loc": r.net_loc,
        "loc_accumulated": r.loc_accumulated,
        "ncpi": r.ncpi,
        "caf": r.caf,
        "task_type": r.task_type,
        "ta_tcer": r.ta_tcer,
        "psac": r.psac,
        "tcer_phase_adj": r.tcer_phase_adj,
        "ctei": r.ctei,
        "grade": r.grade,
        "code_added": r.code_added,
        "code_deleted": r.code_deleted,
        "churn_ratio": r.churn_ratio,
        "unseen_writes": r.unseen_writes,
        # --- timing ---
        "avg_turn_latency_sec": r.avg_turn_latency_sec,
        "session_duration_minutes": r.session_duration_minutes,
        # --- tool usage ---
        "read_write_ratio": r.read_write_ratio,
        "edit_ratio": r.edit_ratio,
        "exploration_ratio": r.exploration_ratio,
        "subagent_density": r.subagent_density,
        # --- context efficiency ---
        "cache_efficiency": r.cache_efficiency,
        "cache_write_ratio": r.cache_write_ratio,
        "non_cached_input_ratio": r.non_cached_input_ratio,
        # --- file-level quality ---
        "high_churn_file_count": r.high_churn_file_count,
        "test_net_loc": r.test_net_loc,
        "doc_net_loc": r.doc_net_loc,
        "test_loc_ratio": r.test_loc_ratio,
        "doc_loc_ratio": r.doc_loc_ratio,
        "models": sorted(u.models),
        "models_label": fmt.models_label(u),
        "cost_by_model": {m: round(c, 6) for m, c in sorted(metrics.cost_by_model(u).items())},
    }


def to_json(reports: list[SessionReport], agg: SessionReport, n_sessions: int) -> str:
    payload = {
        "aggregate": report_row_dict(agg) | {"sessions_counted": n_sessions},
        "sessions": [report_row_dict(r) for r in reports],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


_CSV_FIELDS = [
    "session_id", "is_subagent", "subagent_count", "assistant_turns", "input_tokens",
    "cache_write_tokens", "cache_read_tokens", "output_tokens",
    "total_tokens", "chr", "io_ratio", "cost_usd", "cost_per_mt",
    "tcer", "cpe", "net_loc", "loc_accumulated", "ncpi", "caf",
    "task_type", "ta_tcer", "psac", "tcer_phase_adj", "ctei", "grade",
    "code_added", "code_deleted", "churn_ratio", "unseen_writes",
    "avg_turn_latency_sec", "session_duration_minutes",
    "read_write_ratio", "edit_ratio", "exploration_ratio", "subagent_density",
    "cache_efficiency", "cache_write_ratio", "non_cached_input_ratio",
    "high_churn_file_count", "test_net_loc", "doc_net_loc", "test_loc_ratio", "doc_loc_ratio",
    "models", "models_label",
]


def to_csv(reports: list[SessionReport]) -> str:
    rows = [report_row_dict(r) for r in reports]
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        row["models"] = ";".join(row["models"])
        writer.writerow(row)
    return buf.getvalue()


def to_markdown(reports: list[SessionReport], agg: SessionReport, n_sessions: int,
                code_dir: Path | None, project_name: str = "Project") -> str:
    """Lightweight Markdown report (summary + per-session table + ASCII CTEI chart).

    Designed for embedding in PRs, docs, or wiki pages.
    """
    u = agg.usage
    lines = [
        f"# TCER Report: {project_name}",
        "",
        f"**{n_sessions} sessions** · "
        f"**{u.total:,} tokens** ({u.total_input:,} in / {u.output_tokens:,} out) · "
        f"**{fmt.fmt_money(agg.cost)}** @ list price",
        "",
        "## Summary",
        "",
        "| Metric | Value | Note |",
        "|--------|-------|------|",
        f"| **Net LOC** | {fmt.fmt_int(agg.net_loc)} | Tool-call derived (git-free) |",
        f"| **TCER** | {fmt.fmt_float(agg.tcer, '0.00')} LOC/Mt | Token → Code efficiency |",
        f"| **CPE** | {fmt.fmt_money(agg.cpe)}/kLOC | Cost per 1000 lines |",
        f"| **CHR** | {fmt.fmt_pct(agg.chr)} | Cache hit ratio (lower cost) |",
        f"| **Churn** | {fmt.fmt_pct(agg.churn_ratio)} | Rework fraction (deleted/added) |",
        f"| **CTEI** | {fmt.fmt_float(agg.ctei, '0.000')} | Composite efficiency index |",
        f"| **Grade** | {agg.grade or '-'} | CTEI rating |",
        "",
    ]
    if agg.unseen_writes:
        lines += [
            f"⚠️ **{agg.unseen_writes} unseen Writes** (F1 exposure)",
            "",
            "**LOC 统计假设**：Write 工具调用假设写入的是新文件（原大小 = 0）。",
            "若 Write 覆盖已有文件，added 会高估、deleted 会遗漏。Edit 不受影响。",
            "上述计数是潜在高估的上界。若需精确量化偏差，使用 GUI 的「校准 LOC」对标 git 历史。",
            "",
        ]

    lines += [
        "## Sessions",
        "",
        "| Session | Tokens | CHR | Net LOC | TCER | CTEI | Grade |",
        "|---------|--------|-----|---------|------|------|-------|",
    ]
    for r in reports:
        sid = (r.meta.session_id or r.meta.path.stem)[:12]
        lines.append(
            f"| `{sid}` | {fmt.fmt_int(r.usage.total)} | {fmt.fmt_pct(r.chr)} | "
            f"{fmt.fmt_int(r.net_loc)} | {fmt.fmt_float(r.tcer, '0.0')} | "
            f"{fmt.fmt_float(r.ctei, '0.00')} | {r.grade or '-'} |"
        )

    chart_ascii = text_ctei_chart(reports)
    if chart_ascii:
        lines += ["", "## CTEI Distribution", "", "```", chart_ascii.strip(), "```"]

    lines += [
        "",
        "---",
        f"*Generated by TCER v{_version()} · "
        f"Models: {fmt.models_label(u)} · Window: {fmt.fmt_dt(u.started_at)} → {fmt.fmt_dt(u.ended_at)}*",
    ]
    return "\n".join(lines)


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except (ImportError, AttributeError):
        return "unknown"
