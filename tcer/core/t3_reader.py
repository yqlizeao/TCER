"""T3 Code (statev2) local-session reader.

T3 Code is an orchestration workbench: it drives provider instances
(``acpRegistry_devin`` → real Devin sessions, ``antigravity``, ``claudeAgent``,
``codex``, …) and persists an event-sourced projection in
``~/.t3/userdata/statev2.sqlite`` (``T3_STATE_DB`` override).

Relevant projection tables:

* ``projection_projects`` — workspace grouping (``workspace_root``).
* ``orchestration_v2_projection_threads`` — one row per chat thread;
  ``payload_json.modelSelection.model`` carries the model id.
* ``orchestration_v2_projection_provider_turns`` — provider turns;
  ``payload_json.turnTokenUsage`` is per-turn usage with an explicit
  ``usageStatus`` (``complete`` → real counters; ``unavailable`` → the
  provider does not report → TCER shows 不适用, never fabricates).
* ``orchestration_v2_projection_turn_items`` — ordered items:
  ``user_message`` / ``assistant_message`` / ``reasoning`` /
  ``command_execution`` (``exitCode``) / ``dynamic_tool`` (``toolName``+``input``)
  / ``file_change`` (``fileName``+``diffStr`` unified diff → LOC replay) /
  ``file_search`` / ``todo_list`` / ``checkpoint`` / ``notification``.
* ``orchestration_v2_projection_subagents`` — ``child_thread_id`` folds a
  spawned subagent thread into its parent.

**Skip list**: ``import:*`` threads (``import:claudeAgent:*`` etc.) are
imported copies of external sessions — counting them would double-count the
originating source.  ``usage-scan-cache-v17.json`` is likewise ignored (T3's
secondhand cache of other providers' files).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from tcer.core import pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import as_int as _as_int, first_str as _first_str
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_T3_THREADS = "orchestration_v2_projection_threads"
_T3_PTURNS = "orchestration_v2_projection_provider_turns"
_T3_ITEMS = "orchestration_v2_projection_turn_items"
_T3_PSESSIONS = "orchestration_v2_projection_provider_sessions"
_T3_SUBAGENTS = "orchestration_v2_projection_subagents"
_T3_PROJECTS = "projection_projects"

_SKIP_ITEM_TYPES = frozenset({"checkpoint", "notification"})


# --------------------------------------------------------------------------- #
# Roots / discovery
# --------------------------------------------------------------------------- #

def t3_state_db() -> Path | None:
    """Path to ``statev2.sqlite`` (``T3_STATE_DB`` override → ``~/.t3/userdata``)."""
    env = os.environ.get("T3_STATE_DB")
    if env:
        p = Path(env)
        return p if p.is_file() else None
    cand = Path.home() / ".t3" / "userdata" / "statev2.sqlite"
    return cand if cand.is_file() else None


def _connect(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    try:
        uri = resolved.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (sqlite3.OperationalError, OSError):
        con = sqlite3.connect(str(resolved), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,),
    ).fetchone() is not None


def _json_obj(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _norm_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    s = os.path.normpath(str(cwd).strip())
    if len(s) >= 2 and s[1] == ":":
        s = s[0].upper() + s[1:]
    return s


def _ts(value) -> int | None:
    """T3 stores ISO-8601 strings everywhere."""
    return parse_timestamp_ms(value)


# --------------------------------------------------------------------------- #
# Projects / sessions
# --------------------------------------------------------------------------- #

def list_project_refs() -> list[ProjectRef]:
    db = t3_state_db()
    if db is None:
        return []
    refs: list[ProjectRef] = []
    with _connect(db) as con:
        if not _has_table(con, _T3_PROJECTS):
            return []
        rows = con.execute(
            "select project_id, title, workspace_root from projection_projects "
            "where deleted_at is null and archived_at is null "
            "order by title, project_id").fetchall()
        for r in rows:
            n = con.execute(
                f"select count(*) from {_T3_THREADS} where project_id = ? "
                "and deleted_at is null and thread_id not like 'import:%'",
                (r["project_id"],),
            ).fetchone()[0]
            if n <= 0:
                continue
            cwd = _norm_cwd(r["workspace_root"]) or None
            title = r["title"] if isinstance(r["title"], str) and r["title"] else None
            refs.append(ProjectRef(
                source="t3",
                key=f"t3::{r['project_id']}",
                display_name=title or (Path(cwd).name if cwd else "T3 未知项目"),
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


def _project_id(ref: ProjectRef) -> str:
    return ref.key[len("t3::"):] if ref.key.startswith("t3::") else ref.key


def sessions_for_project(project: str | ProjectRef) -> list[str]:
    """Thread ids for the project (imported copies excluded)."""
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    db = t3_state_db()
    if ref is None or db is None:
        return []
    pid = _project_id(ref)
    with _connect(db) as con:
        if not _has_table(con, _T3_THREADS):
            return []
        rows = con.execute(
            f"select thread_id from {_T3_THREADS} where project_id = ? "
            "and deleted_at is null and thread_id not like 'import:%' "
            "order by created_at, thread_id",
            (pid,),
        ).fetchall()
    return [str(r["thread_id"]) for r in rows]


def latest_activity_ms(project: str | ProjectRef) -> int | None:
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    db = t3_state_db()
    if ref is None or db is None:
        return None
    with _connect(db) as con:
        if not _has_table(con, _T3_THREADS):
            return None
        row = con.execute(
            f"select max(updated_at) as m from {_T3_THREADS} where project_id = ? "
            "and deleted_at is null and thread_id not like 'import:%'",
            (_project_id(ref),),
        ).fetchone()
    return _ts(row["m"]) if row and row["m"] else None


# --------------------------------------------------------------------------- #
# Session meta / subagents
# --------------------------------------------------------------------------- #

def read_session_meta(db_path: Path, thread_id: str) -> SessionMeta:
    with _connect(db_path) as con:
        row = con.execute(
            f"select t.thread_id, t.title, t.default_provider, t.payload_json, "
            f"t.provider_instance_id, p.workspace_root, p.title as ptitle "
            f"from {_T3_THREADS} t left join {_T3_PROJECTS} p "
            f"on p.project_id = t.project_id where t.thread_id = ?",
            (thread_id,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError(f"T3 thread '{thread_id}' not found")
    payload = _json_obj(row["payload_json"])
    sel = payload.get("modelSelection") if isinstance(payload.get("modelSelection"), dict) else {}
    model = _first_str(sel.get("model"))
    provider = _first_str(row["provider_instance_id"], row["default_provider"])
    return SessionMeta(
        session_id=thread_id,
        cwd=_norm_cwd(row["workspace_root"]) or None,
        title=row["title"] or None,
        path=db_path,
        is_subagent=False,
        entrypoint="t3",
        source="t3",
        model_provider=pricing.normalize(model) if model else None,
        thread_source=provider,
    )


def subagent_ids(db_path: Path, thread_id: str) -> list[str]:
    """``child_thread_id`` values recorded for this thread."""
    try:
        with _connect(db_path) as con:
            if not _has_table(con, _T3_SUBAGENTS):
                return []
            rows = con.execute(
                f"select child_thread_id from {_T3_SUBAGENTS} "
                "where thread_id = ? and child_thread_id is not null "
                "order by started_at",
                (thread_id,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(r["child_thread_id"]) for r in rows]


def subagents_of(db_path: Path, thread_id: str) -> int:
    return len(subagent_ids(db_path, thread_id))


# --------------------------------------------------------------------------- #
# Usage aggregation
# --------------------------------------------------------------------------- #

# T3 item type / dynamic tool name → canonical tool.
_T3_TOOL_MAP = {
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "multiedit": "Edit",
    "multi_edit": "Edit",
    "notebookedit": "NotebookEdit",
    "notebook_edit": "NotebookEdit",
    "exec": "Bash",
    "bash": "Bash",
    "shell": "Bash",
    "run_terminal_command": "Bash",
    "runcommand": "Bash",
    "run_command": "Bash",
    "command": "Bash",
    "grep": "Grep",
    "search": "Grep",
    "file_search": "Grep",
    "find_file_by_name": "Glob",
    "glob": "Glob",
    "ls": "Glob",
    "list": "Glob",
    "read_terminal": "Read",
    "view": "Read",
    "webfetch": "WebFetch",
    "web_fetch": "WebFetch",
    "websearch": "WebSearch",
    "web_search": "WebSearch",
    "fetch": "WebFetch",
    "task": "Task",
    "run_subagent": "Task",
    "subagent": "Task",
    "todowrite": "TodoWrite",
    "todo_write": "TodoWrite",
    "todo_list": "TodoWrite",
    "skill": "Skill",
    "askuserquestion": "AskUserQuestion",
    "ask_user_question": "AskUserQuestion",
    "mcp_call_tool": "mcp",
}

_T3_ITEM_TOOL = {
    "command_execution": "Bash",
    "file_search": "Glob",
    "file_change": "Edit",
    "todo_list": "TodoWrite",
}


def _canonical_tool(raw: str, item_type: str, payload: dict) -> str:
    """Map a T3 item to a canonical tool name ("" → not a real tool call)."""
    if item_type == "dynamic_tool":
        name = _first_str(payload.get("toolName"), payload.get("name"))
        if not name:
            nref = payload.get("nativeItemRef")
            nid = nref.get("nativeId") if isinstance(nref, dict) else None
            if isinstance(nid, str) and ":" in nid:
                name = nid.split(":", 1)[0]
        if not name:
            return "Tool"
        lower = str(name).lower().strip()
        if lower in _T3_TOOL_MAP:
            return _T3_TOOL_MAP[lower]
        if lower.startswith("mcp__") or lower.startswith("mcp_"):
            return str(name)
        for canon in ("Read", "Edit", "Write", "Grep", "Glob", "Bash", "Task",
                      "WebSearch", "WebFetch", "TodoWrite"):
            if lower == canon.lower():
                return canon
        return str(name)
    if item_type == "file_search":
        nref = payload.get("nativeItemRef")
        nid = nref.get("nativeId") if isinstance(nref, dict) else None
        if isinstance(nid, str) and ":" in nid:
            head = nid.split(":", 1)[0].lower()
            if head in _T3_TOOL_MAP:
                return _T3_TOOL_MAP[head]
        return "Glob"
    return _T3_ITEM_TOOL.get(item_type, "")


def _item_path(payload: dict) -> str:
    fp = _first_str(payload.get("fileName"), payload.get("file_path"),
                    payload.get("filePath"), payload.get("path"))
    if fp:
        return fp
    inp = payload.get("input")
    if isinstance(inp, dict):
        for k in ("file_path", "filePath", "path", "Path", "notebook_path"):
            v = inp.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def aggregate_usage(db_path: Path, thread_id: str) -> TokenUsage:
    u = _aggregate_single(db_path, thread_id)
    for child in subagent_ids(db_path, thread_id):
        try:
            u = u.merge(_aggregate_single(db_path, child))
        except FileNotFoundError:
            continue
    return u


def _aggregate_single(db_path: Path, thread_id: str) -> TokenUsage:
    u = TokenUsage()
    with _connect(db_path) as con:
        trow = con.execute(
            f"select title, created_at, updated_at, payload_json "
            f"from {_T3_THREADS} where thread_id = ?", (thread_id,),
        ).fetchone()
        if trow is None:
            raise FileNotFoundError(f"T3 thread '{thread_id}' not found")
        ptrows = con.execute(
            f"select provider_turn_id, ordinal, status, started_at, "
            f"completed_at, payload_json from {_T3_PTURNS} "
            "where thread_id = ? order by ordinal",
            (thread_id,),
        ).fetchall() if _has_table(con, _T3_PTURNS) else []
        items = con.execute(
            f"select type, status, provider_turn_id, payload_json "
            f"from {_T3_ITEMS} where thread_id = ? order by ordinal",
            (thread_id,),
        ).fetchall() if _has_table(con, _T3_ITEMS) else []
        srow = con.execute(
            f"select model, payload_json from {_T3_PSESSIONS} "
            "where thread_id = ? order by updated_at limit 1",
            (thread_id,),
        ).fetchone() if _has_table(con, _T3_PSESSIONS) else None

    payload = _json_obj(trow["payload_json"])
    sel = payload.get("modelSelection") if isinstance(payload.get("modelSelection"), dict) else {}
    spayload = _json_obj(srow["payload_json"]) if srow else {}
    model = _first_str(sel.get("model"), srow["model"] if srow else None,
                       spayload.get("model") if isinstance(spayload, dict) else None)
    model = pricing.normalize(model) if model else ""
    if model:
        u.models.add(model)
    u.started_at = _ts(trow["created_at"])
    u.ended_at = _ts(trow["updated_at"])
    if u.started_at and u.ended_at:
        u.session_duration_ms = max(0, u.ended_at - u.started_at)

    # ── provider turns: usage (有则计) + turn index mapping ──
    # 生成耗时估算：provider_turn 的 completed−started 是整轮墙钟（含工具执行，
    # 与 Claude turn_duration 同类，不能当补全耗时）；改用回合内
    # assistant_message+reasoning 项自身的 startedAt→completedAt 之和。
    gen_ms_of: dict[str, int] = {}
    for r in items:
        if str(r["type"] or "") not in ("assistant_message", "reasoning"):
            continue
        o = _json_obj(r["payload_json"])
        s, e = _ts(o.get("startedAt")), _ts(o.get("completedAt"))
        pid = str(r["provider_turn_id"] or o.get("providerTurnId") or "")
        if pid and s and e and e > s:
            gen_ms_of[pid] = gen_ms_of.get(pid, 0) + (e - s)
    turn_of: dict[str, int] = {}
    turn_no = -1
    for r in ptrows:
        p = _json_obj(r["payload_json"])
        usage = p.get("turnTokenUsage") if isinstance(p.get("turnTokenUsage"), dict) else None
        pid = str(r["provider_turn_id"])
        turn_no += 1
        turn_of[pid] = turn_no
        if not usage or usage.get("usageStatus") != "complete":
            # usageStatus unavailable → 真实成本存在但本地无计量（显示不适用）
            u.empty_usage_skipped += 1
            continue
        i = _as_int(usage.get("inputTokens"))
        cr = _as_int(usage.get("cachedInputTokens"))
        cw = _as_int(usage.get("cacheCreationTokens"))
        o = _as_int(usage.get("outputTokens"))
        rsn = _as_int(usage.get("reasoningTokens"))
        # T3/anthropic convention: cachedInput/cacheCreation are subsets of
        # inputTokens (same as Claude); reasoning rides inside output.
        net_i = max(0, i - cr - cw)
        if i + o + cr + cw + rsn <= 0:
            u.empty_usage_skipped += 1
            continue
        turn = turn_no
        u.input_tokens += net_i
        u.output_tokens += o
        u.cache_read_input_tokens += cr
        u.cache_creation_input_tokens += cw
        u.reasoning_output_tokens += rsn
        u.assistant_msgs += 1
        u.bucket(model).add(net_i, cw, cr, o)
        ctx = i + cw  # inputTokens 可能已含 cache_write；峰值取大者
        u.peak_input_tokens = max(u.peak_input_tokens, ctx)
        start = _ts(r["started_at"])
        end = _ts(r["completed_at"])
        dur = (end - start) if (start and end and end > start) else None
        u.add_est_gen_sample(o, gen_ms_of.get(pid))
        u.turn_stats.append(TurnStat(
            turn=turn, ts=start, input_tokens=net_i, cache_write=cw,
            cache_read=cr, output_tokens=o, duration_ms=dur,
            model=model,
        ))

    # ── turn items: prompts / tools / thinking / errors / timeline ──
    for r in items:
        itype = str(r["type"] or "")
        if itype in _SKIP_ITEM_TYPES:
            continue
        o = _json_obj(r["payload_json"])
        status = str(r["status"] or o.get("status") or "").lower()
        pturn = str(r["provider_turn_id"] or o.get("providerTurnId") or "")
        turn = turn_of.get(pturn, 0)
        if itype == "user_message":
            text = o.get("text") if isinstance(o.get("text"), str) else ""
            if text.strip():
                u.user_msgs += 1
                u.user_message_texts.append(truncate_summary(text.strip(), 500))
                if not u.first_prompt_chars:
                    u.first_prompt_chars = len(text.strip())
                if text.strip().startswith("/"):
                    u.slash_command_count += 1
            continue
        if itype == "reasoning":
            u.thinking_count += 1
            continue
        if itype in ("assistant_message", "plan"):
            continue
        tool = _canonical_tool(str(o.get("toolName") or ""), itype, o)
        if not tool:
            continue
        u.tool_calls[tool] = u.tool_calls.get(tool, 0) + 1
        u.tool_ops.append(ToolOp(turn, tool, _item_path(o)))
        failed = status in ("failed", "error")
        if itype == "command_execution":
            ec = o.get("exitCode")
            if ec is not None and _as_int(ec) != 0:
                failed = True
        if failed:
            u.tool_errors += 1
            u.tool_errors_by_tool[tool] = u.tool_errors_by_tool.get(tool, 0) + 1
        if tool == "WebSearch":
            u.web_search_count += 1
            if status == "completed":
                u.web_search_end_count += 1
    return u


# --------------------------------------------------------------------------- #
# LOC replay (file_change.diffStr, completed only)
# --------------------------------------------------------------------------- #

_DIFF_ADD = re.compile(r"^\+(?!\+\+)")
_DIFF_DEL = re.compile(r"^-(?!--)")


def _loc_scan(db_path: Path, thread_id: str, meta: SessionMeta):
    """``(SessionLoc, has_signal)``: replay successful file_change diffs."""
    from tcer.core.loc import _LocAccumulator, merge_session_locs
    acc, sig = _loc_scan_single(db_path, thread_id)
    children = subagent_ids(db_path, thread_id)
    if not children:
        return acc, sig
    slocs = [acc]
    for child in children:
        try:
            sl, s = _loc_scan_single(db_path, child)
        except FileNotFoundError:
            continue
        slocs.append(sl)
        sig = sig or s
    return merge_session_locs(slocs), sig


def _loc_scan_single(db_path: Path, thread_id: str):
    from tcer.core.loc import _LocAccumulator
    acc = _LocAccumulator()
    saw = False
    try:
        with _connect(db_path) as con:
            if not _has_table(con, _T3_ITEMS):
                return acc.finish(), False
            rows = con.execute(
                f"select status, payload_json from {_T3_ITEMS} "
                "where thread_id = ? and type = 'file_change' order by ordinal",
                (thread_id,),
            ).fetchall()
    except sqlite3.Error:
        return acc.finish(), False
    for r in rows:
        if str(r["status"] or "").lower() != "completed":
            continue                                  # 结果裁决：失败不回放
        o = _json_obj(r["payload_json"])
        diff = o.get("diffStr")
        fp = _item_path(o)
        if not isinstance(diff, str) or not diff.strip() or not fp:
            continue
        adds = dels = 0
        for line in diff.splitlines():
            if _DIFF_ADD.match(line):
                adds += 1
            elif _DIFF_DEL.match(line):
                dels += 1
        if not adds and not dels:
            continue
        saw = True
        acc.on_tool_use("Edit", {"file_path": fp,
                                 "old_string": "x\n" * dels,
                                 "new_string": "x\n" * adds})
    return acc.finish(), saw


def session_loc_full(db_path: Path, thread_id: str):
    meta = read_session_meta(db_path, thread_id)
    return _loc_scan(db_path, thread_id, meta)[0]


def has_loc_signal(db_path: Path, thread_id: str) -> bool:
    meta = read_session_meta(db_path, thread_id)
    return _loc_scan(db_path, thread_id, meta)[1]


# --------------------------------------------------------------------------- #
# User messages (user_input SSOT 口径)
# --------------------------------------------------------------------------- #

def read_user_messages(db_path: Path, thread_id: str) -> list[str]:
    return [t for t, _ in read_user_messages_timed(db_path, thread_id)]


def read_user_messages_timed(db_path: Path, thread_id: str) -> list[tuple[str, int | None]]:
    out: list[tuple[str, int | None]] = []
    with _connect(db_path) as con:
        if not _has_table(con, _T3_ITEMS):
            return []
        rows = con.execute(
            f"select payload_json from {_T3_ITEMS} where thread_id = ? "
            "and type = 'user_message' order by ordinal",
            (thread_id,),
        ).fetchall()
    for r in rows:
        o = _json_obj(r["payload_json"])
        text = o.get("text") if isinstance(o.get("text"), str) else ""
        if not text.strip():
            continue
        out.append((truncate_summary(text.strip(), 500),
                    _ts(o.get("startedAt"))))
    return out


# --------------------------------------------------------------------------- #
# Conversation view
# --------------------------------------------------------------------------- #

def read_conversation(db_path: Path, thread_id: str) -> list[dict]:
    """Ordered conversation: text / thinking / tool_use / tool_result."""
    convo: list[dict] = []
    with _connect(db_path) as con:
        if not _has_table(con, _T3_ITEMS):
            return []
        rows = con.execute(
            f"select type, status, payload_json from {_T3_ITEMS} "
            "where thread_id = ? order by ordinal", (thread_id,),
        ).fetchall()
    for r in rows:
        o = _json_obj(r["payload_json"])
        itype = str(r["type"] or "")
        ts = _ts(o.get("startedAt"))
        if itype in _SKIP_ITEM_TYPES:
            continue
        if itype == "user_message":
            text = o.get("text")
            if isinstance(text, str) and text.strip():
                convo.append({"role": "user", "type": "text",
                              "text": text.strip(), "ts": ts})
        elif itype == "assistant_message":
            text = o.get("text")
            if isinstance(text, str) and text.strip():
                convo.append({"role": "assistant", "type": "text",
                              "text": text.strip(), "ts": ts})
        elif itype == "reasoning":
            text = o.get("text")
            if isinstance(text, str) and text.strip():
                convo.append({"role": "assistant", "type": "thinking",
                              "text": text.strip(), "ts": ts})
        elif itype == "todo_list":
            continue
        else:
            tool = _canonical_tool(str(o.get("toolName") or ""), itype, o)
            if not tool:
                continue
            cid = _first_str(o.get("id"),
                             (o.get("nativeItemRef") or {}).get("nativeId")
                             if isinstance(o.get("nativeItemRef"), dict) else None)
            convo.append({
                "role": "assistant", "type": "tool_use", "name": tool,
                "id": cid,
                "input": o.get("input") if isinstance(o.get("input"), dict)
                else {"command": o.get("input")} if o.get("input") else {},
                "ts": ts,
            })
            out = o.get("output")
            status = str(r["status"] or o.get("status") or "").lower()
            if itype == "file_change" and not out:
                out = o.get("diffStr")
            convo.append({
                "role": "tool", "type": "tool_result", "tool_use_id": cid,
                "is_error": status in ("failed", "error")
                or (itype == "command_execution" and _as_int(o.get("exitCode")) != 0
                    and o.get("exitCode") is not None),
                "text": out if isinstance(out, str)
                else json.dumps(out, ensure_ascii=False, default=str)
                if out is not None else "",
                "ts": _ts(o.get("completedAt")),
            })
    return convo
