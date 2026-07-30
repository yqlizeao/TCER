"""Project/session analysis orchestration shared by the CLI and the GUI.

Reads a project's session JSONL, **folds each subagent into its parent session**
(so one session = one main file + its subagents, matching how cc-switch counts
sessions), aggregates token usage, derives git-free LOC from file-mutating tool
calls, scans the working directory for accumulated codebase size, and computes
per-session + aggregate reports. The CLI and Tkinter GUI both call ``analyze_project``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import Callable

from tcer.core import codex_reader, grok_reader, loc, metrics, omp_reader, opencode_reader, reader
from tcer.core.models import ProjectRef, SessionMeta, SessionReport, TokenUsage
from tcer.core.paths import ref_root, resolve_project


class AnalysisCancelled(Exception):
    """Raised when a cooperative cancel check fires mid-analysis."""


@dataclass
class ProjectAnalysis:
    project_hash: str
    reports: list[SessionReport]  # one per real session (subagents folded in)
    aggregate: SessionReport
    code_dir: Path | None  # 项目工作目录（仅展示用途；TCER 不读取真实仓库）
    n_sessions: int  # number of real sessions (not counting subagents separately)
    n_subagents: int  # total subagent files folded into the sessions above
    source: str = "claude"
    project_ref: ProjectRef | None = None


@dataclass(frozen=True)
class _MetricCtx:
    """Shared knobs for per-session ``metrics.compute`` across sources."""

    task_type: str
    baseline_tcer: float
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
    """Keep files whose activity window [started_at, ended_at] overlaps [since, until].

    A session that started before ``since`` but was still active within the window
    counts (active boundary = ``ended_at``, falling back to ``started_at``). This
    matches the "今天有活动" intuition and stays consistent with the project-list
    mtime filter (last-write ≈ ended_at), so a project shown in the left column
    always has sessions to show when opened.
    """
    since_ms = _parse_date_to_ms(since) if since else None
    until_ms = _parse_date_to_ms(until, end_of_day=True) if until else None
    if not since_ms and not until_ms:
        return files
    filtered: list[Path] = []
    for f in files:
        u = usage_of(f)
        if u.started_at is None:
            continue
        # 跨天会话只要在窗口内还活跃就算命中：下界用 ended_at（缺则降级 started_at）。
        active_until = u.ended_at or u.started_at
        if since_ms and active_until < since_ms:
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
        tt = metrics.infer_task_type_from_usage(
            u, net_loc=net, test_net_loc=test_net, doc_net_loc=doc_net)
    else:
        tt = ctx.task_type

    rep = metrics.compute(
        meta, u, net,
        task_type=tt,
        code_added=added,
        code_deleted=deleted,
        code_reworked=reworked,
        high_churn_files=high_churn,
        test_net_loc=test_net,
        doc_net_loc=doc_net,
        tcer_baseline=ctx.baseline_tcer,
        cpe_baseline=ctx.baseline_cpe,
    )
    rep.subagent_count = n_sub
    rep.unseen_writes = unseen
    if sloc:
        details = {fp: cnt for fp, cnt in sloc.file_edit_counts.items() if cnt >= 3}
        if details:
            rep.high_churn_details = dict(sorted(details.items(), key=lambda x: -x[1]))
        # 一次写对率：只编辑 1 次的文件占比——比「高返工文件数」更正面的质量信号。
        counts = sloc.file_edit_counts
        if counts:
            rep.first_pass_file_ratio = (
                sum(1 for c in counts.values() if c == 1) / len(counts))
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
    no_loc: bool = False,
    task_type: str = metrics.DEFAULT_TASK_TYPE,
    baseline_tcer: float | None = None,
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
    if baseline_cpe is None:
        baseline_cpe = metrics.CPE_BASELINE
    auto_infer = metrics.is_auto_task_type(task_type)
    if not auto_infer:
        task_type = metrics.resolve_task_type(task_type)
    else:
        task_type = metrics.DEFAULT_TASK_TYPE  # placeholder; per-session inference wins
    cancel_check = _make_cancel_check(cancel_event)

    for adapter in _ADAPTERS:
        if source == adapter.source or (project_ref and project_ref.source == adapter.source):
            return _analyze_source_project(
                adapter,
                project_ref or project,
                session=session,
                no_loc=no_loc,
                task_type=task_type,
                baseline_tcer=baseline_tcer,
                baseline_cpe=baseline_cpe,
                since=since,
                until=until,
                cancel_check=cancel_check,
                auto_infer=auto_infer,
            )

    if project_ref is not None and project_ref.source == "claude":
        # 跨根独立成条：按 ref 所属根限分会话（跳过 resolve_project，避免选错根）。
        proj = project_ref.path
        root = ref_root(project_ref)
        files = reader.discover_jsonl(
            project_ref.key, roots=[root] if root is not None else None)
        if proj is None:
            proj = resolve_project(project)
    else:
        proj = resolve_project(project)
        if proj is None:
            raise FileNotFoundError(f"project '{project}' not found under ~/.claude/projects")
        files = reader.discover_jsonl(proj.name)  # CLI 裸 hash：跨根 union（兼容）
    if not files:
        raise FileNotFoundError(f"no session files in {proj}")
    if no_subagents:
        files = [f for f in files if not reader.is_subagent(f)]

    # Per-call memo (also backed by process-level mtime cache in scan_session).
    # User message bodies are omitted here — popup uses reader.read_user_messages.
    scan_memo: dict[Path, tuple[TokenUsage, loc.SessionLoc | None]] = {}

    def _scan_of(f: Path) -> tuple[TokenUsage, loc.SessionLoc | None]:
        hit = scan_memo.get(f)
        if hit is None:
            if cancel_check:
                cancel_check()
            hit = reader.scan_session(
                f,
                with_loc=not no_loc,
                include_user_texts=False,
                cancel_check=cancel_check,
            )
            scan_memo[f] = hit
        return hit

    # Group files by parent session id (subagents fold into the owning session).
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(reader.parent_session_id(f), []).append(f)

    # First pass: per-group metadata (cheap head/tail read) to discover the
    # project cwd for display.
    metas: dict[str, SessionMeta] = {}
    for key, gfiles in groups.items():
        if cancel_check:
            cancel_check()
        main = next((f for f in gfiles if not reader.is_subagent(f)), None)
        metas[key] = reader.read_session_meta(main) if main else _synth_meta(key, gfiles[0])

    def _usage_of(f: Path) -> TokenUsage:
        return _scan_of(f)[0]

    files = _filter_by_started_at(files, _usage_of, since, until)
    if since or until:
        groups = {}
        for f in files:
            groups.setdefault(reader.parent_session_id(f), []).append(f)

    if session:
        groups = {k: v for k, v in groups.items() if session in k}
        if not groups:
            raise FileNotFoundError(f"no session matches '{session}'")

    cwd: Path | None = None
    for key in groups:
        if metas[key].cwd:
            cwd = Path(metas[key].cwd)
            break

    code_path = cwd  # 项目工作目录（仅展示；产品定位：不扫描真实仓库）
    ctx = _MetricCtx(
        task_type=task_type,
        baseline_tcer=baseline_tcer,
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
        gu = reduce(
            lambda a, b: a.merge(b),
            (_scan_of(f)[0] for f in gfiles),
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
            _, sl = _scan_of(f)
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


# --------------------------------------------------------------------------- #
# Non-Claude sources: one shared skeleton + per-source adapters
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _SourceAdapter:
    """Per-source hooks for the shared non-Claude analyze pipeline.

    Claude 保持独立路径（子代理折叠 + cwd-keyed 扫描）；Codex / OpenCode / Grok
    共享同一条「逐会话 → 聚合」流水线，源差异全部收敛到这些钩子。``handle``
    是源自定义的会话句柄：Codex/Grok 为 updates/rollout 文件 Path，OpenCode 为
    session id 字符串（legacy 会话为 JSON 路径字符串）。file_cache 的 key 构造
    在各 usage_of / loc_of 钩子内部（每源的失效语义不同，如 Grok 要并入
    signals/events 旁路文件签名）。
    """

    source: str
    entrypoint: str
    resolve: Callable[[str], ProjectRef | None]
    sessions: Callable[[ProjectRef], list]
    read_meta: Callable[[ProjectRef, object], SessionMeta]
    usage_of: Callable[[ProjectRef, object], TokenUsage]
    loc_of: Callable[[ProjectRef, object, SessionMeta], tuple]
    session_key: Callable[[ProjectRef, object], str]
    not_found: str      # .format(project=...)
    no_sessions: str    # .format(name=ref.display_name)
    no_match: str       # .format(session=...)
    requires_path: bool = False  # OpenCode: ref.path (SQLite db) 必须存在
    subagents_of: Callable[[ProjectRef, object], int] | None = None  # omp: folded subagent count


def _analyze_source_project(
    adapter: _SourceAdapter,
    project: str | ProjectRef,
    *,
    session: str | None = None,
    no_loc: bool = False,
    task_type: str = metrics.DEFAULT_TASK_TYPE,
    baseline_tcer: float | None = None,
    baseline_cpe: float | None = None,
    since: str | None = None,
    until: str | None = None,
    cancel_check: Callable[[], None] | None = None,
    auto_infer: bool = False,
) -> ProjectAnalysis:
    """Shared per-session pipeline for Codex / OpenCode / Grok / omp projects."""
    # Def-time defaults would freeze pre-save_baselines() values; resolve lazily
    # so a GUI "保存个人基准" takes effect without restarting (see metrics._refresh_composite_globals).
    if baseline_tcer is None:
        baseline_tcer = metrics.TCER_BASELINE
    if baseline_cpe is None:
        baseline_cpe = metrics.CPE_BASELINE
    ref = project if isinstance(project, ProjectRef) else adapter.resolve(project)
    if ref is None or (adapter.requires_path and ref.path is None):
        raise FileNotFoundError(adapter.not_found.format(project=project))
    handles = adapter.sessions(ref)
    if not handles:
        raise FileNotFoundError(adapter.no_sessions.format(name=ref.display_name))

    usage_memo: dict = {}

    def _usage_of(h) -> TokenUsage:
        u = usage_memo.get(h)
        if u is None:
            if cancel_check:
                cancel_check()
            u = adapter.usage_of(ref, h)
            usage_memo[h] = u
        return u

    handles = _filter_by_started_at(handles, _usage_of, since, until)

    if session:
        handles = [h for h in handles if session in adapter.session_key(ref, h)]
        if not handles:
            raise FileNotFoundError(adapter.no_match.format(session=session))

    code_path = Path(ref.cwd) if ref.cwd else None  # 仅展示；不扫描真实仓库
    ctx = _MetricCtx(
        task_type=task_type,
        baseline_tcer=baseline_tcer, baseline_cpe=baseline_cpe,
        auto_infer=auto_infer,
    )

    reports: list[SessionReport] = []
    agg_u = TokenUsage()
    totals = _empty_loc_totals()

    for h in handles:
        if cancel_check:
            cancel_check()
        meta = adapter.read_meta(ref, h)
        u = _usage_of(h)
        agg_u = agg_u.merge(u)
        if no_loc:
            reports.append(_mk_report(meta, u, None, None, None, ctx=ctx))
            continue
        # Single scan yields the LOC and whether any edit signal existed.
        sloc, has_signal = adapter.loc_of(ref, h, meta)
        if not has_signal:
            # No parseable edit signal → known zero LOC (not unknown). Keeps
            # project aggregate TCER valid when sibling sessions have signal.
            sloc = loc.SessionLoc(added=0, deleted=0)
        _accumulate_sloc_totals(sloc, totals)
        reports.append(_mk_report(
            meta, u, sloc.added - sloc.deleted, sloc.added, sloc.deleted,
            ctx=ctx, sloc=sloc, unseen=sloc.unseen_writes,
        ))

    first = handles[0] if handles else None
    agg_meta = SessionMeta(
        session_id="(aggregate)", cwd=str(code_path) if code_path else ref.cwd,
        title=None,
        path=ref.path or (first.parent if isinstance(first, Path) else Path(".")),
        is_subagent=False, entrypoint=adapter.entrypoint, source=adapter.source,
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

    n_sub_total = (
        sum(adapter.subagents_of(ref, h) for h in handles)
        if adapter.subagents_of else 0
    )
    return ProjectAnalysis(
        project_hash=ref.key,
        reports=reports,
        aggregate=agg,
        code_dir=code_path,
        n_sessions=len(reports),
        n_subagents=n_sub_total,
        source=adapter.source,
        project_ref=ref,
    )


# ---- Codex hooks ----------------------------------------------------------- #
def _codex_usage_of(ref: ProjectRef, f: Path) -> TokenUsage:
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        f, ("codex_usage",), lambda: codex_reader.aggregate_usage(f))


def _codex_loc_of(ref: ProjectRef, f: Path, meta: SessionMeta):
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        f, ("codex_loc",), lambda: codex_reader._loc_scan(f))


_CODEX = _SourceAdapter(
    source="codex", entrypoint="codex",
    resolve=codex_reader.resolve_project,
    sessions=codex_reader.sessions_for_project,
    read_meta=lambda ref, f: codex_reader.read_session_meta(f),
    usage_of=_codex_usage_of,
    loc_of=_codex_loc_of,
    session_key=lambda ref, f: (codex_reader.read_session_meta(f).session_id or f.stem),
    not_found="codex project '{project}' not found under ~/.codex/sessions",
    no_sessions="no Codex session files for '{name}'",
    no_match="no Codex session matches '{session}'",
)


# ---- OpenCode hooks -------------------------------------------------------- #
def _opencode_cache_file(ref: ProjectRef, sid: str) -> Path:
    # Legacy sessions live in their own JSON file; key the mtime cache on it.
    # SQLite sessions key on the db file (coarse: any write invalidates all).
    return Path(sid) if opencode_reader._is_legacy_session(sid) else ref.path


def _opencode_usage_of(ref: ProjectRef, sid: str) -> TokenUsage:
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        _opencode_cache_file(ref, sid), ("opencode_usage", sid),
        lambda: opencode_reader.aggregate_usage(ref.path, sid))


def _opencode_loc_of(ref: ProjectRef, sid: str, meta: SessionMeta):
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        _opencode_cache_file(ref, sid), ("opencode_loc", sid),
        lambda: opencode_reader._loc_scan(ref.path, sid))


_OPENCODE = _SourceAdapter(
    source="opencode", entrypoint="opencode",
    resolve=opencode_reader.resolve_project,
    sessions=opencode_reader.sessions_for_project,
    read_meta=lambda ref, sid: opencode_reader.read_session_meta(ref.path, sid),
    usage_of=_opencode_usage_of,
    loc_of=_opencode_loc_of,
    session_key=lambda ref, sid: sid,
    not_found="opencode project '{project}' not found under ~/.local/share/opencode",
    no_sessions="no OpenCode sessions for '{name}'",
    no_match="no OpenCode session matches '{session}'",
    requires_path=True,
)


# ---- Grok hooks ------------------------------------------------------------ #
def _grok_side_files_key(f: Path) -> tuple:
    """signals.json / events.jsonl 的签名并入缓存 key。

    这两个文件是会话结束后补写/更新的，而 file_cache 的主签名只看
    updates.jsonl —— 不并入会导致取消数/评价/回退行等信号停留在旧值。
    """
    parts = []
    for name in ("signals.json", "events.jsonl"):
        try:
            st = (f.parent / name).stat()
            parts.append((name, int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            parts.append((name, 0, 0))
    return tuple(parts)


def _grok_usage_of(ref: ProjectRef, f: Path) -> TokenUsage:
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        f, ("grok_usage", _grok_side_files_key(f)),
        lambda: grok_reader.aggregate_usage(f))


def _grok_loc_of(ref: ProjectRef, f: Path, meta: SessionMeta):
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        f, ("grok_loc",), lambda: grok_reader._loc_scan(f))


_GROK = _SourceAdapter(
    source="grok", entrypoint="grok",
    resolve=grok_reader.resolve_project,
    sessions=grok_reader.sessions_for_project,
    read_meta=lambda ref, f: grok_reader.read_session_meta(f),
    usage_of=_grok_usage_of,
    loc_of=_grok_loc_of,
    session_key=lambda ref, f: (grok_reader.read_session_meta(f).session_id or f.stem),
    not_found="grok project '{project}' not found under ~/.grok/sessions",
    no_sessions="no Grok session files for '{name}'",
    no_match="no Grok session matches '{session}'",
)


# ---- omp hooks ------------------------------------------------------------- #
def _omp_subagent_key(f: Path) -> tuple:
    """Fold subagent JSONL signatures into the omp cache key.

    omp subagent transcripts live under ``<stem>/`` and are merged into the
    parent by ``aggregate_usage`` / ``_loc_scan``. The main file's mtime alone
    does not change when a subagent file is appended/updated, so without these
    signatures a later subagent write would surface stale merged usage/LOC
    (same reason Grok folds signals.json/events.jsonl into its key).
    """
    parts = []
    for sub in omp_reader._subagent_files(f):
        try:
            st = sub.stat()
            parts.append((sub.name, int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            parts.append((sub.name, 0, 0))
    return tuple(parts)


def _omp_usage_of(ref: ProjectRef, f: Path) -> TokenUsage:
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        f, ("omp_usage", _omp_subagent_key(f)), lambda: omp_reader.aggregate_usage(f))


def _omp_loc_of(ref: ProjectRef, f: Path, meta: SessionMeta):
    from tcer.core import file_cache
    return file_cache.get_or_compute(
        f, ("omp_loc", _omp_subagent_key(f)), lambda: omp_reader._loc_scan(f))


_OMP = _SourceAdapter(
    source="omp", entrypoint="omp",
    resolve=omp_reader.resolve_project,
    sessions=omp_reader.sessions_for_project,
    read_meta=lambda ref, f: omp_reader.read_session_meta(f),
    usage_of=_omp_usage_of,
    loc_of=_omp_loc_of,
    session_key=lambda ref, f: (omp_reader.read_session_meta(f).session_id or f.stem),
    not_found="omp project '{project}' not found under ~/.omp/agent/sessions",
    no_sessions="no omp session files for '{name}'",
    no_match="no omp session matches '{session}'",
    subagents_of=lambda ref, f: len(omp_reader._subagent_files(f)),
)

_ADAPTERS = (_CODEX, _OPENCODE, _GROK, _OMP)


def _synth_meta(session_id: str, sample: Path) -> SessionMeta:
    """Metadata for a session whose main file is missing (orphan subagents only)."""
    return SessionMeta(session_id=session_id, cwd=None, title=None,
                       path=sample, is_subagent=False)


def _parse_date_to_ms(date_str: str, end_of_day: bool = False) -> int:
    """Parse YYYY-MM-DD to ms timestamp (start or end of day, **local** timezone).

    Naive ``datetime.timestamp()`` is interpreted in the system local timezone,
    matching ``fmt_dt`` display and the FilterBar presets (``datetime.now()``) —
    so "今天" means local midnight, not UTC midnight. Raises ValueError on
    malformed input.
    """
    from datetime import datetime
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")  # naive → 本地时区
        if end_of_day:
            # End of day = 23:59:59.999999
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return int(dt.timestamp() * 1000)
    except ValueError as e:
        raise ValueError(f"Invalid date format '{date_str}' (expected YYYY-MM-DD)") from e
