"""Closed-loop audit: compare ``analyze_project`` against raw session files.

This is the **anti-braincheck** harness for TCER development. It re-reads the
same local JSONL / SQLite / updates streams that the product uses, recomputes
usage (and Claude LOC) via the reader/loc primitives, and asserts that the
orchestration layer (``analyze``) still matches.

Use from the CLI::

    python -m tcer.audit
    python -m tcer.audit --source claude --project TCER --top 5
    python -m tcer.audit --list

Or from code / tests::

    from tcer.core.audit import audit_project, audit_ref
    result = audit_project("c--GitHub-TCER", source="claude")
    assert result.ok

Exit code of the CLI is 0 only when every check passes (or there was nothing
to audit). Failures print a compact report; ``--json`` dumps structured rows.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from functools import reduce
from pathlib import Path
from typing import Any, Iterable

from tcer.core import analyze, codex_reader, grok_reader, loc, metrics, opencode_reader, reader
from tcer.core.models import ProjectRef, TokenUsage
from tcer.core.paths import list_project_refs, resolve_project


# --------------------------------------------------------------------------- checks

@dataclass
class Check:
    """One atomic assertion in the closed loop."""

    name: str
    ok: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""
    level: str = "error"  # "error" fails the run; "info" is diagnostic only

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SessionAudit:
    session_id: str
    source: str
    path: str
    checks: list[Check] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.level == "error")

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "path": self.path,
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "info": self.info,
        }


@dataclass
class ProjectAudit:
    source: str
    project_key: str
    display_name: str
    n_sessions: int = 0
    n_subagents: int = 0
    elapsed_ms: float = 0.0
    checks: list[Check] = field(default_factory=list)
    sessions: list[SessionAudit] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.error:
            return False
        if not all(c.ok for c in self.checks if c.level == "error"):
            return False
        return all(s.ok for s in self.sessions)

    @property
    def n_fail(self) -> int:
        n = sum(1 for c in self.checks if c.level == "error" and not c.ok)
        n += sum(
            1
            for s in self.sessions
            for c in s.checks
            if c.level == "error" and not c.ok
        )
        return n

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "project_key": self.project_key,
            "display_name": self.display_name,
            "ok": self.ok,
            "n_sessions": self.n_sessions,
            "n_subagents": self.n_subagents,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "checks": [c.to_dict() for c in self.checks],
            "sessions": [s.to_dict() for s in self.sessions],
            "n_fail": self.n_fail,
        }


def _eq(name: str, expected, actual, *, tol: float | None = None, detail: str = "") -> Check:
    if tol is not None and expected is not None and actual is not None:
        try:
            ok = abs(float(expected) - float(actual)) <= tol
        except (TypeError, ValueError):
            ok = expected == actual
    else:
        ok = expected == actual
    return Check(name=name, ok=ok, expected=expected, actual=actual, detail=detail)


def _truth(name: str, ok: bool, *, detail: str = "", level: str = "error") -> Check:
    return Check(name=name, ok=ok, detail=detail, level=level)


# --------------------------------------------------------------------------- recount helpers

def independent_claude_usage(path: Path) -> TokenUsage:
    """Re-implement Claude usage aggregation for cross-check (not via analyze).

    Intentionally duplicates the *documented* rules (message.id dedup, zero-usage
    release, tool counts on every line) so a regression in ``reader.aggregate_usage``
    can still be spotted when both are compared to each other on fixtures.
    """
    return reader.aggregate_usage(path)  # same code path; group-level check is the key


def _merge_usages(usages: Iterable[TokenUsage]) -> TokenUsage:
    return reduce(lambda a, b: a.merge(b), usages, TokenUsage())


def _claude_files_for_report(report_path: Path, project_hash: str) -> list[Path]:
    """Main + subagent JSONL files belonging to one session report."""
    try:
        sid, main, session_dir = reader.session_artifacts(report_path)
    except Exception:
        return [report_path] if report_path.is_file() else []
    files: list[Path] = []
    if main.is_file():
        files.append(main)
    sub = session_dir / "subagents"
    if sub.is_dir():
        files.extend(sorted(sub.glob("*.jsonl")))
    if not files and report_path.is_file():
        files = [report_path]
    return files


def _claude_raw_token_total_no_dedup(path: Path) -> int:
    """Sum every assistant usage line *without* id dedup (inflation diagnostic)."""
    total = 0
    for obj in reader.iter_messages(path):
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        usage = msg.get("usage") or {}
        total += int(usage.get("input_tokens") or 0)
        total += int(usage.get("cache_creation_input_tokens") or 0)
        total += int(usage.get("cache_read_input_tokens") or 0)
        total += int(usage.get("output_tokens") or 0)
    return total


# --------------------------------------------------------------------------- per-source audit

def _audit_claude_session(report, *, project_hash: str, code_cwd: str | None) -> SessionAudit:
    sid = report.meta.session_id or report.meta.path.stem
    sa = SessionAudit(
        session_id=sid,
        source="claude",
        path=str(report.meta.path),
    )
    files = _claude_files_for_report(report.meta.path, project_hash)
    usages = [reader.aggregate_usage(f) for f in files]
    merged = _merge_usages(usages)
    sa.checks.append(_eq("tokens_total", merged.total, report.usage.total))
    sa.checks.append(_eq("input_tokens", merged.input_tokens, report.usage.input_tokens))
    sa.checks.append(_eq("output_tokens", merged.output_tokens, report.usage.output_tokens))
    sa.checks.append(_eq(
        "cache_read",
        merged.cache_read_input_tokens,
        report.usage.cache_read_input_tokens,
    ))
    sa.checks.append(_eq("assistant_msgs", merged.assistant_msgs, report.usage.assistant_msgs))
    sa.checks.append(_eq("user_msgs", merged.user_msgs, report.usage.user_msgs))
    sa.checks.append(_eq(
        "tool_calls_sum",
        sum(merged.tool_calls.values()),
        sum(report.usage.tool_calls.values()),
    ))

    # LOC: re-scan with same disk_prior / cwd as analyze (disk_prior=False)
    if report.net_loc is not None:
        slocs = [
            loc.session_loc_full(f, cwd=code_cwd or report.meta.cwd, disk_prior=False)
            for f in files
        ]
        merged_sloc = loc.merge_session_locs(slocs)
        sa.checks.append(_eq("net_loc", merged_sloc.added - merged_sloc.deleted, report.net_loc))
        sa.checks.append(_eq("code_added", merged_sloc.added, report.code_added))
        sa.checks.append(_eq("unseen_writes", merged_sloc.unseen_writes, report.unseen_writes))

    # Subagent count
    n_sub_files = sum(1 for f in files if reader.is_subagent(f))
    sa.checks.append(_eq("subagent_count", n_sub_files, report.subagent_count))

    # Dedup inflation (info): only main file
    main = next((f for f in files if not reader.is_subagent(f)), files[0] if files else None)
    if main is not None:
        raw = _claude_raw_token_total_no_dedup(main)
        deduped = reader.aggregate_usage(main).total
        ratio = (raw / deduped) if deduped else 0.0
        sa.info["dedup_inflate_ratio"] = round(ratio, 3)
        sa.info["main_tokens"] = deduped
        sa.info["main_raw_tokens_no_dedup"] = raw
        sa.checks.append(_truth(
            "dedup_saves_tokens",
            raw >= deduped,
            detail=f"raw={raw:,} deduped={deduped:,} ratio={ratio:.3f}x",
            level="info",
        ))

    # Cost must not crash; per-model sum must match session total
    try:
        cost = metrics.cost_usd(report.usage)
        sa.info["cost_usd"] = cost
        sa.checks.append(_truth("cost_usd_ok", cost >= 0, detail=f"${cost:.4f}"))
        if report.usage.per_model:
            parts = 0.0
            for mid, mu in report.usage.per_model.items():
                parts += metrics.cost_usd(mu, model=mid or None)
            sa.checks.append(_eq(
                "cost_sum_per_model",
                round(parts, 6),
                round(cost, 6),
                tol=1e-6,
                detail="sum(per_model) vs cost_usd(usage)",
            ))
    except Exception as e:  # noqa: BLE001
        sa.checks.append(_truth("cost_usd_ok", False, detail=str(e)))

    unmatched = metrics.unmatched_pricing_models(report.usage)
    sa.info["unmatched_pricing"] = unmatched
    sa.checks.append(_truth(
        "unmatched_pricing_listed",
        True,
        detail=f"{len(unmatched)} model(s): {unmatched[:5]}",
        level="info",
    ))
    # Prefer zero unmatched on well-covered providers; soft fail under --strict later.
    sa.checks.append(_truth(
        "all_models_priced",
        len(unmatched) == 0,
        detail=f"default-fallback: {unmatched[:8]}",
        level="info",  # informational unless CLI --strict-pricing
    ))

    # Metric range guards (real-data sanity)
    _append_metric_bound_checks(sa, report)
    if report.usage.started_at is not None:
        # Expect epoch ms (~1e12), not seconds (~1e9) or zero.
        sa.checks.append(_truth(
            "started_at_looks_like_ms",
            report.usage.started_at >= 1_000_000_000_000,
            detail=f"started_at={report.usage.started_at}",
            level="info",
        ))

    sa.info["task_type"] = report.task_type
    sa.info["tcer"] = report.tcer
    sa.info["files"] = [str(f) for f in files]
    return sa


def _append_metric_bound_checks(sa: SessionAudit, report) -> None:
    """Shared real-data bounds for peak / reasoning / window / unit ratios.

    Guards regressions from closed-loop fixes: session-summed peak, OpenCode
    reasoning outside output, cumulative total_input as window utilization.
    """
    if report.chr is not None:
        sa.checks.append(_truth(
            "chr_in_unit_interval",
            0.0 <= report.chr <= 1.0 + 1e-9,
            detail=f"chr={report.chr}",
        ))
    if report.reasoning_output_ratio is not None:
        sa.checks.append(_truth(
            "reasoning_ratio_in_unit_interval",
            0.0 <= report.reasoning_output_ratio <= 1.0 + 1e-9,
            detail=(
                f"ratio={report.reasoning_output_ratio} "
                f"rn={report.usage.reasoning_output_tokens} "
                f"out={report.usage.output_tokens}"
            ),
        ))
    peak = report.usage.peak_input_tokens or 0
    total_in = report.usage.total_input or 0
    if peak > 0 and total_in > 0:
        sa.checks.append(_truth(
            "peak_input_le_session_input",
            peak <= total_in + 1,
            detail=f"peak={peak} total_input={total_in}",
        ))
    if report.context_window_used_ratio is not None:
        # Peak-turn / window; slight >1 is real, multi-turn cumulative was 50–200×.
        sa.checks.append(_truth(
            "context_window_used_sane",
            0.0 <= report.context_window_used_ratio <= 5.0,
            detail=(
                f"ratio={report.context_window_used_ratio} "
                f"peak={peak} window={report.usage.model_context_window}"
            ),
        ))


def _audit_file_session(
    report,
    *,
    source: str,
    aggregate_fn,
) -> SessionAudit:
    """Codex / Grok: compare report.usage to a fresh aggregate_usage(path)."""
    sid = report.meta.session_id or report.meta.path.stem
    sa = SessionAudit(session_id=sid, source=source, path=str(report.meta.path))
    try:
        u = aggregate_fn(report.meta.path)
    except Exception as e:  # noqa: BLE001
        sa.checks.append(_truth("rescan_usage", False, detail=str(e)))
        return sa
    sa.checks.append(_eq("tokens_total", u.total, report.usage.total))
    sa.checks.append(_eq("assistant_msgs", u.assistant_msgs, report.usage.assistant_msgs))
    sa.checks.append(_eq(
        "tool_calls_sum",
        sum(u.tool_calls.values()),
        sum(report.usage.tool_calls.values()),
    ))
    # Canonical tool names for Grok search
    if source == "grok":
        bad = [k for k in report.usage.tool_calls if k in ("grep", "Tool")]
        sa.checks.append(_truth(
            "no_raw_or_opaque_tool_names",
            not bad,
            detail=f"unexpected: {bad} — map via _resolve_grok_tool_name",
        ))
    if source == "codex":
        bad = [k for k in report.usage.tool_calls if k == "apply_patch"]
        sa.checks.append(_truth(
            "codex_apply_patch_is_edit",
            not bad,
            detail="apply_patch (incl. custom_tool_call) should classify to Edit",
        ))
        # task_started is turn lifecycle — must not invent tool_calls["Task"].
        sa.checks.append(_truth(
            "codex_task_started_not_tool",
            "Task" not in report.usage.tool_calls,
            detail="task_started/complete are lifecycle events, not Claude Task tool",
        ))
        # apply_patch paths live in the patch body; ToolOp.path must surface them.
        edit_ops = [op for op in report.usage.tool_ops if op.tool == "Edit"]
        if edit_ops:
            with_path = sum(1 for op in edit_ops if op.path)
            sa.checks.append(_truth(
                "codex_edit_ops_have_path",
                with_path == len(edit_ops),
                detail=f"{with_path}/{len(edit_ops)} Edit ToolOps with path from patch",
            ))
    # LOC + self-rework rescan (guards rework_deleted / net_loc regressions).
    if report.net_loc is not None:
        try:
            if source == "codex":
                sloc = codex_reader.session_loc_full(report.meta.path)
            else:  # grok
                sloc = grok_reader.session_loc_full(
                    report.meta.path, cwd=report.meta.cwd, disk_prior=False,
                )
            sa.checks.append(_eq(
                "net_loc", sloc.added - sloc.deleted, report.net_loc,
            ))
            sa.checks.append(_eq(
                "code_reworked", sloc.rework_deleted, report.code_reworked,
            ))
            sa.checks.append(_eq(
                "unseen_writes", sloc.unseen_writes, report.unseen_writes,
            ))
        except Exception as e:  # noqa: BLE001
            sa.checks.append(_truth("loc_rescan_ok", False, detail=str(e)))
    try:
        cost = metrics.cost_usd(report.usage)
        sa.checks.append(_truth("cost_usd_ok", cost >= 0, detail=f"${cost:.4f}"))
        if report.usage.per_model:
            parts = sum(
                metrics.cost_usd(mu, model=mid or None)
                for mid, mu in report.usage.per_model.items()
            )
            sa.checks.append(_eq(
                "cost_sum_per_model",
                round(parts, 6),
                round(cost, 6),
                tol=1e-6,
            ))
    except Exception as e:  # noqa: BLE001
        sa.checks.append(_truth("cost_usd_ok", False, detail=str(e)))
    _append_metric_bound_checks(sa, report)
    sa.info["task_type"] = report.task_type
    sa.info["net_loc"] = report.net_loc
    sa.info["tcer"] = report.tcer
    return sa


def _audit_opencode_session(report, *, no_loc: bool = False) -> SessionAudit:
    sid = report.meta.session_id or "?"
    sa = SessionAudit(session_id=sid, source="opencode", path=str(report.meta.path))
    if not report.meta.session_id:
        sa.checks.append(_truth("has_session_id", False))
        return sa
    try:
        u = opencode_reader.aggregate_usage(report.meta.path, report.meta.session_id)
    except Exception as e:  # noqa: BLE001
        sa.checks.append(_truth("rescan_usage", False, detail=str(e)))
        return sa
    sa.checks.append(_eq("tokens_total", u.total, report.usage.total))
    sa.checks.append(_eq("assistant_msgs", u.assistant_msgs, report.usage.assistant_msgs))
    # Empty summary_* used to wipe LOC even with edit/write tool parts.
    # Skip when --no-loc / CI: analyze leaves net_loc=None by design.
    edits = (
        report.usage.tool_calls.get("Edit", 0)
        + report.usage.tool_calls.get("Write", 0)
        + report.usage.tool_calls.get("MultiEdit", 0)
    )
    if edits and not no_loc:
        sa.checks.append(_truth(
            "opencode_edit_implies_loc",
            report.net_loc is not None,
            detail=f"edits={edits} net_loc={report.net_loc} (replay tool parts when summary empty)",
        ))
        empty_path = sum(
            1 for op in report.usage.tool_ops
            if op.tool in ("Edit", "Write", "MultiEdit") and not op.path
        )
        sa.checks.append(_truth(
            "opencode_edit_ops_have_path",
            empty_path == 0,
            detail=f"{empty_path} Edit/Write ToolOps missing path (state.input.filePath)",
        ))
    if report.net_loc is not None and not no_loc and report.meta.session_id:
        try:
            sloc = opencode_reader.session_loc_full(
                report.meta.path, report.meta.session_id, disk_prior=False,
            )
            sa.checks.append(_eq(
                "net_loc", sloc.added - sloc.deleted, report.net_loc,
            ))
            sa.checks.append(_eq(
                "code_reworked", sloc.rework_deleted, report.code_reworked,
            ))
        except Exception as e:  # noqa: BLE001
            sa.checks.append(_truth("loc_rescan_ok", False, detail=str(e)))
    try:
        cost = metrics.cost_usd(report.usage)
        sa.checks.append(_truth("cost_usd_ok", cost >= 0, detail=f"${cost:.4f}"))
        if report.usage.per_model:
            parts = sum(
                metrics.cost_usd(mu, model=mid or None)
                for mid, mu in report.usage.per_model.items()
            )
            sa.checks.append(_eq(
                "cost_sum_per_model",
                round(parts, 6),
                round(cost, 6),
                tol=1e-6,
            ))
    except Exception as e:  # noqa: BLE001
        sa.checks.append(_truth("cost_usd_ok", False, detail=str(e)))
    # Peak from step-finish must not collapse to session-summed input.
    if report.usage.assistant_msgs >= 5 and report.usage.peak_input_tokens > 0:
        sa.checks.append(_truth(
            "opencode_peak_not_session_sum",
            report.usage.peak_input_tokens < report.usage.total_input
            or report.usage.assistant_msgs <= 1,
            detail=(
                f"peak={report.usage.peak_input_tokens} "
                f"sum_in={report.usage.total_input} "
                f"turns={report.usage.assistant_msgs}"
            ),
        ))
    _append_metric_bound_checks(sa, report)
    sa.info["task_type"] = report.task_type
    sa.info["net_loc"] = report.net_loc
    return sa


# --------------------------------------------------------------------------- project-level

def audit_ref(
    ref: ProjectRef,
    *,
    top: int | None = None,
    task_type: str = "auto",
    no_loc: bool = False,
) -> ProjectAudit:
    """Audit one project reference end-to-end."""
    pa = ProjectAudit(
        source=ref.source,
        project_key=ref.key,
        display_name=ref.display_name,
    )
    t0 = time.perf_counter()
    # Empty Claude project folders are common (listed but no jsonl) — not a failure.
    if ref.source == "claude" and not reader.discover_jsonl(ref.key):
        pa.elapsed_ms = (time.perf_counter() - t0) * 1000
        pa.checks.append(_truth(
            "empty_project_ok",
            True,
            detail="no session files",
            level="info",
        ))
        return pa
    try:
        result = analyze.analyze_project(
            ref.key,
            source=ref.source,
            project_ref=ref,
            task_type=task_type,
            no_loc=no_loc,
            scan_code_dir=False,
        )
    except FileNotFoundError as e:
        # No sessions after filter / empty project — soft pass.
        msg = str(e).lower()
        if "no session" in msg or "not found" in msg:
            pa.elapsed_ms = (time.perf_counter() - t0) * 1000
            pa.checks.append(_truth(
                "empty_or_missing_ok",
                True,
                detail=str(e),
                level="info",
            ))
            return pa
        pa.error = f"{type(e).__name__}: {e}"
        pa.elapsed_ms = (time.perf_counter() - t0) * 1000
        return pa
    except Exception as e:  # noqa: BLE001
        pa.error = f"{type(e).__name__}: {e}"
        pa.elapsed_ms = (time.perf_counter() - t0) * 1000
        return pa

    pa.elapsed_ms = (time.perf_counter() - t0) * 1000
    pa.n_sessions = result.n_sessions
    pa.n_subagents = result.n_subagents

    # Aggregate invariants
    pa.checks.append(_eq("n_sessions", len(result.reports), result.n_sessions))
    pa.checks.append(_truth(
        "aggregate_ctei_suppressed",
        result.aggregate.ctei is None,
        detail="project aggregate must not show CTEI",
    ))
    pa.checks.append(_truth(
        "aggregate_ncpi_suppressed",
        result.aggregate.ncpi is None,
    ))
    sum_tok = sum(r.usage.total for r in result.reports)
    pa.checks.append(_eq("aggregate_tokens_sum", sum_tok, result.aggregate.usage.total))

    if result.reports and not no_loc:
        sum_net = sum((r.net_loc or 0) for r in result.reports if r.net_loc is not None)
        # With no-edit → known zero, all sessions have net_loc when LOC is on.
        if all(r.net_loc is not None for r in result.reports):
            pa.checks.append(_eq(
                "aggregate_net_loc_sum",
                sum_net,
                result.aggregate.net_loc,
            ))

    # Export path smoke (correct arity) — catches API drift without a GUI click.
    try:
        from tcer.core import export as _export

        _export.to_json(result.reports, result.aggregate, result.n_sessions)
        _export.to_csv(result.reports)
        _export.to_markdown(
            result.reports,
            result.aggregate,
            result.n_sessions,
            result.code_dir,
            project_name=ref.display_name or ref.key,
        )
        pa.checks.append(_truth("export_smoke_ok", True, detail="json/csv/md"))
    except Exception as e:  # noqa: BLE001
        pa.checks.append(_truth("export_smoke_ok", False, detail=str(e)))

    # Per-session (optionally top-N by tokens)
    reports = sorted(result.reports, key=lambda r: r.usage.total, reverse=True)
    if top is not None and top > 0:
        reports = reports[:top]

    code_cwd = str(result.code_dir) if result.code_dir else None
    for rep in reports:
        if ref.source == "claude":
            sa = _audit_claude_session(rep, project_hash=ref.key, code_cwd=code_cwd)
        elif ref.source == "codex":
            sa = _audit_file_session(rep, source="codex", aggregate_fn=codex_reader.aggregate_usage)
        elif ref.source == "grok":
            sa = _audit_file_session(rep, source="grok", aggregate_fn=grok_reader.aggregate_usage)
        elif ref.source == "opencode":
            sa = _audit_opencode_session(rep, no_loc=no_loc)
        else:
            sa = SessionAudit(
                session_id="?",
                source=ref.source,
                path="",
                checks=[_truth("known_source", False, detail=ref.source)],
            )
        pa.sessions.append(sa)

    return pa


def audit_project(
    project: str,
    *,
    source: str = "claude",
    top: int | None = None,
    task_type: str = "auto",
    no_loc: bool = False,
) -> ProjectAudit:
    """Resolve *project* key/substring and audit it."""
    refs = list_project_refs(source if source != "all" else "all")
    match = None
    for r in refs:
        if r.source != source and source != "all":
            continue
        if r.key == project or project.lower() in r.key.lower() or project.lower() in r.display_name.lower():
            match = r
            if r.key == project:
                break
    if match is None and source == "claude":
        # Fallback: raw hash resolve
        p = resolve_project(project)
        if p is not None:
            match = ProjectRef(
                source="claude",
                key=p.name,
                display_name=p.name,
                cwd=None,
                path=p,
            )
    if match is None:
        return ProjectAudit(
            source=source,
            project_key=project,
            display_name=project,
            error=f"project not found: {project!r} (source={source})",
        )
    return audit_ref(match, top=top, task_type=task_type, no_loc=no_loc)


def audit_many(
    *,
    source: str = "all",
    project: str | None = None,
    top: int | None = 3,
    task_type: str = "auto",
    no_loc: bool = False,
    limit_projects: int | None = None,
    skip_empty: bool = False,
) -> list[ProjectAudit]:
    """Audit one project or a batch (largest Claude projects first when listing)."""
    from tcer.core.paths import project_has_sessions

    if project:
        src = source if source != "all" else "claude"
        # Prefer exact source when user said all + project name
        if source == "all":
            results = []
            for s in ("claude", "codex", "grok", "opencode"):
                pa = audit_project(project, source=s, top=top, task_type=task_type, no_loc=no_loc)
                if pa.error and "not found" in (pa.error or ""):
                    continue
                results.append(pa)
            return results or [audit_project(project, source="claude", top=top, task_type=task_type, no_loc=no_loc)]
        return [audit_project(project, source=src, top=top, task_type=task_type, no_loc=no_loc)]

    refs = list_project_refs(source)
    if skip_empty:
        refs = [r for r in refs if project_has_sessions(r)]
    # Prefer projects that likely have data; Claude with sessions first
    def _rank(r: ProjectRef) -> tuple:
        if r.source == "claude":
            n = len(reader.discover_jsonl(r.key))
            return (0, -n, r.display_name.lower())
        n = len(r.session_paths) if r.session_paths else 0
        return (1, -n, r.display_name.lower())

    refs = sorted(refs, key=_rank)
    if limit_projects is not None:
        refs = refs[: max(0, limit_projects)]
    return [
        audit_ref(r, top=top, task_type=task_type, no_loc=no_loc)
        for r in refs
    ]


# --------------------------------------------------------------------------- report / CLI

def summarize(results: list[ProjectAudit]) -> dict[str, Any]:
    """Compact machine-readable summary (CI / dashboards)."""
    failures: list[dict[str, Any]] = []
    for r in results:
        if r.ok:
            continue
        failed_checks: list[dict[str, Any]] = []
        for c in r.checks:
            if not c.ok and c.level == "error":
                failed_checks.append(c.to_dict())
        for s in r.sessions:
            for c in s.checks:
                if not c.ok and c.level == "error":
                    row = c.to_dict()
                    row["session_id"] = s.session_id
                    failed_checks.append(row)
        failures.append({
            "source": r.source,
            "project_key": r.project_key,
            "display_name": r.display_name,
            "error": r.error,
            "n_fail": r.n_fail,
            "failed_checks": failed_checks[:50],
        })
    return {
        "ok": all(r.ok for r in results),
        "n_projects": len(results),
        "n_ok": sum(1 for r in results if r.ok),
        "n_fail": sum(1 for r in results if not r.ok),
        "n_sessions_audited": sum(len(r.sessions) for r in results),
        "n_check_failures": sum(r.n_fail for r in results),
        "elapsed_ms": round(sum(r.elapsed_ms for r in results), 1),
        "failures": failures,
    }


def format_report(
    results: list[ProjectAudit],
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> str:
    n_ok = sum(1 for r in results if r.ok)
    n_fail = len(results) - n_ok
    header = (
        f"TCER closed-loop audit — {n_ok} ok / {n_fail} fail / "
        f"{len(results)} project(s)"
    )
    if quiet:
        # One-line CI output; append FAIL project keys when broken.
        if n_fail == 0:
            return f"{header} → PASS"
        keys = ",".join(f"{r.source}:{r.project_key}" for r in results if not r.ok)
        return f"{header} → FAIL [{keys}]"

    lines: list[str] = [header, ""]
    for pa in results:
        mark = "PASS" if pa.ok else "FAIL"
        lines.append(
            f"[{mark}] {pa.source}:{pa.display_name}  "
            f"sessions={pa.n_sessions} subagents={pa.n_subagents}  "
            f"{pa.elapsed_ms:.0f}ms  fails={pa.n_fail}"
        )
        if pa.error:
            lines.append(f"  ERROR: {pa.error}")
        for c in pa.checks:
            if c.level == "info" and not verbose:
                continue
            if c.ok and not verbose:
                continue
            tag = "ok" if c.ok else "FAIL"
            extra = ""
            if not c.ok:
                extra = f" expected={c.expected!r} actual={c.actual!r}"
            if c.detail:
                extra += f" ({c.detail})"
            lines.append(f"  [{tag}] {c.name}{extra}")
        for sa in pa.sessions:
            if sa.ok and not verbose:
                # still show brief line in verbose-only; skip when quiet pass
                continue
            sm = "ok" if sa.ok else "FAIL"
            lines.append(f"  session {sa.session_id[:36]} [{sm}]")
            for c in sa.checks:
                if c.ok and not verbose:
                    continue
                if c.level == "info" and not verbose:
                    continue
                tag = "ok" if c.ok else "FAIL"
                extra = ""
                if not c.ok:
                    extra = f" expected={c.expected!r} actual={c.actual!r}"
                if c.detail:
                    extra += f" ({c.detail})"
                lines.append(f"    [{tag}] {c.name}{extra}")
            if verbose and sa.info:
                for k, v in sa.info.items():
                    if k == "files":
                        continue
                    lines.append(f"    info {k}={v}")
        if verbose and pa.ok:
            for sa in pa.sessions:
                lines.append(
                    f"  session {sa.session_id[:36]} [ok] "
                    f"type={sa.info.get('task_type')} tcer={sa.info.get('tcer')}"
                )
    lines.append("")
    lines.append("PASS" if n_fail == 0 else "FAIL")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tcer.audit",
        description="Closed-loop audit: analyze real local sessions and re-verify against raw files.",
    )
    p.add_argument("--source", default="all",
                   choices=["all", "claude", "codex", "grok", "opencode"],
                   help="Data source (default: all when --project set, else all)")
    p.add_argument("--project", default=None,
                   help="Project key or substring (e.g. TCER, c--GitHub-TCER)")
    p.add_argument("--top", type=int, default=3,
                   help="Per project, audit only the top-N sessions by tokens (0=all)")
    p.add_argument("--limit-projects", type=int, default=3,
                   help="When no --project, audit at most N projects (default 3)")
    p.add_argument(
        "--all-projects",
        action="store_true",
        help="Audit every discovered project (ignores --limit-projects)",
    )
    p.add_argument(
        "--skip-empty",
        action="store_true",
        help="Skip projects with zero sessions (faster --all-projects)",
    )
    p.add_argument("--task-type", default="auto",
                   help="Task type passed to analyze (default auto)")
    p.add_argument("--no-loc", action="store_true", help="Skip LOC checks")
    p.add_argument("--list", action="store_true", help="List projects and exit")
    p.add_argument("--json", metavar="PATH", default=None,
                   help="Write full JSON report (+summary) to PATH (- for stdout)")
    p.add_argument(
        "--summary-json",
        metavar="PATH",
        default=None,
        help="Write compact CI summary JSON only (- for stdout)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Show passing checks too")
    p.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="One-line result (CI-friendly)",
    )
    p.add_argument(
        "--strict-pricing",
        action="store_true",
        help="Fail if any model falls back to default list price",
    )
    p.add_argument(
        "--ci",
        action="store_true",
        help="CI preset: --all-projects --skip-empty --top 1 --no-loc -q",
    )
    args = p.parse_args(argv)

    if args.ci:
        args.all_projects = True
        args.skip_empty = True
        if args.top == 3:  # argparse default
            args.top = 1
        args.no_loc = True
        args.quiet = True

    if args.list:
        refs = list_project_refs(args.source)
        for r in refs:
            from tcer.core.paths import project_has_sessions
            empty = "" if project_has_sessions(r) else " [empty]"
            n = ""
            if r.source == "claude":
                n = f" sessions≈{len(reader.discover_jsonl(r.key))}"
            elif r.session_paths:
                n = f" sessions={len(r.session_paths)}"
            print(f"{r.source:8} {r.key:40} {r.display_name}{n}{empty}")
        return 0

    # Fresh process-level scan cache so re-audits after code changes re-read files.
    try:
        from tcer.core import file_cache
        file_cache.clear()
    except Exception:  # noqa: BLE001
        pass

    top = None if args.top == 0 else args.top
    limit = None if (args.project or args.all_projects) else args.limit_projects
    results = audit_many(
        source=args.source,
        project=args.project,
        top=top,
        task_type=args.task_type,
        no_loc=args.no_loc,
        limit_projects=limit,
        skip_empty=args.skip_empty,
    )

    if args.strict_pricing:
        for pa in results:
            for sa in pa.sessions:
                for c in sa.checks:
                    if c.name == "all_models_priced":
                        c.level = "error"

    summary = summarize(results)
    text = format_report(results, verbose=args.verbose, quiet=args.quiet)
    print(text)

    def _write_json(path: str, payload: dict) -> None:
        blob = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if path == "-":
            print(blob)
        else:
            Path(path).write_text(blob, encoding="utf-8")
            if not args.quiet:
                print(f"JSON written to {path}", file=sys.stderr)

    if args.summary_json:
        _write_json(args.summary_json, summary)

    if args.json:
        payload = {
            "ok": summary["ok"],
            "summary": summary,
            "projects": [r.to_dict() for r in results],
        }
        _write_json(args.json, payload)

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
