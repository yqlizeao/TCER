"""Project/session analysis orchestration shared by the CLI and the GUI.

Reads a project's session JSONL, **folds each subagent into its parent session**
(so one session = one main file + its subagents, matching how cc-switch counts
sessions), aggregates token usage, derives git-free LOC from file-mutating tool
calls, scans the working directory for accumulated codebase size, and computes
per-session + aggregate reports. The CLI and Tkinter GUI both call ``analyze_project``.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import Callable

from tcer.core import codex_reader, grok_reader, loc, metrics, opencode_reader, reader
from tcer.core.models import ProjectRef, SessionMeta, SessionReport, TokenUsage
from tcer.core.paths import resolve_project


class AnalysisCancelled(Exception):
    """Raised when a cooperative cancel check fires mid-analysis."""


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


@dataclass(frozen=True)
class _MetricCtx:
    """Shared knobs for per-session ``metrics.compute`` across sources."""

    loc_total: int | None
    task_type: str
    baseline_tcer: float
    baseline_ncpi: float
    baseline_cpe: float
    auto_infer: bool = False


def _make_cancel_check(
    cancel_event: threading.Event | None,
) -> Callable[[], None] | None:
    if cancel_event is None:
        return None

    def _check() -> None:
        if cancel_event.is_set():
            raise AnalysisCancelled()

    return _check


def _filter_by_started_at(
    files: list[Path],
    usage_of: Callable[[Path], TokenUsage],
    since: str | None,
    until: str | None,
) -> list[Path]:
    """Keep files whose usage ``started_at`` falls in [since, until] (YYYY-MM-DD)."""
    since_ms = _parse_date_to_ms(since) if since else None
    until_ms = _parse_date_to_ms(until, end_of_day=True) if until else None
    if not since_ms and not until_ms:
        return files
    filtered: list[Path] = []
    for f in files:
        u = usage_of(f)
        if u.started_at is None:
            continue
        if since_ms and u.started_at < since_ms:
            continue
        if until_ms and u.started_at > until_ms:
            continue
        filtered.append(f)
    return filtered


def _mk_report(
    meta: SessionMeta,
    u: TokenUsage,
    net: int | None,
    added: int | None,
    deleted: int | None,
    *,
    ctx: _MetricCtx,
    n_sub: int = 0,
    unseen: int = 0,
    sloc: loc.SessionLoc | None = None,
    set_subagent_density: bool = False,
    task_type_override: str | None = None,
) -> SessionReport:
    """Build one SessionReport from usage + optional LOC (shared by all sources)."""
    high_churn = 0
    test_net = None
    doc_net = None
    reworked = None
    if sloc:
        high_churn = sloc.high_churn_files
        test_net = sloc.test_added - sloc.test_deleted
        doc_net = sloc.doc_added - sloc.doc_deleted
        reworked = sloc.rework_deleted

    if task_type_override:
        tt = task_type_override
    elif ctx.auto_infer:
        tt = metrics.infer_task_type_from_usage(u, net_loc=net)
    else:
        tt = ctx.task_type

    rep = metrics.compute(
        meta, u, net,
        loc_accumulated=ctx.loc_total,
        task_type=tt,
        code_added=added,
        code_deleted=deleted,
        code_reworked=reworked,
        high_churn_files=high_churn,
        test_net_loc=test_net,
        doc_net_loc=doc_net,
        tcer_baseline=ctx.baseline_tcer,
        ncpi_baseline=ctx.baseline_ncpi,
        cpe_baseline=ctx.baseline_cpe,
    )
    rep.subagent_count = n_sub
    rep.unseen_writes = unseen
    if sloc:
        details = {fp: cnt for fp, cnt in sloc.file_edit_counts.items() if cnt >= 3}
        if details:
            rep.high_churn_details = dict(sorted(details.items(), key=lambda x: -x[1]))
    if set_subagent_density and u.effective_turns:
        rep.subagent_density = n_sub / u.effective_turns
    return rep


def _agg_sloc(
    *,
    added: int,
    deleted: int,
    unseen: int = 0,
    rework: int = 0,
    test_added: int = 0,
    test_deleted: int = 0,
    doc_added: int = 0,
    doc_deleted: int = 0,
    file_edit_counts: dict[str, int] | None = None,
) -> loc.SessionLoc:
    sl = loc.SessionLoc(
        added=added,
        deleted=deleted,
        unseen_writes=unseen,
        rework_deleted=rework,
        test_added=test_added,
        test_deleted=test_deleted,
        doc_added=doc_added,
        doc_deleted=doc_deleted,
        file_edit_counts=dict(file_edit_counts or {}),
    )
    sl.recompute_high_churn()
    return sl


def _suppress_aggregate_session_metrics(agg: SessionReport) -> None:
    """NCPI / CTEI / grade are per-session only — clear on project aggregates."""
    agg.ncpi = None
    agg.ctei = None
    agg.grade = None


def _accumulate_sloc_totals(
    sloc: loc.SessionLoc,
    totals: dict,
) -> None:
    """Add one session's LOC into running aggregate counters (mutates ``totals``)."""
    totals["added"] += sloc.added
    totals["deleted"] += sloc.deleted
    totals["unseen"] += sloc.unseen_writes
    totals["rework"] += sloc.rework_deleted
    totals["test_added"] += sloc.test_added
    totals["test_deleted"] += sloc.test_deleted
    totals["doc_added"] += sloc.doc_added
    totals["doc_deleted"] += sloc.doc_deleted
    counts: dict[str, int] = totals["file_edit_counts"]
    for fp, cnt in sloc.file_edit_counts.items():
        counts[fp] = counts.get(fp, 0) + cnt


def _empty_loc_totals() -> dict:
    return {
        "added": 0,
        "deleted": 0,
        "unseen": 0,
        "rework": 0,
        "test_added": 0,
        "test_deleted": 0,
        "doc_added": 0,
        "doc_deleted": 0,
        "file_edit_counts": {},
    }


def _totals_to_sloc(totals: dict) -> loc.SessionLoc:
    return _agg_sloc(
        added=totals["added"],
        deleted=totals["deleted"],
        unseen=totals["unseen"],
        rework=totals["rework"],
        test_added=totals["test_added"],
        test_deleted=totals["test_deleted"],
        doc_added=totals["doc_added"],
        doc_deleted=totals["doc_deleted"],
        file_edit_counts=totals["file_edit_counts"],
    )


def analyze_project(
    project: str,
    *,
    source: str = "claude",
    project_ref: ProjectRef | None = None,
    session: str | None = None,
    no_subagents: bool = False,
    code_dir: str | Path | None = None,
    no_loc: bool = False,
    scan_code_dir: bool = False,
    task_type: str = metrics.DEFAULT_TASK_TYPE,
    baseline_tcer: float | None = None,
    baseline_ncpi: float | None = None,
    baseline_cpe: float | None = None,
    since: str | None = None,
    until: str | None = None,
    cancel_event: threading.Event | None = None,
) -> ProjectAnalysis:
    """Analyze one project (optionally one session) and return per-session + aggregate.

    Subagent JSONL files are merged into their parent session: their tokens and LOC
    are counted (real cost), but they are not listed or counted as separate sessions.
    ``no_subagents=True`` excludes subagent data entirely.

    Time filters ``since`` / ``until`` (YYYY-MM-DD strings) include sessions whose
    ``started_at`` falls within the range (inclusive). Sessions without timestamps
    are excluded.

    ``cancel_event``: optional cooperative cancel; when set, raises
    :class:`AnalysisCancelled` between sessions / mid-JSONL scan.

    Raises ``FileNotFoundError`` if the project or any matching session is missing.
    """
    # Def-time defaults would freeze pre-save_baselines() values; resolve lazily
    # so a GUI "保存个人基准" takes effect without restarting (see metrics._refresh_composite_globals).
    if baseline_tcer is None:
        baseline_tcer = metrics.TCER_BASELINE
    if baseline_ncpi is None:
        baseline_ncpi = metrics.NCPI_BASELINE
    if baseline_cpe is None:
        baseline_cpe = metrics.CPE_BASELINE
    auto_infer = metrics.is_auto_task_type(task_type)
    if not auto_infer:
        task_type = metrics.resolve_task_type(task_type)
    else:
        task_type = metrics.DEFAULT_TASK_TYPE  # placeholder; per-session inference wins
    cancel_check = _make_cancel_check(cancel_event)

    if source == "codex" or (project_ref and project_ref.source == "codex"):
        return _analyze_codex_project(
            project_ref or project,
            session=session,
            code_dir=code_dir,
            no_loc=no_loc,
            scan_code_dir=scan_code_dir,
            task_type=task_type,
            baseline_tcer=baseline_tcer,
            baseline_ncpi=baseline_ncpi,
            baseline_cpe=baseline_cpe,
            since=since,
            until=until,
            cancel_check=cancel_check,
            auto_infer=auto_infer,
        )
    if source == "opencode" or (project_ref and project_ref.source == "opencode"):
        return _analyze_opencode_project(
            project_ref or project,
            session=session,
            code_dir=code_dir,
            no_loc=no_loc,
            scan_code_dir=scan_code_dir,
            task_type=task_type,
            baseline_tcer=baseline_tcer,
            baseline_ncpi=baseline_ncpi,
            baseline_cpe=baseline_cpe,
            since=since,
            until=until,
            cancel_check=cancel_check,
            auto_infer=auto_infer,
        )
    if source == "grok" or (project_ref and project_ref.source == "grok"):
        return _analyze_grok_project(
            project_ref or project,
            session=session,
            code_dir=code_dir,
            no_loc=no_loc,
            scan_code_dir=scan_code_dir,
            task_type=task_type,
            baseline_tcer=baseline_tcer,
            baseline_ncpi=baseline_ncpi,
            baseline_cpe=baseline_cpe,
            since=since,
            until=until,
            cancel_check=cancel_check,
            auto_infer=auto_infer,
        )

    proj = resolve_project(project)
    if proj is None:
        raise FileNotFoundError(f"project '{project}' not found under ~/.claude/projects")

    files = reader.discover_jsonl(proj.name)
    if not files:
        raise FileNotFoundError(f"no session files in {proj}")
    if no_subagents:
        files = [f for f in files if not reader.is_subagent(f)]

    # Per-call memo (also backed by process-level mtime cache in scan_session).
    # User message bodies are omitted here — popup uses reader.read_user_messages.
    # Key includes cwd so F1 disk prior for relative paths can be recomputed correctly.
    scan_memo: dict[tuple[Path, str], tuple[TokenUsage, loc.SessionLoc | None]] = {}

    def _scan_of(
        f: Path, *, cwd: str | None = None,
    ) -> tuple[TokenUsage, loc.SessionLoc | None]:
        key = (f, cwd or "")
        hit = scan_memo.get(key)
        if hit is None:
            if cancel_check:
                cancel_check()
            hit = reader.scan_session(
                f,
                with_loc=not no_loc,
                include_user_texts=False,
                cwd=cwd,
                # Post-session disk is not a reliable Write prior (intermediate
                # Writes + later Edits leave disk ≠ Write payload → false deletes).
                # Opt into disk_prior=True only for deliberate F1 calibration.
                disk_prior=False,
                cancel_check=cancel_check,
            )
            scan_memo[key] = hit
        return hit

    def _usage_of(f: Path) -> TokenUsage:
        # Date filter only needs usage; skip disk prior work when no_loc.
        return _scan_of(f)[0]

    files = _filter_by_started_at(files, _usage_of, since, until)

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
        if cancel_check:
            cancel_check()
        main = next((f for f in gfiles if not reader.is_subagent(f)), None)
        meta = reader.read_session_meta(main) if main else _synth_meta(key, gfiles[0])
        metas[key] = meta
        if cwd is None and meta.cwd:
            cwd = Path(meta.cwd)

    code_path = Path(code_dir) if code_dir else cwd
    # tree_loc scans the whole code dir to size the codebase (NCPI denominator).
    # Opt-in (scan_code_dir): large repos (e.g. Rust `target/`, vendored deps)
    # can take minutes and freeze the UI, so it stays off by default. no_loc
    # suppresses it too (it skips all LOC, session-level included).
    loc_total = (
        loc.tree_loc(code_path)
        if scan_code_dir and not no_loc and code_path and _is_project_dir(code_path)
        else None
    )
    ctx = _MetricCtx(
        loc_total=loc_total,
        task_type=task_type,
        baseline_tcer=baseline_tcer,
        baseline_ncpi=baseline_ncpi,
        baseline_cpe=baseline_cpe,
        auto_infer=auto_infer,
    )

    # Second pass: merge usage + LOC per group, build one report per session.
    reports: list[SessionReport] = []
    totals = _empty_loc_totals()
    total_subs = 0
    agg_u = TokenUsage()
    for key, gfiles in groups.items():
        if cancel_check:
            cancel_check()
        sess_cwd = metas[key].cwd
        gu = reduce(
            lambda a, b: a.merge(b),
            (_scan_of(f, cwd=sess_cwd)[0] for f in gfiles),
            TokenUsage(),
        )
        n_sub = sum(1 for f in gfiles if reader.is_subagent(f))
        total_subs += n_sub
        agg_u = agg_u.merge(gu)
        if no_loc:
            reports.append(_mk_report(
                metas[key], gu, None, None, None, ctx=ctx,
                n_sub=n_sub, unseen=0, set_subagent_density=True,
            ))
            continue
        slocs = []
        for f in gfiles:
            _, sl = _scan_of(f, cwd=sess_cwd)
            if sl is not None:
                slocs.append(sl)
        merged_sloc = loc.merge_session_locs(slocs)
        _accumulate_sloc_totals(merged_sloc, totals)
        reports.append(_mk_report(
            metas[key], gu, merged_sloc.added - merged_sloc.deleted,
            merged_sloc.added, merged_sloc.deleted, ctx=ctx,
            n_sub=n_sub, unseen=merged_sloc.unseen_writes, sloc=merged_sloc,
            set_subagent_density=True,
        ))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else None,
        title=None, path=proj, is_subagent=False,
    )
    # Aggregate task type: majority of per-session inferences when auto.
    agg_tt = (
        metrics.majority_task_type([r.task_type for r in reports])
        if auto_infer and reports else None
    )
    if no_loc:
        agg = _mk_report(
            agg_meta, agg_u, None, None, None, ctx=ctx,
            n_sub=total_subs, unseen=0, set_subagent_density=True,
            task_type_override=agg_tt,
        )
    else:
        agg_sloc = _totals_to_sloc(totals)
        agg = _mk_report(
            agg_meta, agg_u, agg_sloc.added - agg_sloc.deleted,
            agg_sloc.added, agg_sloc.deleted, ctx=ctx,
            n_sub=total_subs, unseen=agg_sloc.unseen_writes, sloc=agg_sloc,
            set_subagent_density=True,
            task_type_override=agg_tt,
        )
    _suppress_aggregate_session_metrics(agg)

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
    scan_code_dir: bool = False,
    task_type: str = metrics.DEFAULT_TASK_TYPE,
    baseline_tcer: float | None = None,
    baseline_ncpi: float | None = None,
    baseline_cpe: float | None = None,
    since: str | None = None,
    until: str | None = None,
    cancel_check: Callable[[], None] | None = None,
    auto_infer: bool = False,
) -> ProjectAnalysis:
    """Analyze a Codex cwd-grouped project."""
    # Def-time defaults would freeze pre-save_baselines() values; resolve lazily
    # so a GUI "保存个人基准" takes effect without restarting (see metrics._refresh_composite_globals).
    if baseline_tcer is None:
        baseline_tcer = metrics.TCER_BASELINE
    if baseline_ncpi is None:
        baseline_ncpi = metrics.NCPI_BASELINE
    if baseline_cpe is None:
        baseline_cpe = metrics.CPE_BASELINE
    ref = project if isinstance(project, ProjectRef) else codex_reader.resolve_project(project)
    if ref is None:
        raise FileNotFoundError(f"codex project '{project}' not found under ~/.codex/sessions")
    files = codex_reader.sessions_for_project(ref)
    if not files:
        raise FileNotFoundError(f"no Codex session files for '{ref.display_name}'")

    from tcer.core import file_cache

    usage_memo: dict[Path, TokenUsage] = {}

    def _usage_of(f: Path) -> TokenUsage:
        u = usage_memo.get(f)
        if u is None:
            if cancel_check:
                cancel_check()
            # Process-level mtime cache; skip when cancellable (partial walk risk).
            if cancel_check is None:
                u = file_cache.get_or_compute(
                    f, ("codex_usage",),
                    lambda: codex_reader.aggregate_usage(f),
                )
            else:
                u = codex_reader.aggregate_usage(f)
            usage_memo[f] = u
        return u

    files = _filter_by_started_at(files, _usage_of, since, until)

    if session:
        files = [
            f for f in files
            if session in (codex_reader.read_session_meta(f).session_id or f.stem)
        ]
        if not files:
            raise FileNotFoundError(f"no Codex session matches '{session}'")

    code_path = Path(code_dir) if code_dir else (Path(ref.cwd) if ref.cwd else None)
    loc_total = (
        loc.tree_loc(code_path)
        if scan_code_dir and not no_loc and code_path and _is_project_dir(code_path)
        else None
    )
    ctx = _MetricCtx(
        loc_total=loc_total, task_type=task_type,
        baseline_tcer=baseline_tcer, baseline_ncpi=baseline_ncpi,
        baseline_cpe=baseline_cpe, auto_infer=auto_infer,
    )

    reports: list[SessionReport] = []
    agg_u = TokenUsage()
    totals = _empty_loc_totals()

    for f in files:
        if cancel_check:
            cancel_check()
        meta = codex_reader.read_session_meta(f)
        u = _usage_of(f)
        agg_u = agg_u.merge(u)
        if no_loc:
            reports.append(_mk_report(meta, u, None, None, None, ctx=ctx))
            continue
        # Single scan yields both the LOC and whether any patch existed.
        if cancel_check is None:
            sloc, has_signal = file_cache.get_or_compute(
                f, ("codex_loc",),
                lambda p=f: codex_reader._loc_scan(p),
            )
        else:
            sloc, has_signal = codex_reader._loc_scan(f)
        if not has_signal:
            # No parseable apply_patch → known zero LOC (not unknown). Keeps
            # project aggregate TCER valid when sibling sessions have patches.
            sloc = loc.SessionLoc(added=0, deleted=0)
        _accumulate_sloc_totals(sloc, totals)
        reports.append(_mk_report(
            meta, u, sloc.added - sloc.deleted, sloc.added, sloc.deleted,
            ctx=ctx, sloc=sloc, unseen=sloc.unseen_writes,
        ))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else ref.cwd,
        title=None, path=ref.path or (files[0].parent if files else Path(".")),
        is_subagent=False, entrypoint="codex", source="codex",
    )
    agg_tt = (
        metrics.majority_task_type([r.task_type for r in reports])
        if auto_infer and reports else None
    )
    if no_loc:
        agg = _mk_report(
            agg_meta, agg_u, None, None, None, ctx=ctx,
            task_type_override=agg_tt,
        )
    else:
        agg_sloc = _totals_to_sloc(totals)
        agg = _mk_report(
            agg_meta, agg_u, agg_sloc.added - agg_sloc.deleted,
            agg_sloc.added, agg_sloc.deleted, ctx=ctx, sloc=agg_sloc,
            unseen=agg_sloc.unseen_writes,
            task_type_override=agg_tt,
        )
    _suppress_aggregate_session_metrics(agg)

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
    scan_code_dir: bool = False,
    task_type: str = metrics.DEFAULT_TASK_TYPE,
    baseline_tcer: float | None = None,
    baseline_ncpi: float | None = None,
    baseline_cpe: float | None = None,
    since: str | None = None,
    until: str | None = None,
    cancel_check: Callable[[], None] | None = None,
    auto_infer: bool = False,
) -> ProjectAnalysis:
    """Analyze an OpenCode project from its local SQLite database."""
    # Def-time defaults would freeze pre-save_baselines() values; resolve lazily
    # so a GUI "保存个人基准" takes effect without restarting (see metrics._refresh_composite_globals).
    if baseline_tcer is None:
        baseline_tcer = metrics.TCER_BASELINE
    if baseline_ncpi is None:
        baseline_ncpi = metrics.NCPI_BASELINE
    if baseline_cpe is None:
        baseline_cpe = metrics.CPE_BASELINE
    ref = project if isinstance(project, ProjectRef) else opencode_reader.resolve_project(project)
    if ref is None or ref.path is None:
        raise FileNotFoundError(f"opencode project '{project}' not found under ~/.local/share/opencode")
    session_ids = opencode_reader.sessions_for_project(ref)
    if not session_ids:
        raise FileNotFoundError(f"no OpenCode sessions for '{ref.display_name}'")

    db_path = ref.path
    usage_memo: dict[str, TokenUsage] = {}

    def _usage_of(sid: str) -> TokenUsage:
        u = usage_memo.get(sid)
        if u is None:
            if cancel_check:
                cancel_check()
            u = opencode_reader.aggregate_usage(db_path, sid)
            usage_memo[sid] = u
        return u

    since_ms = _parse_date_to_ms(since) if since else None
    until_ms = _parse_date_to_ms(until, end_of_day=True) if until else None
    if since_ms or until_ms:
        filtered = []
        for sid in session_ids:
            u = _usage_of(sid)
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
    loc_total = (
        loc.tree_loc(code_path)
        if scan_code_dir and not no_loc and code_path and _is_project_dir(code_path)
        else None
    )
    ctx = _MetricCtx(
        loc_total=loc_total, task_type=task_type,
        baseline_tcer=baseline_tcer, baseline_ncpi=baseline_ncpi,
        baseline_cpe=baseline_cpe, auto_infer=auto_infer,
    )

    reports: list[SessionReport] = []
    agg_u = TokenUsage()
    totals = _empty_loc_totals()

    for sid in session_ids:
        if cancel_check:
            cancel_check()
        meta = opencode_reader.read_session_meta(db_path, sid)
        u = _usage_of(sid)
        agg_u = agg_u.merge(u)
        if no_loc:
            reports.append(_mk_report(meta, u, None, None, None, ctx=ctx))
            continue
        # One SQLite pass for signal + LOC (same pattern as Codex/Grok).
        sloc, has_signal = opencode_reader._loc_scan(db_path, sid)
        if not has_signal:
            # No summary and no edit tools → known zero (not unknown).
            sloc = loc.SessionLoc(added=0, deleted=0)
        _accumulate_sloc_totals(sloc, totals)
        reports.append(_mk_report(
            meta, u, sloc.added - sloc.deleted, sloc.added, sloc.deleted,
            ctx=ctx, sloc=sloc, unseen=sloc.unseen_writes,
        ))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else ref.cwd,
        title=None, path=db_path, is_subagent=False, entrypoint="opencode", source="opencode",
    )
    agg_tt = (
        metrics.majority_task_type([r.task_type for r in reports])
        if auto_infer and reports else None
    )
    if no_loc:
        agg = _mk_report(
            agg_meta, agg_u, None, None, None, ctx=ctx,
            task_type_override=agg_tt,
        )
    else:
        agg_sloc = _totals_to_sloc(totals)
        agg = _mk_report(
            agg_meta, agg_u, agg_sloc.added - agg_sloc.deleted,
            agg_sloc.added, agg_sloc.deleted, ctx=ctx, sloc=agg_sloc,
            unseen=agg_sloc.unseen_writes,
            task_type_override=agg_tt,
        )
    _suppress_aggregate_session_metrics(agg)

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


def _analyze_grok_project(
    project: str | ProjectRef,
    *,
    session: str | None = None,
    code_dir: str | Path | None = None,
    no_loc: bool = False,
    scan_code_dir: bool = False,
    task_type: str = metrics.DEFAULT_TASK_TYPE,
    baseline_tcer: float | None = None,
    baseline_ncpi: float | None = None,
    baseline_cpe: float | None = None,
    since: str | None = None,
    until: str | None = None,
    cancel_check: Callable[[], None] | None = None,
    auto_infer: bool = False,
) -> ProjectAnalysis:
    """Analyze a Grok cwd-grouped project."""
    # Def-time defaults would freeze pre-save_baselines() values; resolve lazily
    # so a GUI "保存个人基准" takes effect without restarting (see metrics._refresh_composite_globals).
    if baseline_tcer is None:
        baseline_tcer = metrics.TCER_BASELINE
    if baseline_ncpi is None:
        baseline_ncpi = metrics.NCPI_BASELINE
    if baseline_cpe is None:
        baseline_cpe = metrics.CPE_BASELINE
    ref = project if isinstance(project, ProjectRef) else grok_reader.resolve_project(project)
    if ref is None:
        raise FileNotFoundError(f"grok project '{project}' not found under ~/.grok/sessions")
    files = grok_reader.sessions_for_project(ref)
    if not files:
        raise FileNotFoundError(f"no Grok session files for '{ref.display_name}'")

    from tcer.core import file_cache

    usage_memo: dict[Path, TokenUsage] = {}

    def _usage_of(f: Path) -> TokenUsage:
        u = usage_memo.get(f)
        if u is None:
            if cancel_check:
                cancel_check()
            if cancel_check is None:
                u = file_cache.get_or_compute(
                    f, ("grok_usage",),
                    lambda: grok_reader.aggregate_usage(f),
                )
            else:
                u = grok_reader.aggregate_usage(f)
            usage_memo[f] = u
        return u

    files = _filter_by_started_at(files, _usage_of, since, until)

    if session:
        files = [
            f for f in files
            if session in (grok_reader.read_session_meta(f).session_id or f.stem)
        ]
        if not files:
            raise FileNotFoundError(f"no Grok session matches '{session}'")

    code_path = Path(code_dir) if code_dir else (Path(ref.cwd) if ref.cwd else None)
    loc_total = (
        loc.tree_loc(code_path)
        if scan_code_dir and not no_loc and code_path and _is_project_dir(code_path)
        else None
    )
    ctx = _MetricCtx(
        loc_total=loc_total, task_type=task_type,
        baseline_tcer=baseline_tcer, baseline_ncpi=baseline_ncpi,
        baseline_cpe=baseline_cpe, auto_infer=auto_infer,
    )

    reports: list[SessionReport] = []
    agg_u = TokenUsage()
    totals = _empty_loc_totals()

    for f in files:
        if cancel_check:
            cancel_check()
        meta = grok_reader.read_session_meta(f)
        u = _usage_of(f)
        agg_u = agg_u.merge(u)
        if no_loc:
            reports.append(_mk_report(meta, u, None, None, None, ctx=ctx))
            continue
        sess_cwd = meta.cwd or ref.cwd
        if cancel_check is None:
            sloc, has_signal = file_cache.get_or_compute(
                f, ("grok_loc", str(sess_cwd or ""), False),
                lambda p=f, c=sess_cwd: grok_reader._loc_scan(p, cwd=c, disk_prior=False),
            )
        else:
            sloc, has_signal = grok_reader._loc_scan(f, cwd=sess_cwd, disk_prior=False)
        if not has_signal:
            # No search_replace/write → known zero LOC (not unknown).
            sloc = loc.SessionLoc(added=0, deleted=0)
        _accumulate_sloc_totals(sloc, totals)
        reports.append(_mk_report(
            meta, u, sloc.added - sloc.deleted, sloc.added, sloc.deleted,
            ctx=ctx, sloc=sloc, unseen=sloc.unseen_writes,
        ))

    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else ref.cwd,
        title=None, path=ref.path or (files[0].parent if files else Path(".")),
        is_subagent=False, entrypoint="grok", source="grok",
    )
    agg_tt = (
        metrics.majority_task_type([r.task_type for r in reports])
        if auto_infer and reports else None
    )
    if no_loc:
        agg = _mk_report(
            agg_meta, agg_u, None, None, None, ctx=ctx,
            task_type_override=agg_tt,
        )
    else:
        agg_sloc = _totals_to_sloc(totals)
        agg = _mk_report(
            agg_meta, agg_u, agg_sloc.added - agg_sloc.deleted,
            agg_sloc.added, agg_sloc.deleted, ctx=ctx, sloc=agg_sloc,
            unseen=agg_sloc.unseen_writes,
            task_type_override=agg_tt,
        )
    _suppress_aggregate_session_metrics(agg)

    return ProjectAnalysis(
        project_hash=ref.key,
        reports=reports,
        aggregate=agg,
        code_dir=code_path,
        n_sessions=len(reports),
        n_subagents=0,
        source="grok",
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
