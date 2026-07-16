"""OpenCode local-session reader.

OpenCode stores current local state in a SQLite database under its data
directory (usually ``~/.local/share/opencode/opencode.db``).  The schema is
defined in the upstream ``session/sql.ts`` module: project/session/message/part
tables carry project directories, aggregate token counters, and hydrated
conversation parts.  This reader maps those rows into TCER's standard data
classes without writing to the database.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

from tcer.core import pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp
from tcer.core.paths import opencode_dir
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_NO_PROJECT_KEY = "__opencode_no_project__"
_NO_PROJECT_LABEL = "OpenCode 未知项目"


def discover_databases() -> list[Path]:
    """Return candidate OpenCode SQLite databases."""
    out: list[Path] = []
    seen: set[Path] = set()
    for base in _data_dirs():
        if not base.is_dir():
            continue
        candidates = list(base.glob("opencode*.db"))
        candidates.extend(base.glob("*.sqlite"))
        candidates.extend(base.glob("*.sqlite3"))
        storage = base / "storage"
        if storage.is_dir():
            candidates.extend(storage.glob("*.db"))
            candidates.extend(storage.glob("*.sqlite"))
            candidates.extend(storage.glob("*.sqlite3"))
        for p in sorted(c for c in candidates if c.is_file()):
            rp = p.resolve()
            if rp not in seen and _has_tables(p, {"session"}):
                out.append(p)
                seen.add(rp)
    return out


def list_project_refs() -> list[ProjectRef]:
    """Group OpenCode sessions by real working directory.

    Prefer ``session.directory`` over ``project.worktree``: OpenCode often uses a
    single ``global`` project with ``worktree='/'``, while each chat has its own
    directory (e.g. ``C:/playground/langfuse``). Grouping only by project_id then
    collapses unrelated trees and shows cwd as ``/``.
    """
    refs: list[ProjectRef] = []
    for db in discover_databases():
        with _connect(db) as con:
            has_project = _has_table(con, "project")
            if has_project:
                rows = con.execute(
                    """
                    select
                      s.project_id,
                      s.directory as session_dir,
                      p.worktree as project_worktree,
                      p.name as project_name,
                      count(*) as n
                    from session s
                    left join project p on p.id = s.project_id
                    group by s.project_id, s.directory, p.worktree, p.name
                    order by s.directory
                    """
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    select
                      coalesce(project_id, directory, ?) as project_id,
                      directory as session_dir,
                      null as project_worktree,
                      null as project_name,
                      count(*) as n
                    from session
                    group by project_id, directory
                    order by directory
                    """,
                    (_NO_PROJECT_KEY,),
                ).fetchall()

        for row in rows:
            project_id = str(row["project_id"] or _NO_PROJECT_KEY)
            session_dir = row["session_dir"] if isinstance(row["session_dir"], str) else None
            worktree = row["project_worktree"] if isinstance(row["project_worktree"], str) else None
            cwd = _effective_cwd(session_dir, worktree)
            name = row["project_name"] if isinstance(row["project_name"], str) else None
            # Prefer path-based label when project name is empty/generic.
            label_src = name if (name and name.strip() and name.strip().lower() not in {"global", "default"}) else cwd
            key = _make_project_key(db.name, project_id, cwd)
            refs.append(ProjectRef(
                source="opencode",
                key=key,
                display_name=_display_name(label_src, cwd),
                cwd=cwd,
                path=db,
            ))
    refs.extend(_legacy_project_refs())
    return refs


def resolve_project(project: str) -> ProjectRef | None:
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


def sessions_for_project(project: str | ProjectRef) -> list[str]:
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    if ref is None or ref.path is None:
        return []
    if ref.session_paths:
        return [str(p) for p in ref.session_paths]
    project_id, key_cwd = _parse_project_key(ref.key)
    cwd = ref.cwd or key_cwd
    with _connect(ref.path) as con:
        rows = []
        # Prefer directory filter so global/project_id buckets don't mix trees.
        if cwd and not _is_useless_worktree(cwd):
            rows = con.execute(
                """
                select id from session
                where directory = ?
                order by time_created, id
                """,
                (cwd,),
            ).fetchall()
        if not rows and project_id and project_id != _NO_PROJECT_KEY:
            rows = con.execute(
                "select id from session where project_id = ? order by time_created, id",
                (project_id,),
            ).fetchall()
        if not rows and project_id == _NO_PROJECT_KEY:
            rows = con.execute(
                "select id from session where project_id is null order by time_created, id"
            ).fetchall()
    return [str(r["id"]) for r in rows]


def read_session_meta(db_path: Path, session_id: str) -> SessionMeta:
    if _is_legacy_session(session_id):
        return _legacy_session_meta(Path(session_id))
    with _connect(db_path) as con:
        if _has_table(con, "project"):
            row = con.execute(
                """
                select
                  s.*, p.worktree as project_worktree, p.name as project_name
                from session s
                left join project p on p.id = s.project_id
                where s.id = ?
                """,
                (session_id,),
            ).fetchone()
        else:
            row = con.execute(
                "select s.*, null as project_worktree, null as project_name from session s where s.id = ?",
                (session_id,),
            ).fetchone()
    if row is None:
        raise FileNotFoundError(f"OpenCode session '{session_id}' not found")

    model = _json_obj(row["model"])
    provider = _first_str(
        model.get("providerID") if isinstance(model, dict) else None,
        model.get("provider_id") if isinstance(model, dict) else None,
    )
    permission = _json_obj(row["permission"])
    title = row["title"] if isinstance(row["title"], str) and row["title"] else None
    cwd = _first_str(row["directory"], row["project_worktree"])
    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=db_path,
        is_subagent=bool(row["parent_id"]),
        entrypoint="opencode",
        source="opencode",
        model_provider=provider,
        approval_policy=_permission_label(permission),
    )


def aggregate_usage(db_path: Path, session_id: str) -> TokenUsage:
    """Aggregate one OpenCode session from SQLite rows."""
    if _is_legacy_session(session_id):
        return _legacy_usage(Path(session_id))
    u = TokenUsage()
    with _connect(db_path) as con:
        session = con.execute("select * from session where id = ?", (session_id,)).fetchone()
        if session is None:
            raise FileNotFoundError(f"OpenCode session '{session_id}' not found")
        messages = con.execute(
            "select * from message where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall() if _has_table(con, "message") else []
        parts = con.execute(
            "select * from part where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall() if _has_table(con, "part") else []

    u.started_at = parse_timestamp_ms(session["time_created"])
    u.ended_at = parse_timestamp_ms(session["time_updated"]) or u.started_at
    if u.started_at and u.ended_at:
        u.session_duration_ms = max(0, u.ended_at - u.started_at)

    model_key = _session_model_key(session)
    _add_session_tokens(u, session, model_key)
    if model_key:
        u.models.add(model_key)

    turn_by_message: dict[str, int] = {}
    assistant_seen = 0
    for msg in messages:
        data = _json_obj(msg["data"])
        role = _first_str(data.get("role"), msg["data"] if msg["data"] in ("user", "assistant") else None)
        if role == "user":
            u.user_msgs += 1
        elif role == "assistant":
            assistant_seen += 1
            mid = str(msg["id"])
            turn_by_message[mid] = assistant_seen - 1
            mkey = _message_model_key(data) or model_key
            if mkey:
                u.models.add(mkey)
    if assistant_seen:
        u.assistant_msgs = max(u.assistant_msgs, assistant_seen)

    call_to_tool: dict[str, str] = {}
    for part in parts:
        data = _json_obj(part["data"])
        ptype = _first_str(data.get("type"), data.get("kind"))
        mid = str(part["message_id"])
        turn = turn_by_message.get(mid, max(0, assistant_seen - 1))
        if ptype == "reasoning":
            u.thinking_count += 1
        elif ptype == "file":
            if _is_image_mime(data.get("mime")):
                u.image_count += 1
        elif ptype in ("tool", "tool-invocation", "tool-call"):
            name = _first_str(data.get("tool"), data.get("toolName"), data.get("name"), data.get("title")) or "Tool"
            tool, path = _classify_tool(name, data)
            u.tool_calls[tool] = u.tool_calls.get(tool, 0) + 1
            u.tool_ops.append(ToolOp(turn, tool, path))
            call_id = _first_str(data.get("callID"), data.get("call_id"), data.get("toolCallId"), data.get("id"))
            if call_id:
                call_to_tool[call_id] = tool
            if _part_is_error(data):
                _add_tool_error(u, tool)
        elif ptype in ("tool-result", "tool-output"):
            call_id = _first_str(data.get("callID"), data.get("call_id"), data.get("toolCallId"))
            tool = call_to_tool.get(call_id or "", "Tool")
            if _part_is_error(data):
                _add_tool_error(u, tool)
        elif ptype in ("step-finish", "step_finish"):
            # Live OpenCode: per-step token snapshot for peak window pressure.
            _note_step_input_peak(u, data)
        elif ptype == "compaction":
            u.compaction_count += 1

    # No step-finish parts (legacy / sparse sessions): fall back to session total.
    if u.peak_input_tokens <= 0 and u.total_input > 0:
        u.peak_input_tokens = u.total_input

    return u


def read_user_messages(db_path: Path, session_id: str) -> list[str]:
    if _is_legacy_session(session_id):
        obj = _read_json(Path(session_id))
        return [
            truncate_summary(t.strip(), 500)
            for t in _legacy_texts_for_path(Path(session_id), obj)
            if t.strip()
        ]
    messages: list[str] = []
    with _connect(db_path) as con:
        if not _has_table(con, "message"):
            return []
        rows = con.execute(
            """
            select m.data as message_data, p.data as part_data
            from message m
            left join part p on p.message_id = m.id
            where m.session_id = ?
            order by m.time_created, p.id
            """,
            (session_id,),
        ).fetchall()
    for row in rows:
        msg_data = _json_obj(row["message_data"])
        if msg_data.get("role") != "user":
            continue
        data = _json_obj(row["part_data"])
        if data.get("type") == "text":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(truncate_summary(text.strip(), 500))
    return messages


def read_conversation(db_path: Path, session_id: str) -> list[dict]:
    """Extract the full ordered conversation for one OpenCode session.

    Mirrors :func:`tcer.core.reader.read_conversation`'s output shape. OpenCode
    stores the turn as ``message`` rows (carrying the role) joined to ordered
    ``part`` rows (text / reasoning / tool call / tool result). The mapping is:

      * ``part.type == 'text'``                    -> user or assistant text
        (role from the owning message row)
      * ``part.type == 'reasoning'``               -> assistant thinking
      * ``part.type in ('tool','tool-invocation','tool-call')`` -> tool_use
      * ``part.type in ('tool-result','tool-output')``          -> tool_result

    Legacy on-disk (pre-SQLite) sessions expose only user text, so those return
    a user-only conversation via :func:`read_user_messages`.
    """
    if _is_legacy_session(session_id):
        return [{"role": "user", "type": "text", "text": t, "ts": None}
                for t in read_user_messages(db_path, session_id)]

    with _connect(db_path) as con:
        if not _has_table(con, "message") or not _has_table(con, "part"):
            return []
        msg_rows = con.execute(
            "select id, data from message where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall()
        part_rows = con.execute(
            "select message_id, data, time_created, id from part "
            "where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall()

    role_by_msg: dict[str, str] = {}
    for m in msg_rows:
        data = _json_obj(m["data"])
        role = _first_str(data.get("role"),
                          m["data"] if m["data"] in ("user", "assistant") else None)
        role_by_msg[str(m["id"])] = role or "assistant"

    convo: list[dict] = []
    for part in part_rows:
        data = _json_obj(part["data"])
        ptype = _first_str(data.get("type"), data.get("kind"))
        role = role_by_msg.get(str(part["message_id"]), "assistant")
        ts = parse_timestamp_ms(part["time_created"])

        if ptype == "text":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                convo.append({"role": role, "type": "text",
                              "text": text.strip(), "ts": ts})
        elif ptype == "reasoning":
            text = _first_str(data.get("text"), data.get("reasoning"), data.get("content"))
            if text and text.strip():
                convo.append({"role": "assistant", "type": "thinking",
                              "text": text.strip(), "ts": ts})
        elif ptype in ("tool", "tool-invocation", "tool-call"):
            name = _first_str(data.get("tool"), data.get("toolName"),
                              data.get("name"), data.get("title")) or "Tool"
            tool, _ = _classify_tool(name, data)
            call_id = _first_str(data.get("callID"), data.get("call_id"),
                                 data.get("toolCallId"), data.get("id"))
            tool_input = data.get("input") if isinstance(data.get("input"), dict) else data.get("args")
            # Prefer normalized live shape (state.input / camelCase) when present.
            try:
                tool_input = _normalize_tool_input(data) or tool_input
            except Exception:  # noqa: BLE001
                pass
            convo.append({
                "role": "assistant", "type": "tool_use",
                "name": tool,
                "id": call_id,
                "input": tool_input if isinstance(tool_input, dict) else {},
                "ts": ts,
            })
        elif ptype in ("tool-result", "tool-output"):
            call_id = _first_str(data.get("callID"), data.get("call_id"),
                                 data.get("toolCallId"))
            out = data.get("output")
            if out is None:
                out = data.get("result") or data.get("content")
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
            convo.append({
                "role": "tool", "type": "tool_result",
                "tool_use_id": call_id,
                "is_error": _part_is_error(data),
                "text": text,
                "ts": ts,
            })
    return convo


_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _loc_scan(
    db_path: Path,
    session_id: str,
    *,
    cwd: str | Path | None = None,
    disk_prior: bool = False,
):
    """Single pass: ``(SessionLoc, has_signal)`` for one OpenCode session.

    Prefer persisted ``summary_*`` counters when present; else replay edit/write
    tool parts. ``has_signal`` is True if summary counters or any parseable edit
    tool exists (mirrors Codex/Grok ``_loc_scan`` so analyze can avoid a second
    DB walk via separate ``has_loc_signal`` + ``session_loc_full``).
    """
    from tcer.core.loc import SessionLoc, _LocAccumulator

    if _is_legacy_session(session_id):
        obj = _read_json(Path(session_id))
        added = _as_int(_dig(obj, "summary", "additions") or obj.get("summary_additions"))
        deleted = _as_int(_dig(obj, "summary", "deletions") or obj.get("summary_deletions"))
        files = _as_int(_dig(obj, "summary", "files") or obj.get("summary_files"))
        if added or deleted or files:
            return SessionLoc(added=added, deleted=deleted), True
        return _legacy_tool_loc(Path(session_id), obj, cwd=cwd, disk_prior=disk_prior)

    with _connect(db_path) as con:
        row = con.execute(
            "select * from session where id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"OpenCode session '{session_id}' not found")
        parts = (
            con.execute(
                "select data from part where session_id = ? order by time_created, id",
                (session_id,),
            ).fetchall()
            if _has_table(con, "part")
            else []
        )

    keys = set(row.keys())
    added = _as_int(row["summary_additions"]) if "summary_additions" in keys else 0
    deleted = _as_int(row["summary_deletions"]) if "summary_deletions" in keys else 0
    files = _as_int(row["summary_files"]) if "summary_files" in keys else 0
    if added or deleted or files:
        diffs = _json_obj(row["summary_diffs"]) if "summary_diffs" in keys else {}
        edit_counts: dict[str, int] = {}
        if isinstance(diffs, list):
            for item in diffs:
                if isinstance(item, dict):
                    path = _first_str(item.get("path"), item.get("file"), item.get("filename"))
                    if path:
                        edit_counts[path] = edit_counts.get(path, 0) + 1
        return SessionLoc(
            added=added,
            deleted=deleted,
            unseen_writes=0,
            rework_deleted=0,
            high_churn_files=sum(1 for c in edit_counts.values() if c >= 3),
            file_edit_counts=edit_counts,
        ), True

    session_cwd = cwd
    if session_cwd is None and "directory" in keys:
        d = row["directory"]
        if isinstance(d, str) and d:
            session_cwd = d
    acc = _LocAccumulator(cwd=session_cwd, disk_prior=disk_prior)
    saw_edit = False
    for part in parts:
        data = _json_obj(part["data"])
        ptype = _first_str(data.get("type"), data.get("kind"))
        if ptype not in ("tool", "tool-invocation", "tool-call"):
            continue
        name = _first_str(data.get("tool"), data.get("toolName"), data.get("name"), data.get("title"))
        if not name:
            continue
        tool, _path = _classify_tool(name, data)
        if tool not in _EDIT_TOOLS:
            continue
        saw_edit = True
        acc.on_tool_use(tool, _normalize_tool_input(data))
    return acc.finish(), saw_edit


def session_loc_full(
    db_path: Path,
    session_id: str,
    *,
    cwd: str | Path | None = None,
    disk_prior: bool = False,
):
    """Return LOC for one OpenCode session (summary counters or tool replay)."""
    return _loc_scan(db_path, session_id, cwd=cwd, disk_prior=disk_prior)[0]


def has_loc_signal(db_path: Path, session_id: str) -> bool:
    """True if summary counters or parseable edit tool parts exist."""
    try:
        return _loc_scan(db_path, session_id)[1]
    except FileNotFoundError:
        return False


def _legacy_tool_loc(session_path: Path, obj: dict, *, cwd, disk_prior: bool):
    """Best-effort ``(SessionLoc, saw_edit)`` from legacy JSON tool parts."""
    from tcer.core.loc import _LocAccumulator

    acc = _LocAccumulator(cwd=cwd, disk_prior=disk_prior)
    saw_edit = False
    for data in _legacy_iter_tool_parts(session_path, obj):
        name = _first_str(data.get("tool"), data.get("toolName"), data.get("name"))
        if not name:
            continue
        tool, _ = _classify_tool(name, data)
        if tool in _EDIT_TOOLS:
            saw_edit = True
            acc.on_tool_use(tool, _normalize_tool_input(data))
    return acc.finish(), saw_edit


def _legacy_iter_tool_parts(session_path: Path, obj: dict):
    """Yield tool-part dicts from a legacy OpenCode session JSON (best-effort)."""
    # Common shapes: messages[].parts[], or sibling part files (handled elsewhere).
    messages = obj.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            parts = msg.get("parts") or msg.get("content") or []
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and _first_str(part.get("type"), part.get("kind")) in (
                    "tool", "tool-invocation", "tool-call",
                ):
                    yield part


def _connect(path: Path) -> sqlite3.Connection:
    """Open OpenCode DB read-only with a busy timeout for concurrent writers.

    OpenCode may hold a write lock while TCER analyzes; ``busy_timeout`` makes
    SQLite wait briefly instead of failing immediately with ``database is locked``.
    """
    resolved = path.resolve()
    try:
        uri = resolved.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (sqlite3.OperationalError, OSError):
        # UNC paths (\\wsl$\...) may not work with as_uri(); fall back to
        # a plain path connection.
        con = sqlite3.connect(str(resolved), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return con


@lru_cache(maxsize=1)
def _data_dirs() -> list[Path]:
    overrides = [
        value
        for value in (
            os.environ.get("OPENCODE_DATA_DIR"),
            os.environ.get("OPENCODE_DATA_HOME"),
        )
        if value
    ]
    if overrides:
        return _dedupe_paths(
            Path(part.strip())
            for value in overrides
            for part in value.split(",")
            if part.strip()
        )

    dirs: list[Path] = []
    dirs.append(opencode_dir())
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        dirs.append(Path(xdg) / "opencode")
    dirs.append(Path.home() / ".local" / "share" / "opencode")
    dirs.append(Path.home() / ".opencode")

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        dirs.append(Path(local_app) / "opencode")
        dirs.append(Path(local_app) / "opencode" / "data")
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "opencode")
        dirs.append(Path(appdata) / "opencode" / "data")

    return _dedupe_paths(dirs + _wsl_data_dirs())


def _dedupe_paths(paths) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for d in paths:
        try:
            key = d.resolve()
        except OSError:
            key = d
        if key not in seen:
            out.append(d)
            seen.add(key)
    return out


# ── WSL discovery ──────────────────────────────────────────────────────

_WSL_PREFIXES = ("\\\\wsl$", "\\\\wsl.localhost")
_WSL_OC_SUBDIRS = (
    Path(".local") / "share" / "opencode",
    Path(".opencode"),
)


def _wsl_data_dirs() -> list[Path]:
    r"""Probe WSL distributions for OpenCode data directories.

    On Windows, WSL filesystems are accessible via ``\\wsl$\<distro>`` or
    ``\\wsl.localhost\<distro>``.  This function enumerates home directories
    inside each visible distribution and returns those that contain OpenCode
    data.  On non-Windows platforms (or when no WSL distributions are
    installed) an empty list is returned.
    """
    if sys.platform != "win32":
        return []

    dirs: list[Path] = []
    seen_distros: set[Path] = set()
    for prefix in _WSL_PREFIXES:
        root = Path(prefix)
        if not root.is_dir():
            continue
        try:
            distros = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for distro in distros:
            try:
                resolved = distro.resolve()
            except OSError:
                resolved = distro
            if resolved in seen_distros:
                continue
            seen_distros.add(resolved)
            home_root = distro / "home"
            if not home_root.is_dir():
                continue
            try:
                users = [u for u in home_root.iterdir() if u.is_dir()]
            except OSError:
                continue
            for user_home in users:
                for sub in _WSL_OC_SUBDIRS:
                    candidate = user_home / sub
                    if candidate.is_dir():
                        dirs.append(candidate)
    return dirs


def _legacy_project_refs() -> list[ProjectRef]:
    roots: list[Path] = []
    for base in _data_dirs():
        roots.append(base / "storage" / "session")
        roots.append(base / "session")
        project_root = base / "project"
        if project_root.is_dir():
            for p in project_root.glob("*"):
                if p.is_dir():
                    roots.append(p / "storage" / "session")
    groups: dict[str, list[Path]] = {}
    cwd_by_key: dict[str, str | None] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob("*/*.json"):
            obj = _read_json(path)
            project_id = _first_str(obj.get("projectID"), obj.get("project_id"), path.parent.name) or _NO_PROJECT_KEY
            groups.setdefault(project_id, []).append(path)
            cwd = _first_str(obj.get("directory"), obj.get("cwd"), obj.get("worktree"))
            if cwd:
                cwd_by_key[project_id] = cwd

    refs: list[ProjectRef] = []
    for project_id, paths in groups.items():
        cwd = cwd_by_key.get(project_id)
        refs.append(ProjectRef(
            source="opencode",
            key=f"legacy:{project_id}",
            display_name=_display_name(project_id, cwd),
            cwd=cwd,
            path=paths[0].parent,
            session_paths=tuple(sorted(paths)),
        ))
    return refs


def _is_legacy_session(session_id: str) -> bool:
    return session_id.endswith(".json") or Path(session_id).suffix == ".json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def _legacy_session_meta(path: Path) -> SessionMeta:
    obj = _read_json(path)
    sid = _first_str(obj.get("id"), obj.get("sessionID"), obj.get("session_id"), path.stem)
    model = _json_obj(obj.get("model"))
    provider = _first_str(
        model.get("providerID") if isinstance(model, dict) else None,
        model.get("provider_id") if isinstance(model, dict) else None,
        obj.get("providerID"),
        obj.get("provider_id"),
    )
    return SessionMeta(
        session_id=sid,
        cwd=_first_str(obj.get("directory"), obj.get("cwd"), obj.get("worktree")),
        title=_first_str(obj.get("title"), obj.get("name")),
        path=path,
        is_subagent=False,
        entrypoint="opencode",
        source="opencode",
        model_provider=provider,
    )


def _legacy_usage(path: Path) -> TokenUsage:
    obj = _read_json(path)
    u = TokenUsage()
    u.started_at = parse_timestamp_ms(_first_str(obj.get("timeCreated"), obj.get("time_created"), obj.get("created")))
    u.ended_at = parse_timestamp_ms(_first_str(obj.get("timeUpdated"), obj.get("time_updated"), obj.get("updated"))) or u.started_at
    if u.started_at and u.ended_at:
        u.session_duration_ms = max(0, u.ended_at - u.started_at)
    tokens = _json_obj(obj.get("tokens")) or obj
    i = _as_int(tokens.get("input") or tokens.get("tokens_input"))
    o = _as_int(tokens.get("output") or tokens.get("tokens_output"))
    cr = _as_int(tokens.get("cache_read") or tokens.get("tokens_cache_read"))
    cw = _as_int(tokens.get("cache_write") or tokens.get("tokens_cache_write"))
    reasoning = _as_int(tokens.get("reasoning") or tokens.get("tokens_reasoning"))
    model = _legacy_model_key(obj)
    u.input_tokens = i
    u.output_tokens = o
    u.cache_read_input_tokens = cr
    u.cache_creation_input_tokens = cw
    u.reasoning_output_tokens = reasoning
    if model:
        u.models.add(model)
    if i + o + cr + cw:
        u.assistant_msgs = 1
        u.bucket(model or "").add(i, cw, cr, o)
    texts = list(_legacy_texts_for_path(path, obj))
    u.user_msgs = len(texts)
    return u


def _legacy_texts_for_path(path: Path, obj: dict):
    yielded = False
    for text in _legacy_texts(obj):
        yielded = True
        yield text
    if yielded:
        return

    sid = _first_str(obj.get("id"), obj.get("sessionID"), obj.get("session_id"), path.stem)
    for msg_path in _legacy_message_paths(path, sid):
        msg_obj = _read_json(msg_path)
        for text in _legacy_texts(msg_obj):
            yield text
        role = _first_str(msg_obj.get("role"), _dig(msg_obj, "data", "role"))
        if role != "user":
            continue
        text = _first_str(msg_obj.get("text"), msg_obj.get("content"), _dig(msg_obj, "data", "text"))
        if text:
            yield text
            continue
        mid = _first_str(msg_obj.get("id"), msg_obj.get("messageID"), msg_obj.get("message_id"), msg_path.stem)
        for part_path in _legacy_part_paths(path, mid):
            part_obj = _read_json(part_path)
            text = _first_str(part_obj.get("text"), part_obj.get("content"), _dig(part_obj, "data", "text"))
            if text:
                yield text


def _legacy_message_paths(session_path: Path, sid: str | None) -> list[Path]:
    if not sid:
        return []
    parts = list(session_path.parts)
    for i, part in enumerate(parts):
        if part == "storage" and i + 1 < len(parts) and parts[i + 1] == "session":
            base = Path(*parts[:i + 1]) / "message"
            candidates = [
                base / f"{sid}.json",
                base / session_path.parent.name / f"{sid}.json",
            ]
            session_dir = base / sid
            if session_dir.is_dir():
                candidates.extend(sorted(session_dir.glob("*.json")))
            return [p for p in candidates if p.is_file()]
    return []


def _legacy_part_paths(session_path: Path, mid: str | None) -> list[Path]:
    if not mid:
        return []
    parts = list(session_path.parts)
    for i, part in enumerate(parts):
        if part == "storage" and i + 1 < len(parts) and parts[i + 1] == "session":
            part_dir = Path(*parts[:i + 1]) / "part" / mid
            if part_dir.is_dir():
                return sorted(part_dir.glob("*.json"))
    return []


def _legacy_texts(obj: dict):
    for msg in obj.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = _first_str(msg.get("role"), _dig(msg, "data", "role"))
        if role != "user":
            continue
        text = _first_str(msg.get("text"), msg.get("content"), _dig(msg, "data", "text"))
        if text:
            yield text


def _legacy_model_key(obj: dict) -> str:
    model = _json_obj(obj.get("model"))
    if isinstance(model, dict):
        provider = _first_str(model.get("providerID"), model.get("provider_id"))
        mid = _first_str(model.get("id"), model.get("modelID"), model.get("model_id"))
        if provider and mid:
            return pricing.normalize(f"{provider}/{mid}")
        if mid:
            return pricing.normalize(mid)
    mid = _first_str(obj.get("modelID"), obj.get("model_id"), obj.get("model"))
    return pricing.normalize(mid) if mid else ""


def _dig(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _has_tables(path: Path, names: set[str]) -> bool:
    try:
        with _connect(path) as con:
            found = {
                r["name"] for r in con.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }
        return names <= found
    except sqlite3.Error:
        return False


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _is_useless_worktree(path: str | None) -> bool:
    """True for placeholder worktrees that should not override session.directory."""
    if not path or not str(path).strip():
        return True
    p = str(path).strip().replace("\\", "/")
    return p in {"/", "\\", ".", "./"}


def _effective_cwd(session_dir: str | None, project_worktree: str | None) -> str | None:
    """Prefer the session's directory over a useless global project worktree."""
    if session_dir and not _is_useless_worktree(session_dir):
        return session_dir
    if project_worktree and not _is_useless_worktree(project_worktree):
        return project_worktree
    return session_dir or project_worktree


def _make_project_key(db_name: str, project_id: str, cwd: str | None) -> str:
    """Stable unique key: ``dbname::project_id::cwd`` (cwd may contain ``:``)."""
    return f"{db_name}::{project_id}::{cwd or ''}"


def _parse_project_key(key: str) -> tuple[str, str | None]:
    """Return ``(project_id, cwd)`` from a ref key."""
    if "::" in key:
        parts = key.split("::", 2)
        # [db, project_id, cwd]
        if len(parts) >= 3:
            return parts[1] or _NO_PROJECT_KEY, (parts[2] or None)
        if len(parts) == 2:
            return parts[1] or _NO_PROJECT_KEY, None
    # Legacy ``dbname:project_id`` keys
    if ":" in key:
        return key.split(":", 1)[1] or _NO_PROJECT_KEY, None
    return key, None


def _project_id_from_key(key: str) -> str:
    return _parse_project_key(key)[0]


def _session_model_key(row: sqlite3.Row) -> str:
    model = _json_obj(row["model"])
    if isinstance(model, dict):
        provider = _first_str(model.get("providerID"), model.get("provider_id"))
        mid = _first_str(model.get("id"), model.get("modelID"), model.get("model_id"))
        if provider and mid:
            return pricing.normalize(f"{provider}/{mid}")
        if mid:
            return pricing.normalize(mid)
    return ""


def _message_model_key(data: dict) -> str:
    provider = _first_str(data.get("providerID"), data.get("provider_id"))
    mid = _first_str(data.get("modelID"), data.get("model_id"), data.get("model"))
    if provider and mid:
        return pricing.normalize(f"{provider}/{mid}")
    return pricing.normalize(mid) if mid else ""


def _add_session_tokens(u: TokenUsage, row: sqlite3.Row, model: str) -> None:
    i = _as_int(row["tokens_input"])
    o_visible = _as_int(row["tokens_output"])
    cr = _as_int(row["tokens_cache_read"])
    cw = _as_int(row["tokens_cache_write"])
    reasoning = _as_int(row["tokens_reasoning"])
    # OpenCode stores reasoning *outside* output (unlike Codex/Grok where
    # reasoning is a subset of output_tokens). Fold it in so:
    # - cost_usd prices reasoning at the output rate
    # - reasoning_output_ratio stays in [0, 1]
    # - total token counts match the billable generation budget
    o = o_visible + reasoning
    u.input_tokens += i
    u.output_tokens += o
    u.cache_read_input_tokens += cr
    u.cache_creation_input_tokens += cw
    u.reasoning_output_tokens += reasoning
    if i + o + cr + cw:
        u.assistant_msgs = max(u.assistant_msgs, 1)
        u.bucket(model or "").add(i, cw, cr, o)
    # peak_input_tokens: prefer step-finish snapshots (set while scanning parts);
    # session totals are multi-step sums and must not be treated as a single peak.


def _note_step_input_peak(u: TokenUsage, data: dict) -> None:
    """Update peak_input_tokens from an OpenCode ``step-finish`` part.

    Live shape::

        {"type": "step-finish", "tokens": {
            "input": N, "output": M, "reasoning": R,
            "cache": {"read": C, "write": W}
        }}

    Peak uses input + cache read + cache write for one step (same dimensions as
    Claude/Codex peak tracking).
    """
    tok = data.get("tokens")
    if not isinstance(tok, dict):
        return
    i = _as_int(tok.get("input"))
    cache = tok.get("cache")
    if isinstance(cache, dict):
        cr = _as_int(cache.get("read"))
        cw = _as_int(cache.get("write"))
    else:
        cr = cw = 0
    step_in = i + cr + cw
    if step_in > 0:
        u.peak_input_tokens = max(u.peak_input_tokens, step_in)


# OpenCode tool ids → TCER-canonical names (case-insensitive keys).
_OPENCODE_TOOL_MAP = {
    "read": "Read",
    "view": "Read",
    "write": "Write",
    "create": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "apply_patch": "Edit",
    "grep": "Grep",
    "search": "Grep",
    "glob": "Glob",
    "list": "Glob",
    "ls": "Glob",
    "bash": "Bash",
    "terminal": "Bash",
    "run": "Bash",
    "execute": "Bash",
    "todowrite": "TodoWrite",
    "todo_write": "TodoWrite",
    "todoread": "TodoRead",
    "todo_read": "TodoRead",
    "webfetch": "WebFetch",
    "web_fetch": "WebFetch",
    "websearch": "WebSearch",
    "web_search": "WebSearch",
    "question": "AskUserQuestion",
    "askuserquestion": "AskUserQuestion",
    "task": "Task",
}


def _classify_tool(name: str, data: dict) -> tuple[str, str]:
    lower = name.lower().strip()
    path = _path_hint(data)
    mapped = _OPENCODE_TOOL_MAP.get(lower)
    if mapped:
        return mapped, path
    # Already-canonical (Read/Edit/…)
    for canon in ("Read", "Edit", "Write", "Grep", "Glob", "Bash", "Task",
                  "WebSearch", "WebFetch", "TodoWrite"):
        if lower == canon.lower():
            return canon, path
    return name, path


def _path_hint(data: dict) -> str:
    """Extract a file/dir path from an OpenCode tool part.

    Live parts nest args under ``state.input`` with camelCase keys
    (``filePath``); older fixtures put ``path`` at the top level.
    """
    for key in ("path", "file", "filePath", "filepath", "file_path", "filename", "directory", "cwd"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    inp = data.get("input")
    if isinstance(inp, dict):
        p = _path_hint(inp)
        if p:
            return p
    state = data.get("state")
    if isinstance(state, dict):
        return _path_hint(state)
    return ""


def _normalize_tool_input(data: dict) -> dict:
    """Map OpenCode tool args to Claude-style keys for ``loc._LocAccumulator``.

    OpenCode uses camelCase (``filePath`` / ``oldString`` / ``newString``) nested
    under ``state.input``; Claude JSONL uses snake_case at ``input``.
    """
    raw: dict = {}
    # Prefer nested state.input (live OpenCode), then top-level input/args.
    state = data.get("state")
    if isinstance(state, dict) and isinstance(state.get("input"), dict):
        raw = dict(state["input"])
    elif isinstance(data.get("input"), dict):
        raw = dict(data["input"])
    elif isinstance(data.get("args"), dict):
        raw = dict(data["args"])
    else:
        raw = dict(data)

    out = dict(raw)
    # Path aliases → file_path
    fp = (
        raw.get("file_path")
        or raw.get("filePath")
        or raw.get("filepath")
        or raw.get("path")
        or raw.get("file")
        or raw.get("filename")
        or data.get("path")
        or data.get("filePath")
    )
    if isinstance(fp, str) and fp:
        out["file_path"] = fp
    # Edit string aliases
    if "old_string" not in out and "oldString" in raw:
        out["old_string"] = raw["oldString"]
    if "new_string" not in out and "newString" in raw:
        out["new_string"] = raw["newString"]
    # Write content aliases
    if "content" not in out:
        for k in ("contents", "text", "code"):
            if k in raw and isinstance(raw[k], str):
                out["content"] = raw[k]
                break
    return out


def _part_is_error(data: dict) -> bool:
    if data.get("error") or data.get("isError") or data.get("is_error"):
        return True
    state = data.get("state")
    if isinstance(state, dict):
        status = str(state.get("status") or state.get("state") or "").lower()
        if status in {"error", "failed", "failure"}:
            return True
        if state.get("error"):
            return True
        return False
    status = str(state or data.get("status") or "").lower()
    return status in {"error", "failed", "failure"}


def _add_tool_error(u: TokenUsage, tool: str) -> None:
    u.tool_errors += 1
    u.tool_errors_by_tool[tool] = u.tool_errors_by_tool.get(tool, 0) + 1


def _json_obj(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _permission_label(permission) -> str | None:
    if isinstance(permission, str):
        return permission
    if isinstance(permission, dict):
        for key in ("mode", "type", "name"):
            val = permission.get(key)
            if isinstance(val, str):
                return val
    return None


def _display_name(name, cwd: str | None) -> str:
    if isinstance(name, str) and name.strip():
        if re.fullmatch(r"[A-Za-z]:[\\/].*|/.*", name):
            return Path(name).name or name
        return name.strip()
    if cwd:
        return Path(cwd).name or cwd
    return _NO_PROJECT_LABEL


def _first_str(*values) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _as_int(v) -> int:
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _is_image_mime(value) -> bool:
    return isinstance(value, str) and value.lower().startswith("image/")
