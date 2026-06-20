"""Rendering and export of session reports (terminal table, CSV, JSON)."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from .models import SessionReport, TokenUsage
from . import metrics, pricing


def models_label(u: TokenUsage) -> str:
    """Friendly, comma-joined list of the models a session used (e.g. 'Claude Opus 4.8')."""
    return ", ".join(pricing.label(m) for m in sorted(u.models)) or "-"


# --------------------------------------------------------------------------- #
# Value formatters
# --------------------------------------------------------------------------- #
def fmt_int(x: int | None) -> str:
    return f"{x:,}" if x else ("0" if x == 0 else "-")


def fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "-"


def fmt_float(x: float | None, p: str = "0.00") -> str:
    if x is None:
        return "-"
    width, _, prec = p.partition(".")
    return f"{x:{int(width) if width else 0}.{len(prec) if prec else 0}f}"


def fmt_money(x: float | None) -> str:
    return f"${x:.4f}" if x is not None else "-"


def fmt_ms(ts: int | None) -> str:
    if ts is None:
        return "-"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Table rendering
# --------------------------------------------------------------------------- #
def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a simple aligned ASCII table (stdlib only)."""
    cols = list(zip(*([headers] + rows))) if rows else [(h,) for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    sep = "  ".join("-" * w for w in widths)
    out = []
    out.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    out.append(sep)
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(out)


def session_table(reports: list[SessionReport]) -> str:
    headers = [
        "session", "subs", "turns", "input", "cacheW", "cacheR", "output",
        "total", "CHR", "I/O", "cost", "model",
    ]
    rows: list[list[str]] = []
    for r in reports:
        u = r.usage
        sid = (r.meta.session_id or r.meta.path.stem)[:12]
        model = models_label(u)
        rows.append([
            sid,
            str(r.subagent_count) if r.subagent_count else "",
            fmt_int(u.assistant_msgs),
            fmt_int(u.input_tokens),
            fmt_int(u.cache_creation_input_tokens),
            fmt_int(u.cache_read_input_tokens),
            fmt_int(u.output_tokens),
            fmt_int(u.total),
            fmt_pct(r.chr),
            fmt_float(r.io_ratio, "0.0"),
            fmt_money(r.cost),
            model[:40],
        ])
    return _table(headers, rows)


def aggregate_block(agg: SessionReport, code_dir: Path | None, n_sessions: int) -> str:
    u = agg.usage
    loc_note = "  (LOC disabled)" if agg.net_loc is None else "  (from session tool-calls)"
    sub_note = f"  ({agg.subagent_count} subagents folded in)" if agg.subagent_count else ""
    lines = [
        "Aggregate",
        "---------",
        f"  sessions counted : {n_sessions}{sub_note}",
        f"  assistant turns  : {u.assistant_msgs}  (skipped {u.empty_usage_skipped} empty-usage)",
        f"  total tokens     : {u.total:,}  (input {u.total_input:,} / output {u.output_tokens:,})",
        f"  cache hit ratio  : {fmt_pct(agg.chr)}",
        f"  I/O ratio        : {fmt_float(agg.io_ratio, '0.0')}",
        f"  cost (list price): {fmt_money(agg.cost)}   ({fmt_money(agg.cost_per_mt)}/Mt)",
        f"  net LOC          : {fmt_int(agg.net_loc)}{loc_note}",
        f"  TCER             : {fmt_float(agg.tcer, '0.00')} LOC/Mt",
        f"  CPE              : {fmt_money(agg.cpe)} per 1k LOC",
        f"  codebase dir     : {code_dir or '-'}",
        f"  models           : {models_label(u)}",
        f"  window           : {fmt_ms(u.started_at)} → {fmt_ms(u.ended_at)}",
    ]
    lines += _composite_lines(agg)
    return "\n".join(lines)


def _composite_lines(agg: SessionReport) -> list[str]:
    """Composite layer (L5) + L3 churn: NCPI / CAF / TA-TCER / PSAC / CTEI. Empty if no data."""
    if agg.ctei is None and agg.ncpi is None and agg.caf is None and agg.churn_ratio is None:
        return []
    grade = f"  [{agg.grade}]" if agg.grade else ""
    lines = []
    if agg.churn_ratio is not None or agg.code_added is not None:
        churn = fmt_pct(agg.churn_ratio)
        f1_warn = (f"  ⚠ {agg.unseen_writes} unseen Writes (F1 exposure)"
                   if agg.unseen_writes else "")
        lines += [
            "",
            "Quality (L3)",
            "------------",
            f"  code added/del   : +{fmt_int(agg.code_added)} / -{fmt_int(agg.code_deleted)}",
            f"  churn ratio      : {churn}  (deleted / added — lower is less rework)",
        ]
        if f1_warn:
            lines.append(f1_warn)
    lines += [
        "",
        "Composite (L5)",
        "--------------",
        f"  task type        : {agg.task_type or '-'}  (TTAF {fmt_float(_ttaf_of(agg), '0.00')})",
        f"  codebase LOC     : {fmt_int(agg.loc_accumulated)}",
        f"  NCPI             : {fmt_float(agg.ncpi, '0.000')}",
        f"  CAF              : {fmt_float(agg.caf, '0.00')}",
        f"  TA-TCER          : {fmt_float(agg.ta_tcer, '0.00')} LOC/Mt",
        f"  PSAC             : {fmt_float(agg.psac, '0.000')}  →  phase-adj TCER {fmt_float(agg.tcer_phase_adj, '0.00')}",
        f"  CTEI             : {fmt_float(agg.ctei, '0.000')}{grade}",
    ]
    return lines


def _ttaf_of(agg: SessionReport) -> float | None:
    from .metrics import TTAF
    return TTAF.get(agg.task_type or "")


# --------------------------------------------------------------------------- #
# CTEI bar chart (metric framework §6.3 — bars colored by rating)
# --------------------------------------------------------------------------- #
# ANSI color per CTEI grade.
_GRADE_COLOR = {
    "优秀": "\033[32m",      # green
    "良好": "\033[36m",      # cyan
    "中等": "\033[33m",      # yellow
    "低效": "\033[31m",      # red
    "极端低效": "\033[35m",  # magenta
}
_RESET = "\033[0m"


def _chart_label(r: SessionReport) -> str:
    base = r.meta.session_id or r.meta.path.stem
    base = base[:12]
    return ("↳" + base[:11]) if r.meta.is_subagent else base


def ctei_chart(reports: list[SessionReport], color: bool = True, width: int = 40) -> str:
    """Horizontal CTEI bar chart, one row per session that has a CTEI score."""
    scored = [r for r in reports if r.ctei is not None]
    if not scored:
        return (
            "CTEI chart: no per-session score available\n"
            "  (sessions produced no measurable net code, or LOC is disabled)"
        )
    scored.sort(key=lambda r: (r.ctei is not None, r.ctei), reverse=True)
    top = max(r.ctei for r in scored)
    scale = top if top > 0 else 1.0
    label_w = max(len(_chart_label(r)) for r in scored)

    out = [
        "CTEI per session  (优秀>2.0  良好1–2  中等0.5–1  低效0.1–0.5  极端低效<0.1)",
        "-" * (label_w + width + 20),
    ]
    for r in scored:
        label = _chart_label(r).ljust(label_w)
        n = max(1, round(r.ctei / scale * width))
        bar = "█" * n
        pad = " " * (width - n)
        if color:
            c = _GRADE_COLOR.get(r.grade or "", "")
            if c:
                bar = f"{c}{bar}{_RESET}"
        out.append(f"{label}  {bar}{pad}  {r.ctei:6.3f}  {r.grade or ''}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def _report_row_dict(r: SessionReport) -> dict:
    u = r.usage
    return {
        "session_id": r.meta.session_id,
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
        "models": sorted(u.models),
        "models_label": models_label(u),
        "cost_by_model": {m: round(c, 6) for m, c in sorted(metrics.cost_by_model(u).items())},
    }


def to_json(reports: list[SessionReport], agg: SessionReport, n_sessions: int) -> str:
    import json
    payload = {
        "aggregate": _report_row_dict(agg) | {"sessions_counted": n_sessions},
        "sessions": [_report_row_dict(r) for r in reports],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def to_csv(reports: list[SessionReport]) -> str:
    rows = [_report_row_dict(r) for r in reports]
    if not rows:
        return ""
    buf = io.StringIO()
    fieldnames = [
        "session_id", "is_subagent", "subagent_count", "assistant_turns", "input_tokens",
        "cache_write_tokens", "cache_read_tokens", "output_tokens",
        "total_tokens", "chr", "io_ratio", "cost_usd", "cost_per_mt",
        "tcer", "cpe", "net_loc", "loc_accumulated", "ncpi", "caf",
        "task_type", "ta_tcer", "psac", "tcer_phase_adj", "ctei", "grade",
        "code_added", "code_deleted", "churn_ratio",
        "models", "models_label",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        row["models"] = ";".join(row["models"])
        writer.writerow(row)
    return buf.getvalue()
