"""Devin (Cognition) local-session reader.

Devin Desktop / Devin CLI keep two stores under the Devin data directory
(``%APPDATA%\\devin`` on Windows, ``~/.local/share/devin`` on Linux,
``DEVIN_SESSIONS_DIR`` env override, legacy ``cognition`` dirs):

1. ``cli/sessions.db`` — canonical metered store. ``sessions`` rows carry
   ``working_directory`` / ``title`` / model; ``message_nodes.chat_message``
   holds one JSON chat message per node of a per-session forest.
   ``metadata.metrics`` carries per-response counters
   (``input_tokens``/``output_tokens``/``cache_read_tokens``/
   ``cache_creation_tokens``/``context_input_tokens``/``ttft_ms``/
   ``total_time_ms``/``generation_model``).  One API response is stored as
   several nodes (text node + tool-call nodes) all repeating the same
   ``request_id`` and metrics — usage is deduplicated **by request_id**
   (same rule as Claude ``message.id``).  ``tool_call_state`` stores the
   ACP ToolCall and its final ToolCallUpdate; edit/write updates embed
   ``content[].type=="diff"`` with authoritative full-file
   ``oldText``/``newText`` — the cleanest possible LOC replay (no F1
   uncertainty). ``subagent_heads`` points at subagent chain heads inside
   the same node forest, so subagent cost is naturally folded.

2. ``User/acp-messages/<hash>.db`` — per-conversation ACP transcripts
   written by the desktop shell, indexed by
   ``User/globalStorage/state.vscdb`` keys
   ``windsurf.acp.messageStore.session.acp/devin-cli/<name>`` (local
   sessions) and ``.../devin-cloud/.../devin-<uuid>`` (cloud sessions).
   Transcripts carry messages/tool calls/ACU events but **no token
   usage** — transcript-only sessions expose content metrics (prompts,
   tools, LOC, timeline) while token metrics stay unsupported.  A
   transcript whose ``devin-cli`` name is already a ``sessions.db`` row is
   skipped (sessions.db is authoritative; counting both would double-count).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from tcer.core import pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import as_int as _as_int, first_str as _first_str
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_MS_STORE_PREFIX = "windsurf.acp.messageStore.session.acp/"


@dataclass(frozen=True)
class DevinSession:
    """Session handle for the Devin adapter.

    ``kind == "db"``: metered row in ``cli/sessions.db`` (``key`` = session id).
    ``kind == "acp"``: transcript-only ``User/acp-messages/<key>.db`` —
    ``cloud`` marks ``devin-cloud`` (cloud Devin) vs ``devin-cli`` residue.
    ``ms_key`` is the messageStore key used for cloud/transcript titling.
    """
    kind: str
    db_path: Path
    key: str
    cloud: bool = False
    ms_key: str = ""


# --------------------------------------------------------------------------- #
# Roots / discovery
# --------------------------------------------------------------------------- #

def _devin_roots() -> list[Path]:
    """Candidate Devin data roots (each may hold ``cli/sessions.db``)."""
    roots: list[Path] = []
    env = os.environ.get("DEVIN_SESSIONS_DIR")
    if env:
        # The env var points at the directory containing sessions.db
        # (typically ``<root>/cli``); normalize to the root. Exclusive —
        # consistent with other sources' env overrides (test isolation).
        p = Path(env)
        return [p.parent if p.name == "cli" else p]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "devin")
    home = Path.home()
    roots.append(home / ".local" / "share" / "devin")
    roots.append(home / ".config" / "devin")
    if sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "devin")
    # Legacy cognition dirs (pre-rename; the app leaves compat symlinks).
    for base in (home / ".local" / "share" / "cognition",
                 home / ".config" / "cognition"):
        roots.append(base)
    out: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        try:
            k = r.resolve()
        except OSError:
            k = r
        if k not in seen:
            out.append(r)
            seen.add(k)
    return out


def discover_databases() -> list[Path]:
    """All ``cli/sessions.db`` files across roots."""
    out: list[Path] = []
    for root in _devin_roots():
        for cand in (root / "cli" / "sessions.db", root / "sessions.db"):
            if cand.is_file() and cand not in out:
                out.append(cand)
    return out


def _acp_dirs() -> list[Path]:
    """``User/acp-messages`` dirs across roots (transcript stores)."""
    return [root / "User" / "acp-messages" for root in _devin_roots()]


def _global_state_dbs() -> list[Path]:
    """``User/globalStorage/state.vscdb`` paths (messageStore index)."""
    return [root / "User" / "globalStorage" / "state.vscdb" for root in _devin_roots()]


def _connect(path: Path) -> sqlite3.Connection:
    """Read-only SQLite connection with a busy timeout (live writers)."""
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
    if len(s) >= 2 and s[1] == ":":          # 工作区盘符统一大写（Antigravity 同口径）
        s = s[0].upper() + s[1:]
    return s


# --------------------------------------------------------------------------- #
# messageStore index (state.vscdb) → acp transcript map
# --------------------------------------------------------------------------- #

def _message_store() -> dict[str, dict]:
    """``{ms_session_key: {"uuid": acp_hash, "lastUpdated": ms}}`` merged across roots.

    Keys look like ``acp/devin-cli/<name>`` or
    ``acp/devin-cloud/devin-session-cache/1/user-*/account-*/devin-cloud/devin-<hex>``.
    """
    out: dict[str, dict] = {}
    for db_path in _global_state_dbs():
        if not db_path.is_file():
            continue
        try:
            with _connect(db_path) as con:
                if not _has_table(con, "ItemTable"):
                    continue
                rows = con.execute(
                    "select key, value from ItemTable where key like ?",
                    (_MS_STORE_PREFIX + "%",),
                ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            key = str(row["key"])
            if not key.startswith(_MS_STORE_PREFIX):
                continue
            sess_key = key[len(_MS_STORE_PREFIX):]
            val = _json_obj(row["value"])
            if isinstance(val, dict) and val.get("uuid"):
                prev = out.get(sess_key)
                if prev is None or _as_int(val.get("lastUpdated")) >= _as_int(prev.get("lastUpdated")):
                    out[sess_key] = val
    return out


def _acp_db_for(uuid_hash: str) -> Path | None:
    for d in _acp_dirs():
        cand = d / f"{uuid_hash}.db"
        if cand.is_file():
            return cand
    return None


def _acp_message_count(db_path: Path) -> int:
    try:
        with _connect(db_path) as con:
            row = con.execute("select value from meta where key='message_count'").fetchone()
            if row:
                return _as_int(row["value"])
            return con.execute("select count(*) from messages").fetchone()[0]
    except sqlite3.Error:
        return 0


# --------------------------------------------------------------------------- #
# Project / session enumeration
# --------------------------------------------------------------------------- #

def _db_sessions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _has_table(con, "sessions"):
        return []
    return con.execute(
        "select id, working_directory, backend_type, model, agent_mode, "
        "created_at, last_activity_at, title, hidden, metadata from sessions "
        "where hidden = 0 order by created_at, id"
    ).fetchall()


def _acp_call(payload: dict) -> dict:
    """The ACP ToolCall object inside a ``tool_call`` message payload."""
    c = payload.get("content")
    return c if isinstance(c, dict) else payload


def _iter_tool_calls(acp_db: Path):
    """Yield ACP ToolCall dicts from a transcript db."""
    try:
        with _connect(acp_db) as con:
            if not _has_table(con, "messages"):
                return
            for (payload,) in con.execute(
                    "select payload from messages where kind='tool_call'"):
                o = _json_obj(payload)
                yield o, _acp_call(o)
    except sqlite3.Error:
        return


def _transcript_cwd(acp_db: Path) -> str:
    """Majority ``cognition.ai/cwd`` over tool calls (local transcripts only —
    cloud sessions run in ``/home/ubuntu`` containers, their cwd is not a
    workspace identity)."""
    from collections import Counter
    votes: Counter[str] = Counter()
    for _o, call in _iter_tool_calls(acp_db):
        meta = call.get("_meta") or {}
        cwd = meta.get("cognition.ai/cwd")
        if isinstance(cwd, str) and cwd:
            votes[_norm_cwd(cwd)] += 1
    return votes.most_common(1)[0][0] if votes else ""


def _transcript_repo(acp_db: Path) -> str:
    """``org/repo`` for cloud sessions: majority ``cognition.ai/repoName``,
    fallback ``info.configOptions[Repositories].currentValue``."""
    from collections import Counter
    votes: Counter[str] = Counter()
    for _o, call in _iter_tool_calls(acp_db):
        meta = call.get("_meta") or {}
        repo = meta.get("cognition.ai/repoName")
        if isinstance(repo, str) and repo:
            votes[repo] += 1
    if votes:
        return votes.most_common(1)[0][0]
    try:
        with _connect(acp_db) as con:
            row = con.execute("select value from meta where key='info'").fetchone()
    except sqlite3.Error:
        return ""
    info = _json_obj(row["value"]) if row else {}
    for opt in info.get("configOptions") or []:
        if isinstance(opt, dict) and opt.get("id") == "repo_id":
            return str(opt.get("currentValue") or "")
        if isinstance(opt, dict) and "repositor" in str(opt.get("name", "")).lower():
            return str(opt.get("currentValue") or "")
    return ""


# ``sourceEventType`` values that are progress/telemetry events, not tools.
_ACP_NOISE_EVENTS = frozenset({
    "acu_consumption_at_last_user_interaction",
    "acu_consumption_summary",
    "context_growth_update",
    "session_analysis",
    "rules_injected",
    "session_title_update",
    "attachment_live_update",
    "terminal_update",
    "shell_interpretation_failed",
    "environment_config_suggestion",
    "auto_snapshot",
    "user_message_queued",
    "permission_request",
    "permission_resolved",
    "options_prompt",
    "slack_connect_prompt",
    "local_access_requested",
    "devin_message",
    "devin_suspended",
    "review_started",
    "review_findings_available",
    "session_analysis_update",
})

# sourceEventType → canonical tool (cloud transcripts).
_ACP_EVENT_TOOL = {
    "shell_process_started": "Bash",
    "shell_process_completed": "Bash",
    "git_push": "Bash",
    "git_commit": "Bash",
    "web_search": "WebSearch",
    "web_get_contents": "WebFetch",
    "search_file_commands": "Grep",
    "memory_drive_synced": "Edit",
    "image_generation": "image_generation",
    "mcp_tool_call": "mcp",
    "mcp_tool_call_started": "mcp",
    "computer_use": "computer_use",
    "ask_user_question": "AskUserQuestion",
    "skill_activated": "Skill",
    "git_view_pr": "Bash",
    "git_pr_checks": "Bash",
    "pr_close": "Bash",
    "stop_command": "Bash",
    "wait_command_start": "Bash",
}

_ACP_KIND_TOOL = {
    "execute": "Bash",
    "edit": "Edit",
    "read": "Read",
    "fetch": "WebFetch",
    "search": "Grep",
    "other": "",
}


def _acp_tool(payload: dict, call: dict) -> str:
    """Canonical tool name for a transcript ToolCall, "" for non-tool events."""
    ev = _first_str(payload.get("sourceEventType"),
                    (call.get("_meta") or {}).get("cognition.ai/eventType"))
    if ev in _ACP_NOISE_EVENTS:
        return ""
    meta = call.get("_meta") or {}
    name = meta.get("cognition.ai/inferenceToolName")
    if isinstance(name, str) and name:
        return _DEVIN_TOOL_MAP.get(name.lower(), name)
    if ev and ev in _ACP_EVENT_TOOL:
        return _ACP_EVENT_TOOL[ev]
    if ev == "multi_edit_result":
        kind = call.get("kind")
        return "Edit" if kind == "edit" else "Read"
    kind = str(call.get("kind") or "").lower()
    if kind in _ACP_KIND_TOOL:
        return _ACP_KIND_TOOL[kind] or ""
    # Local transcripts may lack kind: title verb prefix is the fallback.
    title = str(call.get("title") or "")
    if title.startswith("Ran "):
        return "Bash"
    if title.startswith(("Edited ", "Edit ")):
        return "Edit"
    if title.startswith("Read "):
        return "Read"
    if title.startswith("Searched"):
        return "Grep"
    return ""


def _acp_call_path(call: dict) -> str:
    for src in (call.get("rawInput"), call.get("_meta") or {}):
        if isinstance(src, dict):
            p = _first_str(src.get("file_path"), src.get("filePath"),
                           src.get("path"), src.get("cognition.ai/filePath"))
            if p:
                return p
    for fu in (call.get("_meta") or {}).get("cognition.ai/fileUpdates") or []:
        if isinstance(fu, dict) and isinstance(fu.get("file_path"), str):
            return fu["file_path"]
    return ""


_NO_CWD = "__devin_no_cwd__"


def list_project_refs() -> list[ProjectRef]:
    """Group Devin sessions by workspace: local cwd / cloud repo."""
    by_key: dict[str, dict] = {}
    db_sids: set[str] = set()
    for db in discover_databases():
        try:
            with _connect(db) as con:
                rows = _db_sessions(con)
        except sqlite3.Error:
            continue
        for r in rows:
            db_sids.add(str(r["id"]))
            cwd = _norm_cwd(r["working_directory"]) or _NO_CWD
            slot = by_key.setdefault(f"dir::{cwd}", {"db": db, "n": 0})
            slot["n"] += 1
            if slot["db"] is None:
                slot["db"] = db
    # Transcript layer: cloud sessions + local transcripts not in sessions.db.
    for ms_key, val in _message_store().items():
        cloud = ms_key.startswith("devin-cloud/")
        name = ms_key.rsplit("/", 1)[-1]
        if not cloud and name in db_sids:
            continue                                  # sessions.db is authoritative
        acp = _acp_db_for(str(val["uuid"]))
        if acp is None or _acp_message_count(acp) <= 0:
            continue
        if cloud:
            repo = _transcript_repo(acp)
            key = f"repo::{repo}" if repo else "repo::"
        else:
            cwd = _transcript_cwd(acp) or _NO_CWD
            key = f"dir::{cwd}"
        by_key.setdefault(key, {"db": None, "n": 0})["n"] += 1
    refs: list[ProjectRef] = []
    for key, slot in sorted(by_key.items()):
        kind, _, val = key.partition("::")
        if kind == "repo":
            disp = val if val else "云端会话"
            refs.append(ProjectRef(
                source="devin", key=f"devin::cloud::{val}",
                display_name=disp, cwd=None, path=slot["db"]))
        elif val != _NO_CWD:
            refs.append(ProjectRef(
                source="devin", key=f"devin::{val}",
                display_name=Path(val).name or val, cwd=val, path=slot["db"]))
        else:
            refs.append(ProjectRef(
                source="devin", key="devin::(none)",
                display_name="Devin 未知项目", cwd=None, path=slot["db"]))
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


def sessions_for_project(project: str | ProjectRef) -> list[DevinSession]:
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    if ref is None:
        return []
    # Cloud refs group by repo (cwd is None); local refs by normalized cwd.
    is_cloud_ref = ref.key.startswith("devin::cloud::")
    repo_key = ref.key[len("devin::cloud::"):] if is_cloud_ref else ""
    cwd = _norm_cwd(ref.cwd)
    handles: list[DevinSession] = []
    db_sids: set[str] = set()
    all_sids: set[str] = set()
    for db in discover_databases():
        try:
            with _connect(db) as con:
                rows = _db_sessions(con)
        except sqlite3.Error:
            continue
        for r in rows:
            sid = str(r["id"])
            all_sids.add(sid)
            if is_cloud_ref:
                continue                            # cloud project → no db rows
            if _norm_cwd(r["working_directory"]) != cwd:
                continue
            db_sids.add(sid)
            handles.append(DevinSession("db", db, sid))
    for ms_key, val in _message_store().items():
        cloud = ms_key.startswith("devin-cloud/")
        name = ms_key.rsplit("/", 1)[-1]
        if not cloud and name in all_sids:
            continue
        acp = _acp_db_for(str(val["uuid"]))
        if acp is None or _acp_message_count(acp) <= 0:
            continue
        if is_cloud_ref:
            if not cloud or _transcript_repo(acp) != repo_key:
                continue
        elif cloud:
            continue                                # cloud lives under repo keys
        elif _transcript_cwd(acp) != cwd:
            continue
        handles.append(DevinSession("acp", acp, str(val["uuid"]), cloud=cloud,
                                    ms_key=ms_key))
    handles.sort(key=lambda h: (h.kind != "db", h.key))
    return handles


def latest_activity_ms(project: str | ProjectRef) -> int | None:
    ref = project if isinstance(project, ProjectRef) else resolve_project(project)
    if ref is None:
        return None
    cwd = _norm_cwd(ref.cwd)
    best: int | None = None
    for db in discover_databases():
        try:
            with _connect(db) as con:
                rows = con.execute(
                    "select working_directory, last_activity_at from sessions "
                    "where hidden = 0").fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            if cwd and _norm_cwd(r["working_directory"]) != cwd:
                continue
            v = parse_timestamp_ms(r["last_activity_at"] * 1000
                                   if isinstance(r["last_activity_at"], (int, float))
                                   else None)
            if v and (best is None or v > best):
                best = v
    for h in sessions_for_project(ref):
        if h.kind != "acp":
            continue
        v = _acp_last_ms(h.db_path)
        if v and (best is None or v > best):
            best = v
    return best


# --------------------------------------------------------------------------- #
# Session meta
# --------------------------------------------------------------------------- #

def read_session_meta(db_path: Path, session_id: str) -> SessionMeta:
    """Meta for a ``sessions.db`` row."""
    with _connect(db_path) as con:
        row = con.execute(
            "select id, working_directory, backend_type, model, agent_mode, "
            "title, hidden, metadata from sessions where id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError(f"Devin session '{session_id}' not found")
    meta = _json_obj(row["metadata"])
    model = _first_str(row["model"], meta.get("model") if isinstance(meta, dict) else None)
    return SessionMeta(
        session_id=session_id,
        cwd=_norm_cwd(row["working_directory"]) or None,
        title=row["title"] or None,
        path=db_path,
        is_subagent=False,
        entrypoint=_first_str(row["backend_type"], "devin"),
        source="devin",
        model_provider=pricing.normalize(model) if model else None,
        approval_policy=row["agent_mode"] or None,
    )


def _acp_first_user_text(acp_db: Path) -> str:
    try:
        with _connect(acp_db) as con:
            row = con.execute(
                "select payload from messages where kind='user_message' "
                "order by position limit 1").fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    for c in _json_obj(row["payload"]).get("content") or []:
        if isinstance(c, dict):
            inner = c.get("content")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                return truncate_summary(inner["text"].strip(), 120)
    return ""


def _acp_model(acp_db: Path) -> str:
    try:
        with _connect(acp_db) as con:
            row = con.execute("select value from meta where key='info'").fetchone()
    except sqlite3.Error:
        return ""
    info = _json_obj(row["value"]) if row else {}
    for opt in info.get("configOptions") or []:
        if isinstance(opt, dict) and opt.get("category") == "model":
            return str(opt.get("currentValue") or "")
    return ""


def read_acp_meta(h: DevinSession) -> SessionMeta:
    """Meta for a transcript-only session (cloud or local residue)."""
    title = _acp_first_user_text(h.db_path)
    # 云端会话用卡片上的云图标区分（thread_source="cloud"），不在标题里塞文字
    label = ("「转录」" if not h.cloud else "") + (title or h.key[:12])
    model = _acp_model(h.db_path)
    return SessionMeta(
        session_id=h.key,
        cwd=_transcript_cwd(h.db_path) or None,
        title=label,
        path=h.db_path,
        is_subagent=False,
        entrypoint="devin-cloud" if h.cloud else "devin",
        source="devin",
        model_provider=pricing.normalize(model) if model else None,
        thread_source="cloud" if h.cloud else "transcript",
    )


def session_key_of(project: str | ProjectRef, h: DevinSession) -> str:
    return h.key


# --------------------------------------------------------------------------- #
# Usage aggregation — sessions.db (metered)
# --------------------------------------------------------------------------- #

def _metric_int(metrics: dict, key: str) -> int:
    return _as_int(metrics.get(key)) if isinstance(metrics, dict) else 0


def aggregate_usage(handle: DevinSession) -> TokenUsage:
    if handle.kind == "acp":
        return _aggregate_acp(handle.db_path)
    return _aggregate_db(handle.db_path, handle.key)


def _aggregate_db(db_path: Path, session_id: str) -> TokenUsage:
    u = TokenUsage()
    with _connect(db_path) as con:
        srow = con.execute(
            "select model, created_at, last_activity_at from sessions where id = ?",
            (session_id,),
        ).fetchone()
        if srow is None:
            raise FileNotFoundError(f"Devin session '{session_id}' not found")
        nodes = con.execute(
            "select node_id, chat_message, created_at from message_nodes "
            "where session_id = ? order by node_id", (session_id,),
        ).fetchall() if _has_table(con, "message_nodes") else []
        calls = con.execute(
            "select tool_call_id, tool_call_json, tool_call_update_json, "
            "source_node_ids from tool_call_state where session_id = ?",
            (session_id,),
        ).fetchall() if _has_table(con, "tool_call_state") else []

    sess_model = pricing.normalize(srow["model"]) if srow["model"] else ""
    if sess_model:
        u.models.add(sess_model)
    u.started_at = parse_timestamp_ms(srow["created_at"] * 1000
                                    if isinstance(srow["created_at"], (int, float)) else None)
    u.ended_at = parse_timestamp_ms(srow["last_activity_at"] * 1000
                                    if isinstance(srow["last_activity_at"], (int, float)) else None)
    if u.started_at and u.ended_at:
        u.session_duration_ms = max(0, u.ended_at - u.started_at)

    # request_id → merged metrics (per-field max: identical duplicates stay
    # idempotent, streaming snapshots take the latest values).
    booked: dict[str, dict] = {}
    node_req: dict[int, str] = {}      # node_id → request_id (tool-op turn mapping)
    req_turn: dict[str, int] = {}      # request_id → turn index
    req_ts: dict[str, int | None] = {}
    req_model: dict[str, str] = {}
    req_calls: dict[str, int] = {}
    turn = -1
    for row in nodes:
        m = _json_obj(row["chat_message"])
        if not isinstance(m, dict):
            continue
        md = m.get("metadata") or {}
        role = m.get("role")
        nid = row["node_id"]
        ts_ms = parse_timestamp_ms(row["created_at"] * 1000
                                   if isinstance(row["created_at"], (int, float))
                                   else md.get("created_at"))
        if role == "user":
            tele = md.get("telemetry") or {}
            if md.get("is_user_input") is True and tele.get("source") == "user":
                text = m.get("content")
                text = text if isinstance(text, str) else ""
                u.user_msgs += 1
                if text.strip():
                    u.user_message_texts.append(truncate_summary(text.strip(), 500))
                    if not u.first_prompt_chars:
                        u.first_prompt_chars = len(text.strip())
                    if text.strip().startswith("/"):
                        u.slash_command_count += 1
            continue
        if role != "assistant":
            continue

        rid = md.get("request_id") or ""
        metrics = md.get("metrics") or {}
        i = _metric_int(metrics, "input_tokens")
        o = _metric_int(metrics, "output_tokens")
        cr = _metric_int(metrics, "cache_read_tokens")
        cw = _metric_int(metrics, "cache_creation_tokens")
        ctx = _metric_int(metrics, "context_input_tokens") or (i + cr + cw)
        if rid and rid in booked:
            # Duplicate node for the same response: merge max per field
            # (streaming snapshots grow monotonically; identical stays put).
            rec = booked[rid]
            rec["i"] = max(rec["i"], i)
            rec["o"] = max(rec["o"], o)
            rec["cr"] = max(rec["cr"], cr)
            rec["cw"] = max(rec["cw"], cw)
            rec["ctx"] = max(rec["ctx"], ctx)
            rec["dur"] = max(rec["dur"], _metric_int(metrics, "total_time_ms"))
            rec["ttft"] = max(rec["ttft"], _metric_int(metrics, "ttft_ms"))
            tc = len(m.get("tool_calls") or [])
            rec["calls"] = max(rec["calls"], tc)
            node_req[nid] = rid
            continue
        turn += 1
        if rid:
            booked[rid] = {"i": i, "o": o, "cr": cr, "cw": cw, "ctx": ctx,
                           "dur": _metric_int(metrics, "total_time_ms"),
                           "ttft": _metric_int(metrics, "ttft_ms"),
                           "calls": len(m.get("tool_calls") or []),
                           "turn": turn}
            node_req[nid] = rid
            req_turn[rid] = turn
            req_ts[rid] = ts_ms
            req_calls[rid] = len(m.get("tool_calls") or [])
        else:
            # No dedup anchor → count individually (same as Claude id-less lines).
            booked[f"__noid_{nid}"] = {
                "i": i, "o": o, "cr": cr, "cw": cw, "ctx": ctx,
                "dur": _metric_int(metrics, "total_time_ms"),
                "ttft": _metric_int(metrics, "ttft_ms"),
                "calls": len(m.get("tool_calls") or []), "turn": turn}
            req_ts[f"__noid_{nid}"] = ts_ms
            req_calls[f"__noid_{nid}"] = len(m.get("tool_calls") or [])
        gm = _first_str(md.get("generation_model"), sess_model)
        req_model[rid or f"__noid_{nid}"] = pricing.normalize(gm) if gm else ""
        if i + o + cr + cw <= 0:
            u.empty_usage_skipped += 1

    for rid, rec in booked.items():
        i, o, cr, cw = rec["i"], rec["o"], rec["cr"], rec["cw"]
        u.input_tokens += i
        u.output_tokens += o
        u.cache_read_input_tokens += cr
        u.cache_creation_input_tokens += cw
        if i + o + cr + cw <= 0:
            continue
        u.assistant_msgs += 1
        mkey = req_model.get(rid, "")
        if mkey:
            u.models.add(mkey)
        u.bucket(mkey).add(i, cw, cr, o)
        u.peak_input_tokens = max(u.peak_input_tokens, rec["ctx"])
        dur = rec["dur"] or None
        if rec["ttft"]:
            u.ttft_ms_samples.append(rec["ttft"])
        u.add_est_gen_sample(o, dur)
        u.turn_stats.append(TurnStat(
            turn=rec["turn"], ts=req_ts.get(rid), input_tokens=i,
            cache_write=cw, cache_read=cr, output_tokens=o,
            duration_ms=dur, tool_calls=rec["calls"], model=mkey,
        ))

    # Tool calls: tool_call_state is keyed by (session_id, tool_call_id) —
    # already dedup'd; the ToolCallUpdate carries the verdict status.
    for row in calls:
        call = _json_obj(row["tool_call_json"])
        upd = _json_obj(row["tool_call_update_json"])
        name = _tool_name(call, upd)
        tool, path = _classify_tool(name, call, upd)
        u.tool_calls[tool] = u.tool_calls.get(tool, 0) + 1
        t_turn = req_turn.get(node_req.get(_source_node(row), -1) or "", 0)
        u.tool_ops.append(ToolOp(t_turn, tool, path))
        status = str(upd.get("status") or "").lower()
        if status and status != "completed":
            u.tool_errors += 1
            u.tool_errors_by_tool[tool] = u.tool_errors_by_tool.get(tool, 0) + 1
        if tool == "WebSearch":
            u.web_search_count += 1
            u.web_search_end_count += 1
    return u


def _source_node(row: sqlite3.Row) -> int:
    """First node id from ``source_node_ids`` (JSON array / CSV)."""
    v = row["source_node_ids"]
    if isinstance(v, str):
        arr = _json_obj(v)
        if isinstance(arr, list) and arr:
            return _as_int(arr[0])
        try:
            return int(v.split(",")[0])
        except (ValueError, IndexError):
            return -1
    return _as_int(v)


def _tool_name(call: dict, upd: dict) -> str:
    meta = upd.get("_meta") or call.get("_meta") or {}
    name = meta.get("cognition.ai/inferenceToolName")
    if isinstance(name, str) and name:
        return name
    title = _first_str(call.get("title"), upd.get("title"))
    return title or ""


_DEVIN_TOOL_MAP = {
    "exec": "Bash",
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "grep": "Grep",
    "find_file_by_name": "Glob",
    "find_code_context": "Grep",
    "webfetch": "WebFetch",
    "web_search": "WebSearch",
    "run_subagent": "Task",
    "skill": "Skill",
    "todo_write": "TodoWrite",
    "ask_user_question": "AskUserQuestion",
    "notebook_edit": "NotebookEdit",
    "mcp_call_tool": "mcp",
    "mcp_list_tools": "mcp",
}


def _classify_tool(name: str, call: dict, upd: dict) -> tuple[str, str]:
    lower = name.lower().strip()
    path = ""
    for src in (upd.get("rawInput"), call.get("rawInput"), call.get("input")):
        if isinstance(src, dict):
            p = _first_str(src.get("file_path"), src.get("filePath"),
                           src.get("path"), src.get("notebook_path"))
            if p:
                path = p
                break
    mapped = _DEVIN_TOOL_MAP.get(lower)
    if mapped:
        return mapped, path
    if lower.startswith("mcp__") or lower.startswith("mcp_"):
        return name, path                    # MCP 原名计入工具画像
    for canon in ("Read", "Edit", "Write", "Grep", "Glob", "Bash", "Task",
                  "WebSearch", "WebFetch", "TodoWrite"):
        if lower == canon.lower():
            return canon, path
    return name or "Tool", path


# --------------------------------------------------------------------------- #
# Usage aggregation — acp transcripts (no token metering)
# --------------------------------------------------------------------------- #

def _aggregate_acp(acp_db: Path) -> TokenUsage:
    u = TokenUsage()
    with _connect(acp_db) as con:
        if not _has_table(con, "messages"):
            return u
        rows = con.execute(
            "select kind, payload from messages order by position").fetchall()
    seen_calls: set[str] = set()
    turn = -1
    for row in rows:
        o = _json_obj(row["payload"])
        kind = row["kind"]
        ts = parse_timestamp_ms(_as_int(o.get("sourceEventTimestampMs"))
                                or _as_int((o.get("_meta") or {}).get("cognition.ai/createdAtMs")))
        if ts:
            u.started_at = min(u.started_at, ts) if u.started_at else ts
            u.ended_at = max(u.ended_at, ts) if u.ended_at else ts
        if kind == "user_message":
            text = ""
            for c in o.get("content") or []:
                if isinstance(c, dict):
                    inner = c.get("content")
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        text += inner["text"]
            u.user_msgs += 1
            if text.strip():
                u.user_message_texts.append(truncate_summary(text.strip(), 500))
                if not u.first_prompt_chars:
                    u.first_prompt_chars = len(text.strip())
                if text.strip().startswith("/"):
                    u.slash_command_count += 1
        elif kind == "agent_thought":
            u.thinking_count += 1
        elif kind == "tool_call":
            call = _acp_call(o)
            cid = _first_str(call.get("toolCallId"), call.get("id"), o.get("id"))
            if cid and cid in seen_calls:
                continue
            if cid:
                seen_calls.add(cid)
            tool = _acp_tool(o, call)
            if not tool:
                continue                  # progress/telemetry event, not a tool
            u.tool_calls[tool] = u.tool_calls.get(tool, 0) + 1
            u.tool_ops.append(ToolOp(max(0, turn), tool, _acp_call_path(call)))
            status = str(call.get("status") or "").lower()
            if status in ("failed", "error"):
                u.tool_errors += 1
                u.tool_errors_by_tool[tool] = u.tool_errors_by_tool.get(tool, 0) + 1
            if tool == "WebSearch":
                u.web_search_count += 1
                u.web_search_end_count += 1
        elif kind == "agent_message":
            turn += 1                      # display-turn proxy (no usage data)
            u.empty_usage_skipped += 1     # 真实成本存在但本地无计量
    if u.started_at and u.ended_at:
        u.session_duration_ms = max(0, u.ended_at - u.started_at)
    return u


def _acp_last_ms(acp_db: Path) -> int | None:
    try:
        st = acp_db.stat()
        return int(st.st_mtime_ns // 1_000_000)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# LOC replay
# --------------------------------------------------------------------------- #

def _loc_scan(handle: DevinSession, meta: SessionMeta):
    """``(SessionLoc, has_signal)`` — diff-content replay, successes only."""
    from tcer.core.loc import SessionLoc, _LocAccumulator
    acc = _LocAccumulator()
    saw = False
    if handle.kind == "db":
        try:
            with _connect(handle.db_path) as con:
                rows = con.execute(
                    "select tool_call_json, tool_call_update_json from "
                    "tool_call_state where session_id = ?",
                    (handle.key,),
                ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            upd = _json_obj(row["tool_call_update_json"])
            if str(upd.get("status") or "").lower() != "completed":
                continue
            saw = _replay_diffs(acc, upd.get("content") or upd) or saw
    else:
        seen_calls: set[str] = set()
        for o, call in _iter_tool_calls(handle.db_path):
            cid = _first_str(call.get("toolCallId"), call.get("id"), o.get("id"))
            if cid and cid in seen_calls:
                continue
            if cid:
                seen_calls.add(cid)
            if str(call.get("status") or "").lower() != "completed":
                continue
            saw = _replay_file_updates(acc, call) or saw
            saw = _replay_diffs(acc, call.get("content") or call) or saw
    return acc.finish(), saw


def _replay_file_updates(acc, call: dict) -> bool:
    """Cloud ``multi_edit_result``: ``_meta.fileUpdates`` carries authoritative
    ``lines_added``/``lines_removed`` per file (the diff text itself is stored
    externally by ``contentsKey``). Replayed as count-preserving synthetic
    edits — same net-delta convention as the accumulator's Edit."""
    meta = call.get("_meta") or {}
    fus = meta.get("cognition.ai/fileUpdates")
    if not isinstance(fus, list) or not fus:
        return False
    saw = False
    for fu in fus:
        if not isinstance(fu, dict):
            continue
        fp = fu.get("file_path")
        if not isinstance(fp, str) or not fp:
            continue
        a = _as_int(fu.get("lines_added"))
        d = _as_int(fu.get("lines_removed"))
        if not a and not d:
            continue
        saw = True
        if fu.get("action_type") == "create":
            acc.on_tool_use("Write", {"file_path": fp, "content": "x\n" * a})
            acc.note_write_original(fp, "")
        else:
            acc.on_tool_use("Edit", {"file_path": fp,
                                     "old_string": "x\n" * d,
                                     "new_string": "x\n" * a})
    return saw


def _replay_diffs(acc, content) -> bool:
    """Replay ``content[].type=="diff"`` entries (authoritative old/new)."""
    saw = False
    items = content if isinstance(content, list) else [content]
    for c in items:
        if not isinstance(c, dict) or c.get("type") != "diff":
            continue
        path = c.get("path")
        if not isinstance(path, str) or not path:
            continue
        old = c.get("oldText") if isinstance(c.get("oldText"), str) else None
        new = c.get("newText") if isinstance(c.get("newText"), str) else ""
        saw = True
        if old is None:
            # New file → Write semantics (old=0 is confirmed, not assumed:
            # note_write_original with "" resolves the unseen flag).
            acc.on_tool_use("Write", {"file_path": path, "content": new})
            acc.note_write_original(path, "")
        else:
            # Full-file before/after → net line delta of the real change.
            acc.on_tool_use("Edit", {"file_path": path,
                                     "old_string": old, "new_string": new})
    return saw


def session_loc_full(handle: DevinSession):
    return _loc_scan(handle, read_session_meta(handle.db_path, handle.key)
                     if handle.kind == "db" else read_acp_meta(handle))[0]


def subagents_of(handle: DevinSession) -> int:
    if handle.kind != "db":
        return 0
    try:
        with _connect(handle.db_path) as con:
            if not _has_table(con, "subagent_heads"):
                return 0
            return con.execute(
                "select count(*) from subagent_heads where session_id = ?",
                (handle.key,),
            ).fetchone()[0]
    except sqlite3.Error:
        return 0


# --------------------------------------------------------------------------- #
# User messages (user_input SSOT 口径)
# --------------------------------------------------------------------------- #

def read_user_messages(db_path: Path, session_id: str) -> list[str]:
    return [t for t, _ in read_user_messages_timed(db_path, session_id)]


def read_user_messages_timed(db_path: Path, session_id: str) -> list[tuple[str, int | None]]:
    """真人提问：role=user ∧ is_user_input ∧ telemetry.source=user。"""
    out: list[tuple[str, int | None]] = []
    with _connect(db_path) as con:
        if not _has_table(con, "message_nodes"):
            return []
        rows = con.execute(
            "select chat_message, created_at from message_nodes "
            "where session_id = ? order by node_id", (session_id,),
        ).fetchall()
    for row in rows:
        m = _json_obj(row["chat_message"])
        if m.get("role") != "user":
            continue
        md = m.get("metadata") or {}
        tele = md.get("telemetry") or {}
        if md.get("is_user_input") is not True or tele.get("source") != "user":
            continue
        text = m.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        ts = parse_timestamp_ms(row["created_at"] * 1000
                                if isinstance(row["created_at"], (int, float)) else None)
        out.append((truncate_summary(text.strip(), 500), ts))
    return out


def read_acp_user_messages_timed(acp_db: Path) -> list[tuple[str, int | None]]:
    out: list[tuple[str, int | None]] = []
    try:
        with _connect(acp_db) as con:
            rows = con.execute(
                "select payload from messages where kind='user_message' "
                "order by position").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        o = _json_obj(row["payload"])
        meta = o.get("_meta") or {}
        ts = parse_timestamp_ms(_as_int(o.get("sourceEventTimestampMs"))
                                or _as_int(meta.get("cognition.ai/createdAtMs")))
        text = ""
        for c in o.get("content") or []:
            if isinstance(c, dict):
                inner = c.get("content")
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    text += inner["text"]
        if text.strip():
            out.append((truncate_summary(text.strip(), 500), ts))
    return out


# --------------------------------------------------------------------------- #
# Conversation view (阅读器)
# --------------------------------------------------------------------------- #

def read_conversation(db_path: Path, session_id: str) -> list[dict]:
    """Ordered conversation for a metered session (deduped by message_id)."""
    convo: list[dict] = []
    with _connect(db_path) as con:
        rows = con.execute(
            "select node_id, chat_message, created_at from message_nodes "
            "where session_id = ? order by node_id", (session_id,),
        ).fetchall() if _has_table(con, "message_nodes") else []
    seen_mid: set[str] = set()
    for row in rows:
        m = _json_obj(row["chat_message"])
        if not isinstance(m, dict):
            continue
        mid = m.get("message_id")
        if isinstance(mid, str) and mid and mid in seen_mid:
            continue
        if mid:
            seen_mid.add(mid)
        role = m.get("role")
        ts = parse_timestamp_ms(row["created_at"] * 1000
                                if isinstance(row["created_at"], (int, float)) else None)
        if role in ("user", "assistant"):
            text = m.get("content")
            if isinstance(text, str) and text.strip():
                convo.append({"role": role, "type": "text",
                              "text": text.strip(), "ts": ts})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                name = _DEVIN_TOOL_MAP.get(str(tc.get("name") or "").lower(),
                                           str(tc.get("name") or "Tool"))
                convo.append({"role": "assistant", "type": "tool_use",
                              "name": name, "id": tc.get("id"),
                              "input": tc.get("arguments")
                              if isinstance(tc.get("arguments"), dict) else {},
                              "ts": ts})
        elif role == "tool":
            convo.append({"role": "tool", "type": "tool_result",
                          "tool_use_id": None,
                          "is_error": False,
                          "text": m.get("content") if isinstance(m.get("content"), str) else "",
                          "ts": ts})
    return convo


def read_acp_conversation(acp_db: Path) -> list[dict]:
    """Ordered conversation for a transcript-only session."""
    convo: list[dict] = []
    try:
        with _connect(acp_db) as con:
            if not _has_table(con, "messages"):
                return []
            rows = con.execute(
                "select kind, payload from messages order by position").fetchall()
    except sqlite3.Error:
        return []
    seen_calls: set[str] = set()
    for row in rows:
        o = _json_obj(row["payload"])
        kind = row["kind"]
        ts = parse_timestamp_ms(_as_int(o.get("sourceEventTimestampMs"))
                                or _as_int((o.get("_meta") or {}).get("cognition.ai/createdAtMs")))
        if kind == "user_message":
            text = ""
            for c in o.get("content") or []:
                if isinstance(c, dict):
                    inner = c.get("content")
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        text += inner["text"]
            if text.strip():
                convo.append({"role": "user", "type": "text",
                              "text": text.strip(), "ts": ts})
        elif kind == "agent_message":
            text = ""
            for c in o.get("content") or []:
                if isinstance(c, dict):
                    inner = c.get("content")
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        text += inner["text"]
            if text.strip():
                convo.append({"role": "assistant", "type": "text",
                              "text": text.strip(), "ts": ts})
        elif kind == "agent_thought":
            text = ""
            for c in o.get("content") or []:
                if isinstance(c, dict):
                    inner = c.get("content")
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        text += inner["text"]
            if text.strip():
                convo.append({"role": "assistant", "type": "thinking",
                              "text": text.strip(), "ts": ts})
        elif kind == "tool_call":
            call = _acp_call(o)
            cid = _first_str(call.get("toolCallId"), call.get("id"), o.get("id"))
            if cid and cid in seen_calls:
                continue
            if cid:
                seen_calls.add(cid)
            tool = _acp_tool(o, call) or str(call.get("title") or "Tool")
            status = str(call.get("status") or "").lower()
            convo.append({
                "role": "assistant", "type": "tool_use", "name": tool,
                "id": cid,
                "input": call.get("rawInput")
                if isinstance(call.get("rawInput"), dict) else {},
                "ts": ts,
            })
            out = call.get("rawOutput")
            convo.append({
                "role": "tool", "type": "tool_result", "tool_use_id": cid,
                "is_error": status in ("failed", "error"),
                "text": out if isinstance(out, str)
                else (json.dumps(out, ensure_ascii=False, default=str)
                      if out is not None else str(call.get("title") or "")),
                "ts": ts,
            })
    return convo
