"""Project/session analysis orchestration shared by the CLI and the GUI.

Reads a project's session JSONL, **folds each subagent into its parent session**
(so one session = one main file + its subagents, matching how cc-switch counts
sessions), aggregates token usage, derives git-free LOC from file-mutating tool
calls, scans the working directory for accumulated codebase size, and computes
per-session + aggregate reports. The CLI and Tkinter GUI both call ``analyze_project``.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from pathlib import Path

from . import loc, metrics, reader
from .models import SessionMeta, SessionReport, TokenUsage
from .paths import resolve_project


@dataclass
class ProjectAnalysis:
    project_hash: str
    reports: list[SessionReport]  # one per real session (subagents folded in)
    aggregate: SessionReport
    code_dir: Path | None  # directory scanned for accumulated LOC (None if unknown)
    n_sessions: int  # number of real sessions (not counting subagents separately)
    n_subagents: int  # total subagent files folded into the sessions above


def analyze_project(
    project: str,
    *,
    session: str | None = None,
    no_subagents: bool = False,
    code_dir: str | Path | None = None,
    no_loc: bool = False,
    task_type: str = "feature",
    baseline_tcer: float = metrics.TCER_BASELINE,
    baseline_ncpi: float = metrics.NCPI_BASELINE,
    baseline_cpe: float = metrics.CPE_BASELINE,
) -> ProjectAnalysis:
    """Analyze one project (optionally one session) and return per-session + aggregate.

    Subagent JSONL files are merged into their parent session: their tokens and LOC
    are counted (real cost), but they are not listed or counted as separate sessions.
    ``no_subagents=True`` excludes subagent data entirely.

    Raises ``FileNotFoundError`` if the project or any matching session is missing.
    """
    proj = resolve_project(project)
    if proj is None:
        raise FileNotFoundError(f"project '{project}' not found under ~/.claude/projects")

    files = reader.discover_jsonl(proj.name)
    if not files:
        raise FileNotFoundError(f"no session files in {proj}")
    if no_subagents:
        files = [f for f in files if not reader.is_subagent(f)]

    # Group files by parent session id (subagents fold into the owning session).
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(reader.parent_session_id(f), []).append(f)

    if session:
        groups = {k: v for k, v in groups.items() if session in k}
        if not groups:
            raise FileNotFoundError(f"no session matches '{session}'")

    # First pass: per-group metadata (cheap head/tail read) to discover the cwd.
    metas: dict[str, SessionMeta] = {}
    cwd: Path | None = None
    for key, gfiles in groups.items():
        main = next((f for f in gfiles if not reader.is_subagent(f)), None)
        meta = reader.read_session_meta(main) if main else _synth_meta(key, gfiles[0])
        metas[key] = meta
        if cwd is None and meta.cwd:
            cwd = Path(meta.cwd)

    code_path = Path(code_dir) if code_dir else cwd
    loc_total = None if no_loc else (loc.tree_loc(code_path) if code_path else None)

    def _mk(meta, u, net, added, deleted, n_sub) -> SessionReport:
        rep = metrics.compute(
            meta, u, net,
            loc_accumulated=loc_total, task_type=task_type,
            code_added=added, code_deleted=deleted,
            tcer_baseline=baseline_tcer, ncpi_baseline=baseline_ncpi,
            cpe_baseline=baseline_cpe,
        )
        rep.subagent_count = n_sub
        return rep

    # Second pass: merge usage + LOC per group, build one report per session.
    reports: list[SessionReport] = []
    tot_added = tot_deleted = 0
    total_subs = 0
    agg_u = TokenUsage()
    for key, gfiles in groups.items():
        gu = reduce(lambda a, b: a.merge(b),
                    (reader.aggregate_usage(f) for f in gfiles), TokenUsage())
        n_sub = sum(1 for f in gfiles if reader.is_subagent(f))
        total_subs += n_sub
        agg_u = agg_u.merge(gu)
        if no_loc:
            reports.append(_mk(metas[key], gu, None, None, None, n_sub))
            continue
        added = deleted = 0
        for f in gfiles:
            a, d = loc.session_loc(f)
            added += a
            deleted += d
        tot_added += added
        tot_deleted += deleted
        reports.append(_mk(metas[key], gu, added - deleted, added, deleted, n_sub))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else None,
        title=None, path=proj, is_subagent=False,
    )
    if no_loc:
        agg = _mk(agg_meta, agg_u, None, None, None, total_subs)
    else:
        agg = _mk(agg_meta, agg_u, tot_added - tot_deleted, tot_added, tot_deleted, total_subs)

    return ProjectAnalysis(
        project_hash=proj.name, reports=reports, aggregate=agg,
        code_dir=code_path, n_sessions=len(reports), n_subagents=total_subs,
    )


def _synth_meta(session_id: str, sample: Path) -> SessionMeta:
    """Metadata for a session whose main file is missing (orphan subagents only)."""
    return SessionMeta(session_id=session_id, cwd=None, title=None,
                       path=sample, is_subagent=False)
