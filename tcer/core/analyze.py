"""Project/session analysis orchestration shared by the CLI and the GUI.

Reads a project's session JSONL, **folds each subagent into its parent session**
(so one session = one main file + its subagents, matching how cc-switch counts
sessions), aggregates token usage, derives git-free LOC from file-mutating tool
calls, scans the working directory for accumulated codebase size, and computes
per-session + aggregate reports. The CLI and Tkinter GUI both call ``analyze_project``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import reduce
from pathlib import Path

from tcer.core import codex_reader, loc, metrics, opencode_reader, reader
from tcer.core.models import ProjectRef, SessionMeta, SessionReport, TokenUsage
from tcer.core.paths import resolve_project


@dataclass
class ProjectAnalysis:
    project_hash: str
    reports: list[SessionReport]  # one per real session (subagents folded in)
    aggregate: SessionReport
    code_dir: Path | None  # directory scanned for accumulated LOC (None if unknown)
    n_sessions: int  # number of real sessions (not counting subagents separately)
    n_subagents: int  # total subagent files folded into the sessions above
    source: str = "claude"
    project_ref: ProjectRef | None = None


def analyze_project(
    project: str,
    *,
    source: str = "claude",
    project_ref: ProjectRef | None = None,
    session: str | None = None,
    no_subagents: bool = False,
    code_dir: str | Path | None = None,
    no_loc: bool = False,
    task_type: str = "feature",
    baseline_tcer: float = metrics.TCER_BASELINE,
    baseline_ncpi: float = metrics.NCPI_BASELINE,
    baseline_cpe: float = metrics.CPE_BASELINE,
    since: str | None = None,
    until: str | None = None,
) -> ProjectAnalysis:
    """Analyze one project (optionally one session) and return per-session + aggregate.

    Subagent JSONL files are merged into their parent session: their tokens and LOC
    are counted (real cost), but they are not listed or counted as separate sessions.
    ``no_subagents=True`` excludes subagent data entirely.

    Time filters ``since`` / ``until`` (YYYY-MM-DD strings) include sessions whose
    ``started_at`` falls within the range (inclusive). Sessions without timestamps
    are excluded.

    Raises ``FileNotFoundError`` if the project or any matching session is missing.
    """
    if source == "codex" or (project_ref and project_ref.source == "codex"):
        return _analyze_codex_project(
            project_ref or project,
            session=session,
            code_dir=code_dir,
            no_loc=no_loc,
            task_type=task_type,
            baseline_tcer=baseline_tcer,
            baseline_ncpi=baseline_ncpi,
            baseline_cpe=baseline_cpe,
            since=since,
            until=until,
        )
    if source == "opencode" or (project_ref and project_ref.source == "opencode"):
        return _analyze_opencode_project(
            project_ref or project,
            session=session,
            code_dir=code_dir,
            no_loc=no_loc,
            task_type=task_type,
            baseline_tcer=baseline_tcer,
            baseline_ncpi=baseline_ncpi,
            baseline_cpe=baseline_cpe,
            since=since,
            until=until,
        )

    proj = resolve_project(project)
    if proj is None:
        raise FileNotFoundError(f"project '{project}' not found under ~/.claude/projects")

    files = reader.discover_jsonl(proj.name)
    if not files:
        raise FileNotFoundError(f"no session files in {proj}")
    if no_subagents:
        files = [f for f in files if not reader.is_subagent(f)]

    # Time filtering: parse YYYY-MM-DD to ms timestamp, filter sessions by started_at.
    since_ms = _parse_date_to_ms(since) if since else None
    until_ms = _parse_date_to_ms(until, end_of_day=True) if until else None
    if since_ms or until_ms:
        filtered = []
        for f in files:
            u = reader.aggregate_usage(f)
            if u.started_at is None:
                continue  # skip sessions with no timestamp
            if since_ms and u.started_at < since_ms:
                continue
            if until_ms and u.started_at > until_ms:
                continue
            filtered.append(f)
        files = filtered

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
    loc_total = None if no_loc else (
        loc.tree_loc(code_path) if code_path and _is_project_dir(code_path) else None
    )

    def _mk(meta, u, net, added, deleted, n_sub, unseen, sloc=None) -> SessionReport:
        # Extract file-level quality metrics from SessionLoc if available
        high_churn = 0
        test_net = None
        doc_net = None
        reworked = None
        if sloc:
            high_churn = sloc.high_churn_files
            test_net = sloc.test_added - sloc.test_deleted
            doc_net = sloc.doc_added - sloc.doc_deleted
            reworked = sloc.rework_deleted

        rep = metrics.compute(
            meta, u, net,
            loc_accumulated=loc_total, task_type=task_type,
            code_added=added, code_deleted=deleted,
            code_reworked=reworked,
            high_churn_files=high_churn,
            test_net_loc=test_net,
            doc_net_loc=doc_net,
            tcer_baseline=baseline_tcer, ncpi_baseline=baseline_ncpi,
            cpe_baseline=baseline_cpe,
        )
        rep.subagent_count = n_sub
        rep.unseen_writes = unseen
        # Populate high-churn file details (path→count for files edited ≥3).
        # For merged sessions, union across all slocs; counts are approximate
        # (each sloc's file_edit_counts starts from scratch).
        if sloc:
            details = {}
            for fp, cnt in sloc.file_edit_counts.items():
                if cnt >= 3:
                    details[fp] = details.get(fp, 0) + cnt
            if details:
                rep.high_churn_details = dict(sorted(details.items(), key=lambda x: -x[1]))
        # Fill subagent_density (subagent_count / effective_turns)
        if u.effective_turns:
            rep.subagent_density = n_sub / u.effective_turns
        return rep

    # Second pass: merge usage + LOC per group, build one report per session.
    reports: list[SessionReport] = []
    tot_added = tot_deleted = tot_unseen = 0
    tot_rework = 0
    tot_high_churn = 0
    tot_test_added = tot_test_deleted = 0
    tot_doc_added = tot_doc_deleted = 0
    total_subs = 0
    tot_file_edit_counts: dict[str, int] = {}
    agg_u = TokenUsage()
    for key, gfiles in groups.items():
        gu = reduce(lambda a, b: a.merge(b),
                    (reader.aggregate_usage(f) for f in gfiles), TokenUsage())
        n_sub = sum(1 for f in gfiles if reader.is_subagent(f))
        total_subs += n_sub
        agg_u = agg_u.merge(gu)
        if no_loc:
            reports.append(_mk(metas[key], gu, None, None, None, n_sub, unseen=0))
            continue
        # Sum LOC across all files in this group (main session + its subagents).
        slocs = [loc.session_loc_full(f) for f in gfiles]
        added = sum(s.added for s in slocs)
        deleted = sum(s.deleted for s in slocs)
        unseen = sum(s.unseen_writes for s in slocs)
        rework = sum(s.rework_deleted for s in slocs)
        # Aggregate file-level quality metrics
        merged_sloc = loc.SessionLoc(
            added=added,
            deleted=deleted,
            unseen_writes=unseen,
            rework_deleted=rework,
            high_churn_files=sum(s.high_churn_files for s in slocs),
            test_added=sum(s.test_added for s in slocs),
            test_deleted=sum(s.test_deleted for s in slocs),
            doc_added=sum(s.doc_added for s in slocs),
            doc_deleted=sum(s.doc_deleted for s in slocs),
        )
        # Merge file_edit_counts from all slocs so high_churn_details works.
        for s in slocs:
            for fp, cnt in s.file_edit_counts.items():
                merged_sloc.file_edit_counts[fp] = merged_sloc.file_edit_counts.get(fp, 0) + cnt
        tot_added += added
        tot_deleted += deleted
        tot_unseen += unseen
        tot_rework += rework
        tot_high_churn += merged_sloc.high_churn_files
        tot_test_added += merged_sloc.test_added
        tot_test_deleted += merged_sloc.test_deleted
        tot_doc_added += merged_sloc.doc_added
        tot_doc_deleted += merged_sloc.doc_deleted
        for fp, cnt in merged_sloc.file_edit_counts.items():
            tot_file_edit_counts[fp] = tot_file_edit_counts.get(fp, 0) + cnt
        reports.append(_mk(metas[key], gu, added - deleted, added, deleted, n_sub, unseen, merged_sloc))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else None,
        title=None, path=proj, is_subagent=False,
    )
    if no_loc:
        agg = _mk(agg_meta, agg_u, None, None, None, total_subs, unseen=0)
    else:
        agg_sloc = loc.SessionLoc(
            added=tot_added,
            deleted=tot_deleted,
            unseen_writes=tot_unseen,
            rework_deleted=tot_rework,
            high_churn_files=tot_high_churn,
            test_added=tot_test_added,
            test_deleted=tot_test_deleted,
            doc_added=tot_doc_added,
            doc_deleted=tot_doc_deleted,
            file_edit_counts=tot_file_edit_counts,
        )
        agg = _mk(agg_meta, agg_u, tot_added - tot_deleted, tot_added, tot_deleted, total_subs, tot_unseen, agg_sloc)

    # NCPI / CTEI / grade are per-session concepts: NCPI = net_loc / current
    # codebase size. For the aggregate, ``net_loc`` is the *sum* of every
    # session's output over the whole project life (including rewrites and the F1
    # Write-overwrite overcount), while the denominator is the codebase's *current*
    # snapshot — so the ratio routinely exceeds 1 (a project writes more lines over
    # its life than it currently contains) and the multiplicative CTEI then
    # explodes. Suppress them at the aggregate level rather than show a misleading
    # "优秀". Per-session NCPI/CTEI (shown in the ranking tab) stay valid; TCER /
    # PSAC / NTCER remain meaningful as aggregates and are kept.
    agg.ncpi = None
    agg.ctei = None
    agg.grade = None

    # Project-level memory files (read from disk once for the aggregate).
    mem_dir = proj / "memory"
    if mem_dir.is_dir():
        agg.memory_files = sorted(
            str(f) for f in mem_dir.iterdir() if f.is_file()
        )
        agg.memory_dir = str(mem_dir)

    return ProjectAnalysis(
        project_hash=proj.name, reports=reports, aggregate=agg,
        code_dir=code_path, n_sessions=len(reports), n_subagents=total_subs,
        source="claude", project_ref=project_ref,
    )


def _analyze_codex_project(
    project: str | ProjectRef,
    *,
    session: str | None = None,
    code_dir: str | Path | None = None,
    no_loc: bool = False,
    task_type: str = "code_creation",
    baseline_tcer: float = metrics.TCER_BASELINE,
    baseline_ncpi: float = metrics.NCPI_BASELINE,
    baseline_cpe: float = metrics.CPE_BASELINE,
    since: str | None = None,
    until: str | None = None,
) -> ProjectAnalysis:
    """Analyze a Codex cwd-grouped project."""
    ref = project if isinstance(project, ProjectRef) else codex_reader.resolve_project(project)
    if ref is None:
        raise FileNotFoundError(f"codex project '{project}' not found under ~/.codex/sessions")
    files = codex_reader.sessions_for_project(ref)
    if not files:
        raise FileNotFoundError(f"no Codex session files for '{ref.display_name}'")

    since_ms = _parse_date_to_ms(since) if since else None
    until_ms = _parse_date_to_ms(until, end_of_day=True) if until else None
    if since_ms or until_ms:
        filtered = []
        for f in files:
            u = codex_reader.aggregate_usage(f)
            if u.started_at is None:
                continue
            if since_ms and u.started_at < since_ms:
                continue
            if until_ms and u.started_at > until_ms:
                continue
            filtered.append(f)
        files = filtered

    if session:
        files = [
            f for f in files
            if session in (codex_reader.read_session_meta(f).session_id or f.stem)
        ]
        if not files:
            raise FileNotFoundError(f"no Codex session matches '{session}'")

    code_path = Path(code_dir) if code_dir else (Path(ref.cwd) if ref.cwd else None)
    loc_total = None if no_loc else (
        loc.tree_loc(code_path) if code_path and _is_project_dir(code_path) else None
    )

    def _mk(meta, u, net, added, deleted, sloc=None) -> SessionReport:
        high_churn = sloc.high_churn_files if sloc else 0
        test_net = (sloc.test_added - sloc.test_deleted) if sloc else None
        doc_net = (sloc.doc_added - sloc.doc_deleted) if sloc else None
        reworked = sloc.rework_deleted if sloc else None
        rep = metrics.compute(
            meta, u, net,
            loc_accumulated=loc_total, task_type=task_type,
            code_added=added, code_deleted=deleted, code_reworked=reworked,
            high_churn_files=high_churn,
            test_net_loc=test_net, doc_net_loc=doc_net,
            tcer_baseline=baseline_tcer, ncpi_baseline=baseline_ncpi,
            cpe_baseline=baseline_cpe,
        )
        if sloc:
            details = {fp: cnt for fp, cnt in sloc.file_edit_counts.items() if cnt >= 3}
            if details:
                rep.high_churn_details = dict(sorted(details.items(), key=lambda x: -x[1]))
        return rep

    reports: list[SessionReport] = []
    agg_u = TokenUsage()
    tot_added = tot_deleted = tot_rework = 0
    tot_high_churn = 0
    tot_test_added = tot_test_deleted = 0
    tot_doc_added = tot_doc_deleted = 0
    tot_file_edit_counts: dict[str, int] = {}
    all_loc_known = not no_loc

    for f in files:
        meta = codex_reader.read_session_meta(f)
        u = codex_reader.aggregate_usage(f)
        agg_u = agg_u.merge(u)
        if no_loc or not codex_reader.has_loc_signal(f):
            all_loc_known = False
            reports.append(_mk(meta, u, None, None, None))
            continue

        sloc = codex_reader.session_loc_full(f)
        tot_added += sloc.added
        tot_deleted += sloc.deleted
        tot_rework += sloc.rework_deleted
        tot_high_churn += sloc.high_churn_files
        tot_test_added += sloc.test_added
        tot_test_deleted += sloc.test_deleted
        tot_doc_added += sloc.doc_added
        tot_doc_deleted += sloc.doc_deleted
        for fp, cnt in sloc.file_edit_counts.items():
            tot_file_edit_counts[fp] = tot_file_edit_counts.get(fp, 0) + cnt
        reports.append(_mk(meta, u, sloc.added - sloc.deleted, sloc.added, sloc.deleted, sloc))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else ref.cwd,
        title=None, path=ref.path or (files[0].parent if files else Path(".")),
        is_subagent=False, entrypoint="codex", source="codex",
    )
    if all_loc_known:
        agg_sloc = loc.SessionLoc(
            added=tot_added,
            deleted=tot_deleted,
            unseen_writes=0,
            rework_deleted=tot_rework,
            high_churn_files=tot_high_churn,
            test_added=tot_test_added,
            test_deleted=tot_test_deleted,
            doc_added=tot_doc_added,
            doc_deleted=tot_doc_deleted,
            file_edit_counts=tot_file_edit_counts,
        )
        agg = _mk(agg_meta, agg_u, tot_added - tot_deleted, tot_added, tot_deleted, agg_sloc)
    else:
        agg = _mk(agg_meta, agg_u, None, None, None)
    agg.ncpi = None
    agg.ctei = None
    agg.grade = None

    return ProjectAnalysis(
        project_hash=ref.key,
        reports=reports,
        aggregate=agg,
        code_dir=code_path,
        n_sessions=len(reports),
        n_subagents=0,
        source="codex",
        project_ref=ref,
    )


def _analyze_opencode_project(
    project: str | ProjectRef,
    *,
    session: str | None = None,
    code_dir: str | Path | None = None,
    no_loc: bool = False,
    task_type: str = "code_creation",
    baseline_tcer: float = metrics.TCER_BASELINE,
    baseline_ncpi: float = metrics.NCPI_BASELINE,
    baseline_cpe: float = metrics.CPE_BASELINE,
    since: str | None = None,
    until: str | None = None,
) -> ProjectAnalysis:
    """Analyze an OpenCode project from its local SQLite database."""
    ref = project if isinstance(project, ProjectRef) else opencode_reader.resolve_project(project)
    if ref is None or ref.path is None:
        raise FileNotFoundError(f"opencode project '{project}' not found under ~/.local/share/opencode")
    session_ids = opencode_reader.sessions_for_project(ref)
    if not session_ids:
        raise FileNotFoundError(f"no OpenCode sessions for '{ref.display_name}'")

    db_path = ref.path
    since_ms = _parse_date_to_ms(since) if since else None
    until_ms = _parse_date_to_ms(until, end_of_day=True) if until else None
    if since_ms or until_ms:
        filtered = []
        for sid in session_ids:
            u = opencode_reader.aggregate_usage(db_path, sid)
            if u.started_at is None:
                continue
            if since_ms and u.started_at < since_ms:
                continue
            if until_ms and u.started_at > until_ms:
                continue
            filtered.append(sid)
        session_ids = filtered

    if session:
        session_ids = [sid for sid in session_ids if session in sid]
        if not session_ids:
            raise FileNotFoundError(f"no OpenCode session matches '{session}'")

    code_path = Path(code_dir) if code_dir else (Path(ref.cwd) if ref.cwd else None)
    loc_total = None if no_loc else (
        loc.tree_loc(code_path) if code_path and _is_project_dir(code_path) else None
    )

    def _mk(meta, u, net, added, deleted, sloc=None) -> SessionReport:
        high_churn = sloc.high_churn_files if sloc else 0
        test_net = (sloc.test_added - sloc.test_deleted) if sloc else None
        doc_net = (sloc.doc_added - sloc.doc_deleted) if sloc else None
        reworked = sloc.rework_deleted if sloc else None
        rep = metrics.compute(
            meta, u, net,
            loc_accumulated=loc_total, task_type=task_type,
            code_added=added, code_deleted=deleted, code_reworked=reworked,
            high_churn_files=high_churn,
            test_net_loc=test_net, doc_net_loc=doc_net,
            tcer_baseline=baseline_tcer, ncpi_baseline=baseline_ncpi,
            cpe_baseline=baseline_cpe,
        )
        if sloc:
            details = {fp: cnt for fp, cnt in sloc.file_edit_counts.items() if cnt >= 3}
            if details:
                rep.high_churn_details = dict(sorted(details.items(), key=lambda x: -x[1]))
        return rep

    reports: list[SessionReport] = []
    agg_u = TokenUsage()
    tot_added = tot_deleted = tot_rework = 0
    tot_high_churn = 0
    tot_test_added = tot_test_deleted = 0
    tot_doc_added = tot_doc_deleted = 0
    tot_file_edit_counts: dict[str, int] = {}
    all_loc_known = not no_loc

    for sid in session_ids:
        meta = opencode_reader.read_session_meta(db_path, sid)
        u = opencode_reader.aggregate_usage(db_path, sid)
        agg_u = agg_u.merge(u)
        if no_loc or not opencode_reader.has_loc_signal(db_path, sid):
            all_loc_known = False
            reports.append(_mk(meta, u, None, None, None))
            continue
        sloc = opencode_reader.session_loc_full(db_path, sid)
        tot_added += sloc.added
        tot_deleted += sloc.deleted
        tot_rework += sloc.rework_deleted
        tot_high_churn += sloc.high_churn_files
        tot_test_added += sloc.test_added
        tot_test_deleted += sloc.test_deleted
        tot_doc_added += sloc.doc_added
        tot_doc_deleted += sloc.doc_deleted
        for fp, cnt in sloc.file_edit_counts.items():
            tot_file_edit_counts[fp] = tot_file_edit_counts.get(fp, 0) + cnt
        reports.append(_mk(meta, u, sloc.added - sloc.deleted, sloc.added, sloc.deleted, sloc))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else ref.cwd,
        title=None, path=db_path, is_subagent=False, entrypoint="opencode", source="opencode",
    )
    if all_loc_known:
        agg_sloc = loc.SessionLoc(
            added=tot_added,
            deleted=tot_deleted,
            unseen_writes=0,
            rework_deleted=tot_rework,
            high_churn_files=tot_high_churn,
            test_added=tot_test_added,
            test_deleted=tot_test_deleted,
            doc_added=tot_doc_added,
            doc_deleted=tot_doc_deleted,
            file_edit_counts=tot_file_edit_counts,
        )
        agg = _mk(agg_meta, agg_u, tot_added - tot_deleted, tot_added, tot_deleted, agg_sloc)
    else:
        agg = _mk(agg_meta, agg_u, None, None, None)
    agg.ncpi = None
    agg.ctei = None
    agg.grade = None

    return ProjectAnalysis(
        project_hash=ref.key,
        reports=reports,
        aggregate=agg,
        code_dir=code_path,
        n_sessions=len(reports),
        n_subagents=0,
        source="opencode",
        project_ref=ref,
    )


def _synth_meta(session_id: str, sample: Path) -> SessionMeta:
    """Metadata for a session whose main file is missing (orphan subagents only)."""
    return SessionMeta(session_id=session_id, cwd=None, title=None,
                       path=sample, is_subagent=False)


# Project marker files that indicate a real project root.
_PROJECT_MARKERS = frozenset({
    ".git", ".hg", ".svn",
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "go.sum",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "Makefile", "CMakeLists.txt", "meson.build",
    "Gemfile", "composer.json", "mix.exs",
    ".claude",  # Claude Code project directory
})


def _is_project_dir(path: Path) -> bool:
    """True if *path* looks like an actual project directory worth scanning for LOC.

    Returns False for home directories, drive roots, and system directories —
    places where a Claude Code session might happen to run (``cd ~``) but that
    are not themselves a codebase.  The heuristic is: any directory containing a
    project marker file (``.git``, ``package.json``, ``pyproject.toml``, etc.)
    is accepted; directories that match known non-project patterns are rejected.
    """
    resolved = path.resolve()

    # Home directory (e.g. C:\Users\Administrator, /home/alice)
    try:
        if resolved == Path.home().resolve():
            return False
    except Exception:
        pass

    # Drive roots: C:\, D:\, /, etc.
    if resolved == resolved.root or resolved == resolved.anchor.rstrip("\\/"):
        return False

    # Windows system directories
    parts_lower = [p.lower() for p in resolved.parts]
    if sys.platform == "win32":
        if any(s in parts_lower for s in ("windows", "program files", "program files (x86)")):
            return False

    # Linux/macOS top-level directories
    if len(resolved.parts) <= 2 and resolved.parts[0] == "/":
        if resolved.name in ("usr", "tmp", "var", "etc", "opt", "root", "proc", "sys"):
            return False

    # Positive check: project marker present → definitely a project
    try:
        children = set(entry.name for entry in resolved.iterdir())
    except OSError:
        return False
    if children & _PROJECT_MARKERS:
        return True

    # No markers found and path is very shallow (e.g. C:\Users\Administrator
    # which has only user-profile subdirs) → treat as non-project.
    # Real projects without markers are still accepted if they have code files.
    return any(
        loc._is_code(fn)
        for fn in children
        if "." in fn
    )


def _parse_date_to_ms(date_str: str, end_of_day: bool = False) -> int:
    """Parse YYYY-MM-DD to ms timestamp (start or end of day UTC).

    Raises ValueError on malformed input.
    """
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            # End of day = 23:59:59.999999
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return int(dt.timestamp() * 1000)
    except ValueError as e:
        raise ValueError(f"Invalid date format '{date_str}' (expected YYYY-MM-DD)") from e
