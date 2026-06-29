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
    """Group OpenCode sessions by project/worktree directory."""
    refs: list[ProjectRef] = []
    for db in discover_databases():
        with _connect(db) as con:
            has_project = _has_table(con, "project")
            if has_project:
                rows = con.execute(
                    """
                    select
                      s.project_id,
                      coalesce(p.worktree, s.directory) as cwd,
                      coalesce(p.name, s.directory, s.project_id) as name,
                      count(*) as n
                    from session s
                    left join project p on p.id = s.project_id
                    group by s.project_id, cwd, name
                    order by name
                    """
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    select
                      coalesce(project_id, directory, ?) as project_id,
                      directory as cwd,
                      coalesce(directory, project_id, ?) as name,
                      count(*) as n
                    from session
                    group by project_id, directory
                    order by name
                    """,
                    (_NO_PROJECT_KEY, _NO_PROJECT_LABEL),
                ).fetchall()

        for row in rows:
            project_id = str(row["project_id"] or _NO_PROJECT_KEY)
            cwd = row["cwd"] if isinstance(row["cwd"], str) and row["cwd"] else None
            key = f"{db.name}:{project_id}"
            refs.append(ProjectRef(
                source="opencode",
                key=key,
                display_name=_display_name(row["name"], cwd),
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
    project_id = _project_id_from_key(ref.key)
    with _connect(ref.path) as con:
        rows = con.execute(
            "select id from session where project_id = ? order by time_created, id",
            (project_id,),
        ).fetchall()
        if not rows and ref.cwd:
            rows = con.execute(
                "select id from session where directory = ? order by time_created, id",
                (ref.cwd,),
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
        elif ptype == "compaction":
            u.compaction_count += 1

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


def session_loc_full(db_path: Path, session_id: str):
    """Return LOC from OpenCode's persisted session summary."""
    from tcer.core.loc import SessionLoc

    if _is_legacy_session(session_id):
        obj = _read_json(Path(session_id))
        added = _as_int(_dig(obj, "summary", "additions") or obj.get("summary_additions"))
        deleted = _as_int(_dig(obj, "summary", "deletions") or obj.get("summary_deletions"))
        return SessionLoc(added=added, deleted=deleted)

    with _connect(db_path) as con:
        row = con.execute(
            "select summary_additions, summary_deletions, summary_files, summary_diffs from session where id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError(f"OpenCode session '{session_id}' not found")
    added = _as_int(row["summary_additions"])
    deleted = _as_int(row["summary_deletions"])
    diffs = _json_obj(row["summary_diffs"])
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
    )


def has_loc_signal(db_path: Path, session_id: str) -> bool:
    if _is_legacy_session(session_id):
        obj = _read_json(Path(session_id))
        return _as_int(_dig(obj, "summary", "files") or obj.get("summary_files")) > 0 or (
            _as_int(_dig(obj, "summary", "additions") or obj.get("summary_additions")) > 0
            or _as_int(_dig(obj, "summary", "deletions") or obj.get("summary_deletions")) > 0
        )
    with _connect(db_path) as con:
        row = con.execute(
            "select summary_additions, summary_deletions, summary_files from session where id = ?",
            (session_id,),
        ).fetchone()
    return bool(row and any(_as_int(row[k]) > 0 for k in row.keys()))


def _connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


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

    return _dedupe_paths(dirs)


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


def _project_id_from_key(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


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
    o = _as_int(row["tokens_output"])
    cr = _as_int(row["tokens_cache_read"])
    cw = _as_int(row["tokens_cache_write"])
    reasoning = _as_int(row["tokens_reasoning"])
    u.input_tokens += i
    u.output_tokens += o
    u.cache_read_input_tokens += cr
    u.cache_creation_input_tokens += cw
    u.reasoning_output_tokens += reasoning
    if i + o + cr + cw:
        u.assistant_msgs = max(u.assistant_msgs, 1)
        u.bucket(model or "").add(i, cw, cr, o)


def _classify_tool(name: str, data: dict) -> tuple[str, str]:
    lower = name.lower()
    path = _path_hint(data)
    if lower in {"read", "view"}:
        return "Read", path
    if lower in {"write", "create"}:
        return "Write", path
    if lower in {"edit", "patch", "apply_patch"}:
        return "Edit", path
    if lower in {"grep", "search"}:
        return "Grep", path
    if lower in {"glob", "list", "ls"}:
        return "Glob", path
    if lower in {"bash", "terminal", "run", "execute"}:
        return "Bash", path
    return name, path


def _path_hint(data: dict) -> str:
    for key in ("path", "file", "filePath", "filepath", "filename", "directory", "cwd"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    inp = data.get("input")
    if isinstance(inp, dict):
        return _path_hint(inp)
    return ""


def _part_is_error(data: dict) -> bool:
    if data.get("error") or data.get("isError") or data.get("is_error"):
        return True
    state = str(data.get("state") or data.get("status") or "").lower()
    return state in {"error", "failed", "failure"}


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
