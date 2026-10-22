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
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import as_int as _as_int, first_str as _first_str
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


# Child (subagent) sessions are folded into their parent by aggregate_usage /
# _loc_scan, so project listings only expose top-level sessions.  A child whose
# parent row is gone (deleted) is promoted to top-level rather than dropped.
_TOP_LEVEL = "(parent_id is null or parent_id not in (select id from session))"


def sessions_for_project(project: str | ProjectRef) -> list[str]:
    """Top-level session ids of the project (subagent child sessions folded)."""
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    if ref is None or ref.path is None:
        return []
    project_id, key_cwd = _parse_project_key(ref.key)
    cwd = ref.cwd or key_cwd
    with _connect(ref.path) as con:
        rows = []
        # Prefer directory filter so global/project_id buckets don't mix trees.
        if cwd and not _is_useless_worktree(cwd):
            rows = con.execute(
                (
                    "select id from session where directory = ? and "
                    + _TOP_LEVEL + " order by time_created, id"
                ),
                (cwd,),
            ).fetchall()
        if not rows and project_id and project_id != _NO_PROJECT_KEY:
            rows = con.execute(
                "select id from session where project_id = ? and " + _TOP_LEVEL + " order by time_created, id",
                (project_id,),
            ).fetchall()
        if not rows and project_id == _NO_PROJECT_KEY:
            rows = con.execute(
                "select id from session where project_id is null and " + _TOP_LEVEL + " order by time_created, id"
            ).fetchall()
    return [str(r["id"]) for r in rows]


def latest_activity_ms(project: str | ProjectRef) -> int | None:
    """max(session.time_created) for the project, as epoch ms.

    ``time_created`` is an integer ms column → value is already epoch ms.
    Scoping mirrors ``sessions_for_project`` (directory → project_id → null);
    Returns None if no DB / no matching rows / NULL max.
    """
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    if ref is None or ref.path is None:
        return None
    project_id, key_cwd = _parse_project_key(ref.key)
    cwd = ref.cwd or key_cwd
    val = None
    with _connect(ref.path) as con:
        row = None
        if cwd and not _is_useless_worktree(cwd):
            row = con.execute(
                "select max(time_created) as m from session where directory = ?",
                (cwd,),
            ).fetchone()
        if (row is None or row["m"] is None) and project_id and project_id != _NO_PROJECT_KEY:
            row = con.execute(
                "select max(time_created) as m from session where project_id = ?",
                (project_id,),
            ).fetchone()
        if (row is None or row["m"] is None) and project_id == _NO_PROJECT_KEY:
            row = con.execute(
                "select max(time_created) as m from session where project_id is null"
            ).fetchone()
        if row is not None:
            val = row["m"]
    return parse_timestamp_ms(val) if val is not None else None


def read_session_meta(db_path: Path, session_id: str) -> SessionMeta:
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
    keys = set(row.keys())
    version = (row["version"] if "version" in keys
               and isinstance(row["version"], str) and row["version"] else None)
    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=db_path,
        is_subagent=bool(row["parent_id"]),
        entrypoint="opencode",
        source="opencode",
        cli_version=version,
        model_provider=provider,
        approval_policy=_permission_label(permission),
    )


def subagent_ids(db_path: Path, session_id: str) -> list[str]:
    """All descendant (``task`` tool child) session ids of *session_id*, oldest first."""
    with _connect(db_path) as con:
        return _descendant_ids(con, session_id)


def _descendant_ids(con: sqlite3.Connection, session_id: str) -> list[str]:
    try:
        rows = con.execute(
            """
            with recursive d(id) as (
              select id from session where parent_id = ?
              union
              select s.id from session s join d on s.parent_id = d.id
            )
            select s.id from session s join d on s.id = d.id
            order by s.time_created, s.id
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(r["id"]) for r in rows if str(r["id"]) != session_id]


def aggregate_usage(db_path: Path, session_id: str) -> TokenUsage:
    """Aggregate one OpenCode session, folding its subagent child sessions.

    ``task`` tool calls spawn child sessions (``session.parent_id``).  Their
    tokens are real cost, so they merge into the parent instead of counting as
    separate sessions (same policy as Claude / omp subagents).
    """
    u = _aggregate_single(db_path, session_id)
    for child in subagent_ids(db_path, session_id):
        u = u.merge(_aggregate_single(db_path, child, is_subagent=True))
    return u


def _aggregate_single(db_path: Path, session_id: str, *, is_subagent: bool = False) -> TokenUsage:
    """Aggregate ONE OpenCode session from SQLite rows (no subagent merge)."""
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
    if model_key:
        u.models.add(model_key)

    # revert 列非空 = 用户回退过 AI 的改动（最直接的「产出被拒绝」信号）。
    if "revert" in session.keys():
        rev = session["revert"]
        if rev is not None and str(rev).strip() not in ("", "null", "{}", "[]"):
            u.revert_events += 1

    # OpenCode 自报成本 → 与 TCER 价表计价交叉校验（0/缺失视为未知）。
    if "cost" in session.keys():
        try:
            reported = float(session["cost"] or 0)
        except (TypeError, ValueError):
            reported = 0.0
        if reported > 0:
            u.reported_cost_usd = reported

    turn_by_message: dict[str, int] = {}
    msg_buckets: dict[str, list[int]] = {}  # model -> [input, cache_write, cache_read, output]
    assistant_seen = 0
    for msg in messages:
        data = _json_obj(msg["data"])
        role = _first_str(data.get("role"), msg["data"] if msg["data"] in ("user", "assistant") else None)
        if role == "user":
            # A subagent's user message is the Task dispatch prompt, not a human turn.
            if not is_subagent:
                u.user_msgs += 1
        elif role == "assistant":
            assistant_seen += 1
            mid = str(msg["id"])
            turn_by_message[mid] = assistant_seen - 1
            mkey = _message_model_key(data) or model_key
            if mkey:
                u.models.add(mkey)
            # 逐消息 token 快照 → peak 补齐（多回合且无 step-finish 时不再留 0）。
            tok = data.get("tokens")
            if isinstance(tok, dict):
                cache = tok.get("cache")
                cr = _as_int(cache.get("read")) if isinstance(cache, dict) else 0
                cw = _as_int(cache.get("write")) if isinstance(cache, dict) else 0
                inp = _as_int(tok.get("input"))
                step_in = inp + cr + cw
                if step_in > 0:
                    u.peak_input_tokens = max(u.peak_input_tokens, step_in)
                # 逐消息按模型累计（与 _add_session_tokens 同口径：reasoning 折入 output）。
                out = _as_int(tok.get("output")) + _as_int(tok.get("reasoning"))
                if inp + cr + cw + out > 0 and mkey:
                    b = msg_buckets.setdefault(mkey, [0, 0, 0, 0])
                    b[0] += inp
                    b[1] += cw
                    b[2] += cr
                    b[3] += out
    _add_session_tokens(u, session, model_key, msg_buckets)
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
            # reasoning part 带 time.{start,end} → 思考耗时。
            t = data.get("time")
            if isinstance(t, dict):
                start = _as_int(t.get("start"))
                end = _as_int(t.get("end"))
                if end > start > 0:
                    u.reasoning_ms_total += end - start
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
            _note_step_turn_stat(u, data, turn, parse_timestamp_ms(part["time_created"]))
        elif ptype == "compaction":
            u.compaction_count += 1

    # No step-finish parts (sparse sessions): the session total is a
    # multi-step sum, so it only approximates a single-turn peak when the
    # session has at most one assistant turn. Multi-turn sessions without
    # step snapshots keep peak at 0 → context_window_used_ratio stays None
    # instead of being inflated by the summed total.
    if u.peak_input_tokens <= 0 and u.total_input > 0 and u.assistant_msgs <= 1:
        u.peak_input_tokens = u.total_input

    return u


def read_user_messages(db_path: Path, session_id: str) -> list[str]:
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
        role = msg_data.get("role")
        if role is None and row["message_data"] in ("user", "assistant"):
            # 裸字符串角色（data 列直接存 "user"）：aggregate_usage /
            # read_conversation 都为同一形状做了防御，这里保持一致。
            role = row["message_data"]
        if role != "user":
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
    """
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
            # Live parts carry the result inside state (output / error).
            st = data.get("state")
            if isinstance(st, dict) and st.get("status") in ("completed", "error"):
                res = st.get("output") if st.get("status") == "completed" else st.get("error")
                convo.append({
                    "role": "tool", "type": "tool_result",
                    "tool_use_id": call_id,
                    "is_error": st.get("status") == "error",
                    "text": res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str),
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


def _loc_scan(db_path: Path, session_id: str):
    """Single pass: ``(SessionLoc, has_signal)`` for one session + its subagents.

    Child (``task`` subagent) sessions are folded via ``merge_session_locs`` so
    their output counts for the parent.  ``has_signal`` is True if summary
    counters or any parseable edit tool exists (mirrors Codex/Grok ``_loc_scan``
    so analyze can avoid a second DB walk).
    """
    from tcer.core.loc import merge_session_locs

    own, signal = _loc_scan_single(db_path, session_id)
    children = subagent_ids(db_path, session_id)
    if not children:
        return own, signal
    slocs = [own]
    for child in children:
        try:
            sl, sig = _loc_scan_single(db_path, child)
        except FileNotFoundError:
            continue
        slocs.append(sl)
        signal = signal or sig
    return merge_session_locs(slocs), signal


def _loc_scan_single(db_path: Path, session_id: str):
    """``(SessionLoc, has_signal)`` for ONE session (no subagent merge).

    Prefer persisted ``summary_*`` counters when present; else replay the
    *successful* edit/write/apply_patch tool parts.
    """
    from tcer.core.loc import SessionLoc, _LocAccumulator

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

    acc = _LocAccumulator()
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
        # 按结果裁决：失败 / 被拒 / 中断（pending、running）的调用不产生行增量。
        if _tool_status(data) not in ("", "completed"):
            continue
        for call_name, inp, is_new in _edit_calls(name, tool, data):
            acc.on_tool_use(call_name, inp)
            if is_new and call_name == "Write":
                # 确认是新文件（apply_patch Add / write metadata.exists=false）：
                # old=0 假定成立，撤销 unseen 暴露。
                acc.note_write_original(inp.get("file_path", ""), "")
    return acc.finish(), saw_edit


def _tool_status(data: dict) -> str:
    """Lower-cased ``state.status`` ("" when the part carries no state)."""
    state = data.get("state")
    if isinstance(state, dict):
        return str(state.get("status") or "").lower()
    return ""


def _synthetic_lines(n: int) -> str:
    return "x\n" * max(0, n)


def _edit_calls(raw_name: str, tool: str, data: dict):
    """Yield ``(accumulator_tool, input, is_new_file)`` for one successful edit part.

    * ``apply_patch`` has no oldString/newString: its input is a single
      ``patchText``; per-file ``additions/deletions`` come from
      ``state.metadata.files`` (fallback: parse the patch text).
    * ``edit`` prefers ``state.metadata.filediff`` (authoritative diff counts,
      also right for ``replaceAll``), falling back to oldString/newString.
    * ``write`` replays ``content``; ``metadata.exists is False`` marks a new file.
    """
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    lower = raw_name.lower().strip()

    if lower in ("apply_patch", "patch"):
        files = meta.get("files")
        entries: list[tuple[str, str, int, int]] = []  # (path, type, adds, dels)
        if isinstance(files, list) and files:
            for f in files:
                if not isinstance(f, dict):
                    continue
                fp = _first_str(f.get("movePath"), f.get("filePath"), f.get("relativePath"))
                if fp:
                    entries.append((fp, str(f.get("type") or "update"),
                                    _as_int(f.get("additions")), _as_int(f.get("deletions"))))
        else:
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            entries = _parse_patch_text(inp.get("patchText"))
        for fp, ftype, adds, dels in entries:
            if ftype == "add":
                yield "Write", {"file_path": fp, "content": _synthetic_lines(adds)}, True
            else:
                yield "Edit", {"file_path": fp, "new_string": _synthetic_lines(adds),
                               "old_string": _synthetic_lines(dels)}, False
        return

    inp = _normalize_tool_input(data)
    fd = meta.get("filediff")
    if tool == "Edit" and isinstance(fd, dict) and ("additions" in fd or "deletions" in fd):
        fp = _first_str(fd.get("file"), inp.get("file_path"))
        if fp:
            yield "Edit", {"file_path": fp,
                           "new_string": _synthetic_lines(_as_int(fd.get("additions"))),
                           "old_string": _synthetic_lines(_as_int(fd.get("deletions")))}, False
            return
    yield tool, inp, (tool == "Write" and meta.get("exists") is False)


_PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")
_PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")


def _parse_patch_text(text) -> list[tuple[str, str, int, int]]:
    """Fallback ``apply_patch`` envelope parser -> ``[(path, type, adds, dels)]``."""
    if not isinstance(text, str):
        return []
    out: list[list] = []
    for line in text.splitlines():
        m = _PATCH_FILE_RE.match(line)
        if m:
            out.append([m.group(2), m.group(1).lower(), 0, 0])
            continue
        if not out:
            continue
        mv = _PATCH_MOVE_RE.match(line)
        if mv:
            out[-1][0] = mv.group(1)
        elif line.startswith("+"):
            out[-1][2] += 1
        elif line.startswith("-"):
            out[-1][3] += 1
    # Delete File blocks carry no -lines in the envelope; size is unknown -> 0.
    return [(p, t, a, d) for p, t, a, d in out]


def session_loc_full(db_path: Path, session_id: str):
    """Return LOC for one OpenCode session (summary counters or tool replay)."""
    return _loc_scan(db_path, session_id)[0]


def has_loc_signal(db_path: Path, session_id: str) -> bool:
    """True if summary counters or parseable edit tool parts exist."""
    try:
        return _loc_scan(db_path, session_id)[1]
    except FileNotFoundError:
        return False


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
    # Old TCER ``dbname:project_id`` key format (pre-``::``; not an OpenCode format)
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


def _add_session_tokens(u: TokenUsage, row: sqlite3.Row, model: str,
                        msg_buckets: dict[str, list[int]] | None = None) -> None:
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
        if msg_buckets and len(msg_buckets) > 1:
            # Mixed-model session: price each model's own message tokens; the
            # residual of the session counters (rounding / unattributed steps)
            # stays on the session model so totals still match the row.
            res = [i, cw, cr, o]
            for mk, b in msg_buckets.items():
                u.bucket(mk).add(*b)
                res = [r - x for r, x in zip(res, b)]
            res = [max(0, r) for r in res]
            if any(res):
                u.bucket(model or next(iter(msg_buckets))).add(*res)
        else:
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


def _note_step_turn_stat(u: TokenUsage, data: dict, turn: int, ts: int | None) -> None:
    """step-finish 快照 → 时间线 TurnStat（仅展示用，不参与聚合 token）。"""
    tok = data.get("tokens")
    if not isinstance(tok, dict):
        return
    i = _as_int(tok.get("input"))
    o = _as_int(tok.get("output")) + _as_int(tok.get("reasoning"))
    cache = tok.get("cache")
    cr = _as_int(cache.get("read")) if isinstance(cache, dict) else 0
    cw = _as_int(cache.get("write")) if isinstance(cache, dict) else 0
    if i + o + cr + cw <= 0:
        return
    u.turn_stats.append(TurnStat(
        turn=turn, ts=ts, input_tokens=i, cache_write=cw,
        cache_read=cr, output_tokens=o,
        user_turn=u.user_msgs if u.user_msgs > 0 else 1,
    ))


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
    "shell": "Bash",       # v2 rename of bash (dev branch)
    "pwsh": "Bash",
    "powershell": "Bash",
    "cmd": "Bash",
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
    "subagent": "Task",    # v2 rename of task (dev branch)
    "skill": "Skill",
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




def _is_image_mime(value) -> bool:
    return isinstance(value, str) and value.lower().startswith("image/")
