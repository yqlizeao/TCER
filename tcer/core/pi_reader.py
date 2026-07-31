"""Pi (upstream ``earendil-works/pi``) local-session reader.

Pi is the parent project of the omp fork (``can1357/oh-my-pi``). It stores
local sessions as JSONL under
``~/.pi/agent/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl`` — the **same event
schema** as omp (``session`` / ``message`` / ``model_change`` / … lines;
``message.role`` ∈ ``user`` / ``assistant`` / ``toolResult``), so this module
reuses :mod:`tcer.core.omp_reader`'s entire parsing layer verbatim and only
overrides the source/directory-facing entry points: Pi sessions are discovered
under ``~/.pi`` and tagged ``source="pi"``.

Two Pi enhancements over omp — ``reasoning`` and ``cacheWrite1h`` in the
assistant ``usage`` block — are picked up by the shared
``omp_reader._add_turn_usage`` (extended to read both; omp data has neither
field, so omp is unaffected).

Differences from omp absorbed without code here:

  * Pi has no 256-byte ``type:"title"`` slot — the first line is the
    ``type:"session"`` header directly; ``_session_line`` finds it regardless.
  * Pi assistant messages carry no ``contextSnapshot`` / ``duration`` / ``ttft``
    (those are omp-fork additions), so ``peak_input_tokens`` stays 0 and TTFT is
    unavailable — mirrored in ``metric_defs._SOURCE_SUPPORT`` where ``pi`` is
    deliberately absent from ``ttft`` / ``ttft_p95``.

Subagent folding assumes Pi nests transcripts under ``<stem>/`` like omp; if
Pi's layout differs, ``_is_subagent_file`` simply returns False and subagents
are counted as main sessions (no crash; surfaces via audit token totals).

Read-only; does not touch Pi's ``agent.db`` etc.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from tcer.core import omp_reader
from tcer.core.models import ProjectRef, SessionMeta
from tcer.core.paths import encode_hash, pi_sessions_dir

# -- 解析层:与 omp 同族 JSONL,逐函数复用(不重写) --------------------------- #
iter_events = omp_reader.iter_events
aggregate_usage = omp_reader.aggregate_usage
_loc_scan = omp_reader._loc_scan
session_loc_full = omp_reader.session_loc_full
has_loc_signal = omp_reader.has_loc_signal
read_user_messages = omp_reader.read_user_messages
read_conversation = omp_reader.read_conversation
_subagent_files = omp_reader._subagent_files
_classify_omp_tool = omp_reader._classify_omp_tool
# Pi 工具名为 omp 子集(同源上游);未知名 pass-through(omp 既定),不丢数据。
_PI_TOOL_MAP = omp_reader._OMP_TOOL_MAP

_NO_CWD_KEY = "__pi_no_cwd__"
_NO_CWD_LABEL = "Pi 无工作目录"


def discover_sessions() -> list[Path]:
    """Recursively collect Pi session JSONL files (one per session)."""
    base = pi_sessions_dir()
    if not base.is_dir():
        return []
    return sorted(base.rglob("*.jsonl"))


def list_project_refs() -> list[ProjectRef]:
    """Group Pi sessions by cwd for the unified project list."""
    groups: dict[str, list[Path]] = {}
    cwd_by_key: dict[str, str | None] = {}
    for p in discover_sessions():
        cwd = omp_reader._normalize_cwd(omp_reader._session_line(p).get("cwd"))
        key = encode_hash(cwd) if cwd else _NO_CWD_KEY
        groups.setdefault(key, []).append(p)
        cwd_by_key.setdefault(key, cwd)

    refs: list[ProjectRef] = []
    for key, paths in groups.items():
        cwd = cwd_by_key.get(key)
        refs.append(ProjectRef(
            source="pi",
            key=key,
            display_name=_display_name_for_cwd(cwd),
            cwd=cwd,
            path=Path(cwd) if cwd else None,
            session_paths=tuple(sorted(
                p for p in paths if not omp_reader._is_subagent_file(p))),
        ))
    return refs


def resolve_project(project: str) -> ProjectRef | None:
    """Resolve a Pi project key/display substring to a project ref."""
    refs = list_project_refs()
    for ref in refs:
        if ref.key == project:
            return ref
    needle = project.lower()
    matches = [
        r for r in refs
        if needle in r.key.lower()
        or needle in r.display_name.lower()
        or (r.cwd and needle in r.cwd.lower())
    ]
    return matches[0] if len(matches) == 1 else None


def sessions_for_project(project: str | ProjectRef) -> list[Path]:
    """Return the Pi *main* session files for a project ref or key."""
    if isinstance(project, ProjectRef):
        paths = list(project.session_paths)
    else:
        ref = resolve_project(project)
        paths = list(ref.session_paths) if ref else []
    return sorted(p for p in paths if not omp_reader._is_subagent_file(p))


def read_session_meta(path: Path) -> SessionMeta:
    """omp 的元数据解析逻辑,仅把 tag 改为 ``source``/``entrypoint`` ="pi"。"""
    return dataclasses.replace(
        omp_reader.read_session_meta(path), entrypoint="pi", source="pi")


def _display_name_for_cwd(cwd: str | None) -> str:
    if not cwd:
        return _NO_CWD_LABEL
    return Path(cwd).name or cwd
