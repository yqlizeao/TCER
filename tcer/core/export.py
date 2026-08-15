"""Report serialization: per-session row dict + JSON / CSV / Markdown export.

Split out of the former ``report.py`` (whose terminal table / aggregate block /
ANSI chart were CLI-only and are gone). The shared ranking helper
``score_ranking`` feeds both the Markdown ASCII chart and the GUI's Canvas bar
chart, so data prep stays separate from presentation.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from tcer.core import format as fmt
from tcer.core import metrics
from tcer.core.models import SessionReport, TokenUsage


# --------------------------------------------------------------------------- #
# Shared chart data
# --------------------------------------------------------------------------- #
def _chart_label(r: SessionReport) -> str:
    base = (r.meta.session_id or r.meta.path.stem)[:12]
    return ("↳" + base[:11]) if r.meta.is_subagent else base


# --------------------------------------------------------------------------- #
# 综合效率分 v2 ranking (bounded 0–100 → no P90 truncation needed)
# --------------------------------------------------------------------------- #
_SCORE_AXIS_KEYS = ("output", "cost", "quality")


def score_ranking(reports: list[SessionReport]) -> list[tuple[str, float, str]]:
    """``(label, score, tier)`` per scored session, sorted by 综合效率分 descending."""
    scored = [r for r in reports if r.score is not None]
    scored.sort(key=lambda r: r.score, reverse=True)
    return [(_chart_label(r), r.score, r.tier or "") for r in scored]


def score_decompose(report: SessionReport) -> dict[str, float] | None:
    """三条正交轴的收缩后分值（0–1）：产出/成本/质量。None 当会话未评分。

    质量轴可能为 None（无质量信号）、成本轴可能为 None（无成本数据，如
    net_loc=0 的会话）——缺失轴一律以 0.5（中性）填充展示，避免分解面板出现
    空洞、也避免把「无数据」画成最差档拉偏项目均值对比；效率分本身在
    efficiency_score 里已按可用轴重分权，不受影响。
    """
    if report.score is None:
        return None
    return {
        "output": report.score_output_axis if report.score_output_axis is not None else 0.5,
        "cost": report.score_cost_axis if report.score_cost_axis is not None else 0.5,
        "quality": report.score_quality_axis if report.score_quality_axis is not None else 0.5,
    }


def score_decompose_avg(reports: list[SessionReport]) -> dict[str, float] | None:
    """Average of each axis across scored sessions (for the 与项目均值对比 panel)."""
    all_axes: list[dict[str, float]] = []
    for r in reports:
        f = score_decompose(r)
        if f is not None:
            all_axes.append(f)
    if not all_axes:
        return None
    n = len(all_axes)
    return {k: sum(d[k] for d in all_axes) / n for k in _SCORE_AXIS_KEYS}


def _score_tier_legend() -> str:
    """Tier-band legend from ``SCORE_TIER_BANDS`` (SSOT), best→worst."""
    bands = metrics.SCORE_TIER_BANDS
    parts = []
    for i, (label, lo) in enumerate(bands):
        if i == 0:
            parts.append(f"{label}>{lo:g}")
        elif i == len(bands) - 1:
            parts.append(f"{label}<{bands[i - 1][1]:g}")
        else:
            parts.append(f"{label}{lo:g}–{bands[i - 1][1]:g}")
    return "  ".join(parts)


def text_score_chart(reports: list[SessionReport], width: int = 40) -> str:
    """Plain-ASCII 综合效率分 bar chart (0–100, no truncation) for Markdown."""
    ranking = score_ranking(reports)
    if not ranking:
        return (
            "综合效率分 chart: no per-session score available\n"
            "  (sessions produced no measurable net code, or LOC is disabled)"
        )
    label_w = max(len(label) for label, _, _ in ranking)
    out = [f"综合效率分 per session  ({_score_tier_legend()})",
           "-" * (label_w + width + 20)]
    for label, score, tier in ranking:
        n = max(1, min(width, round(score / 100.0 * width)))
        bar = "█" * n
        pad = " " * (width - n)
        out.append(f"{label.ljust(label_w)}  {bar}{pad}  {score:6.2f}  {tier}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Row serialization
# --------------------------------------------------------------------------- #
def report_row_dict(r: SessionReport) -> dict:
    u = r.usage
    return {
        "session_id": r.meta.session_id,
        "source": r.meta.source,
        "title": r.meta.title,
        "path": str(r.meta.path),
        "is_subagent": r.meta.is_subagent,
        "subagent_count": r.subagent_count,
        "cwd": r.meta.cwd,
        "assistant_turns": u.assistant_msgs,
        "input_tokens": u.input_tokens,
        "cache_write_tokens": u.cache_creation_input_tokens,
        "cache_write_1h_tokens": u.cache_write_1h_tokens,
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
        "caf": r.caf,
        "task_type": r.task_type,
        "ta_tcer": r.ta_tcer,
        "score": r.score,
        "tier": r.tier,
        "score_output_axis": r.score_output_axis,
        "score_cost_axis": r.score_cost_axis,
        "score_quality_axis": r.score_quality_axis,
        "code_added": r.code_added,
        "code_deleted": r.code_deleted,
        "churn_ratio": r.churn_ratio,
        "unseen_writes": r.unseen_writes,
        # --- timing (epoch ms; server folds ms→s for the time axis) ---
        "started_at": u.started_at,
        "ended_at": u.ended_at,
        "api_calls": r.usage.api_calls,
        "avg_request_latency_ms": r.avg_request_latency_ms,
        "session_duration_minutes": r.session_duration_minutes,
        # --- tool usage ---
        "read_write_ratio": r.read_write_ratio,
        "edit_ratio": r.edit_ratio,
        "exploration_ratio": r.exploration_ratio,
        # --- context efficiency ---
        "cache_efficiency": r.cache_efficiency,
        "cache_write_ratio": r.cache_write_ratio,
        "non_cached_input_ratio": r.non_cached_input_ratio,
        # --- file-level quality ---
        "high_churn_file_count": r.high_churn_file_count,
        "first_pass_file_ratio": r.first_pass_file_ratio,
        "test_net_loc": r.test_net_loc,
        "doc_net_loc": r.doc_net_loc,
        "test_loc_ratio": r.test_loc_ratio,
        "doc_loc_ratio": r.doc_loc_ratio,
        # --- new quality metrics ---
        "user_msgs": u.user_msgs,
        "entrypoint": r.meta.entrypoint,
        "cli_version": r.meta.cli_version,
        "model_provider": r.meta.model_provider,
        "thread_source": r.meta.thread_source,
        "git_branch": r.meta.git_branch,
        "git_commit": r.meta.git_commit,
        "git_repository": r.meta.git_repository,
        "approval_policy": r.meta.approval_policy,
        "sandbox_policy": r.meta.sandbox_policy,
        "permission_profile": r.meta.permission_profile,
        "collaboration_mode": r.meta.collaboration_mode,
        "reasoning_effort": r.meta.reasoning_effort,
        "tool_error_count": u.tool_errors,
        "tool_error_rate": r.tool_error_rate,
        "thinking_count": u.thinking_count,
        "reasoning_output_tokens": u.reasoning_output_tokens,
        "reasoning_output_ratio": r.reasoning_output_ratio,
        "model_context_window": u.model_context_window,
        "peak_input_tokens": u.peak_input_tokens,
        "context_window_used_ratio": r.context_window_used_ratio,
        "output_tps": r.output_tps,
        "time_to_first_token_sec": r.time_to_first_token_sec,
        "task_count": u.task_count,
        "completed_task_count": u.completed_task_count,
        "aborted_task_count": u.aborted_task_count,
        "task_completion_rate": r.task_completion_rate,
        "compaction_count": u.compaction_count,
        "web_search_count": u.web_search_count,
        "image_count": u.image_count,
        "local_image_count": u.local_image_count,
        "patch_apply_count": u.patch_apply_count,
        "patch_apply_success_count": u.patch_apply_success_count,
        "patch_apply_success_rate": r.patch_apply_success_rate,
        "rate_limit_snapshots": u.rate_limit_snapshots,
        "rate_limit_reached_count": u.rate_limit_reached_count,
        "rate_limit_names": sorted(u.rate_limit_names),
        "rate_limit_peak_used": u.rate_limit_peak_used,
        "ttft_p95_sec": r.ttft_p95_sec,
        "abort_reasons": dict(sorted(u.abort_reasons.items())),
        "cancellation_count": u.cancellation_count,
        "regeneration_count": u.regeneration_count,
        "positive_ratings": u.positive_ratings,
        "negative_ratings": u.negative_ratings,
        "git_commit_count": u.git_commit_count,
        "pr_created_count": u.pr_created_count,
        "pr_merged_count": u.pr_merged_count,
        "reverted_lines": u.reverted_lines,
        "permission_request_count": u.permission_request_count,
        "permission_wait_ms_total": u.permission_wait_ms_total,
        "itl_p50_ms": u.itl_p50_ms,
        "itl_p99_ms": u.itl_p99_ms,
        "user_modified_count": u.user_modified_count,
        "revert_events": u.revert_events,
        "mcp_calls_by_attr": dict(sorted(u.mcp_calls_by_attr.items())),
        "hook_run_count": u.hook_run_count,
        "hook_error_count": u.hook_error_count,
        "hook_duration_ms_total": u.hook_duration_ms_total,
        "queued_input_count": u.queued_input_count,
        "slash_command_count": u.slash_command_count,
        "correction_msg_count": u.correction_msg_count,
        "first_prompt_chars": u.first_prompt_chars,
        "plan_mode_count": u.plan_mode_count,
        "read_truncation_count": u.read_truncation_count,
        "reasoning_ms_total": u.reasoning_ms_total,
        "patch_diff_added": u.patch_diff_added,
        "patch_diff_deleted": u.patch_diff_deleted,
        "source_reported_cost_usd": u.reported_cost_usd,
        "files_touched": r.files_touched,
        "search_edit_ratio": r.search_edit_ratio,
        "read_before_write": r.read_before_write,
        "edit_verify_ratio": r.edit_verify_ratio,
        "first_edit_turn": r.first_edit_turn,
        "bash_ratio": r.bash_ratio,
        "retry_loop_count": r.retry_loop_count,
        "retry_loop_max_len": r.retry_loop_max_len,
        "turn_cost_max_share": r.turn_cost_max_share,
        "turn_cost_spike_turn": r.turn_cost_spike_turn,
        "cache_invalidation_events": r.cache_invalidation_events,
        "ai_active_ratio": r.ai_active_ratio,
        "user_gap_median_min": r.user_gap_median_min,
        # Raw tool-name → call count. Keys stay verbatim (``Skill``,
        # ``mcp__server__tool``, …) so downstream consumers can derive the
        # Skill / MCP / plugin dimensions; CSV keeps ignoring it (dict column).
        "tool_calls": dict(sorted(u.tool_calls.items())),
        # "<Tool>:<variant>" → count (``Skill:dataviz``, ``Agent:Explore``).
        # Claude sessions only for now — the other CLIs don't expose the skill /
        # subagent identity in a shape the readers already parse.
        "tool_variants": dict(sorted(u.tool_variants.items())),
        "models": sorted(u.models),
        "models_label": fmt.models_label(u),
        "cost_by_model": {m: round(c, 6) for m, c in sorted(metrics.cost_by_model(u).items())},
    }


def session_to_json(r: SessionReport) -> str:
    """单会话 JSON 导出：一份完整的 report_row_dict（不含冗余聚合包装）。"""
    return json.dumps(report_row_dict(r), indent=2, ensure_ascii=False, default=str)


def to_json(reports: list[SessionReport], agg: SessionReport, n_sessions: int) -> str:
    payload = {
        "aggregate": report_row_dict(agg) | {"sessions_counted": n_sessions},
        "sessions": [report_row_dict(r) for r in reports],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


_CSV_FIELDS = [
    "session_id", "source", "started_at", "ended_at", "is_subagent", "subagent_count", "assistant_turns", "input_tokens",
    "cache_write_tokens", "cache_read_tokens", "output_tokens",
    "total_tokens", "chr", "io_ratio", "cost_usd", "cost_per_mt",
    "tcer", "cpe", "net_loc", "caf",
    "task_type", "ta_tcer",
    "score", "tier", "score_output_axis", "score_cost_axis", "score_quality_axis",
    "code_added", "code_deleted", "churn_ratio", "unseen_writes",
    "api_calls", "avg_request_latency_ms", "session_duration_minutes",
    "read_write_ratio", "edit_ratio", "exploration_ratio",
    "cache_efficiency", "cache_write_ratio", "non_cached_input_ratio",
    "high_churn_file_count", "test_net_loc", "doc_net_loc", "test_loc_ratio", "doc_loc_ratio",
    "user_msgs", "entrypoint", "tool_error_count", "tool_error_rate",
    "cli_version", "model_provider", "thread_source", "git_branch", "git_commit",
    "approval_policy", "sandbox_policy", "collaboration_mode", "reasoning_effort",
    "thinking_count", "reasoning_output_tokens", "reasoning_output_ratio",
    "model_context_window", "peak_input_tokens", "context_window_used_ratio", "output_tps", "time_to_first_token_sec",
    "task_count", "completed_task_count", "aborted_task_count", "task_completion_rate",
    "compaction_count", "web_search_count", "image_count", "local_image_count",
    "patch_apply_count", "patch_apply_success_count", "patch_apply_success_rate",
    "rate_limit_snapshots", "rate_limit_reached_count", "rate_limit_peak_used",
    "files_touched", "search_edit_ratio", "read_before_write",
    "cache_write_1h_tokens", "first_pass_file_ratio", "ttft_p95_sec",
    "cancellation_count", "regeneration_count",
    "positive_ratings", "negative_ratings",
    "git_commit_count", "pr_created_count", "pr_merged_count",
    "reverted_lines", "permission_request_count", "permission_wait_ms_total",
    "itl_p50_ms", "itl_p99_ms", "user_modified_count", "revert_events",
    "hook_run_count", "hook_error_count", "hook_duration_ms_total",
    "queued_input_count", "slash_command_count", "correction_msg_count",
    "first_prompt_chars", "plan_mode_count", "read_truncation_count",
    "reasoning_ms_total", "patch_diff_added", "patch_diff_deleted",
    "source_reported_cost_usd", "edit_verify_ratio", "first_edit_turn",
    "bash_ratio", "retry_loop_count", "retry_loop_max_len",
    "turn_cost_max_share", "turn_cost_spike_turn", "cache_invalidation_events",
    "ai_active_ratio", "user_gap_median_min",
    "models", "models_label",
]

# report_row_dict 中有意不进 CSV 的键：隐私/宽度（title/path/cwd）、以及 dict/list 结构化字段。
# started_at/ended_at 已列入 _CSV_FIELDS（epoch ms，与 JSON 导出对齐）。
# 新增导出字段必须进 _CSV_FIELDS 或此集合之一——test_export 有漂移护栏。
_CSV_EXCLUDED = frozenset({
    "title", "path", "cwd",
    "git_repository", "permission_profile",
    "rate_limit_names", "abort_reasons", "mcp_calls_by_attr", "cost_by_model",
    # Dict-valued: one CSV column per tool name would be unbounded and unstable
    # across sessions. Uploaded as JSON to the server layer instead.
    "tool_calls", "tool_variants",
})


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
                project_name: str = "Project") -> str:
    """Lightweight Markdown report (summary + per-session table + ASCII 综合效率分 chart).

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
        f"| **Churn** | {fmt.fmt_pct(agg.churn_ratio)} | Self-rework fraction (reworked/added) |",
        f"| **综合效率分** | {fmt.fmt_float(agg.score, '0.0')} | Composite efficiency score (0–100) |",
        f"| **Tier** | {agg.tier or '-'} | Efficiency-score rating |",
        "",
    ]
    if agg.unseen_writes:
        lines += [
            f"⚠️ **{agg.unseen_writes} unseen Writes** (F1 exposure)",
            "",
            "**LOC 统计假设**：Write 工具调用假设写入的是新文件（原大小 = 0）。",
            "若 Write 覆盖已有文件，added 会高估、deleted 会遗漏。Edit 不受影响。",
            "上述计数是残留高估的上界（会话数据带 originalFile 时已自动修正）。",
            "",
        ]

    lines += [
        "## Sessions",
        "",
        "| Session | Tokens | CHR | Net LOC | TCER | 效率分 | Tier |",
        "|---------|--------|-----|---------|------|------|-------|",
    ]
    for r in reports:
        sid = (r.meta.session_id or r.meta.path.stem)[:12]
        lines.append(
            f"| `{sid}` | {fmt.fmt_int(r.usage.total)} | {fmt.fmt_pct(r.chr)} | "
            f"{fmt.fmt_int(r.net_loc)} | {fmt.fmt_float(r.tcer, '0.0')} | "
            f"{fmt.fmt_float(r.score, '0.0')} | {r.tier or '-'} |"
        )

    chart_ascii = text_score_chart(reports)
    if chart_ascii:
        lines += ["", "## 综合效率分 Distribution", "", "```", chart_ascii.strip(), "```"]

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
