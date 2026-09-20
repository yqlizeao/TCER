"""Google Antigravity CLI (agy) local-session reader.

Antigravity CLI stores sessions under ``~/.gemini/antigravity-cli``:
  * ``conversation_summaries.db``: SQLite database cataloging all conversations,
    their workspace URIs (``workspace_uris``, e.g. ``["file:///C:/GitHub/TCER"]``),
    step counts, last modified timestamps, and title/preview texts.
  * ``conversations/<conversation_id>.db``: Per-session SQLite database containing:
    - ``trajectory_meta``: Cascade / trajectory metadata.
    - ``trajectory_metadata_blob``: Raw protobuf blob containing workspace URI and project id.
    - ``gen_metadata``: LLM generation responses in protobuf format. Authoritative
      source of token counts (input, cached input, output, thinking/reasoning) and
      model names (e.g. ``gemini-3.8-flash``).
    - ``steps``: Chronological execution steps in protobuf format:
      * step_type == 14: User input (prompts).
      * step_type == 15: Assistant turns / tool calls (``write_to_file``,
        ``replace_file_content``, ``view_file``, ``run_command``, etc.).
      * step_type == 132: Tool execution results and exit codes.

This module implements a zero-dependency, pure Python protobuf wire format decoder
to parse Antigravity's SQLite databases into TCER's canonical models (``TokenUsage``,
``SessionMeta``, ``SessionLoc``, and ``ProjectRef``). Read-only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tcer.core import loc, pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import (
    as_int as _as_int,
    is_correction as _is_correction,
    is_slash_command as _is_slash_command,
)
from tcer.core.paths import antigravity_conversations_dir, antigravity_dir

# Antigravity tool name -> TCER canonical tool name
_ANTIGRAVITY_TOOL_MAP = {
    "view_file": "Read",
    "read_file": "Read",
    "write_to_file": "Write",
    "write_file": "Write",
    "replace_file_content": "Edit",
    "edit_file": "Edit",
    "run_command": "Bash",
    "manage_task": "Task",
    "search_web": "WebSearch",
    "read_url_content": "WebFetch",
    "ask_question": "Ask",
    "generate_image": "Image",
}


# --------------------------------------------------------------------------- #
# Lightweight pure-Python Protobuf wire format decoder (Zero Dependencies)
# --------------------------------------------------------------------------- #

def _parse_proto_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Decode raw protobuf wire format into a list of (field_num, wire_type, value).

    Supported wire types:
      0: varint (value is int)
      1: 64-bit (value is 8 bytes)
      2: length-delimited (value is bytes)
      5: 32-bit (value is 4 bytes)
    """
    pos = 0
    n = len(data)
    res = []
    while pos < n:
        shift = 0
        tag = 0
        while pos < n:
            b = data[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        if pos > n:
            break
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            res.append((field_num, 0, val))
        elif wire_type == 1:  # 64-bit
            val = data[pos:pos + 8]
            pos += 8
            res.append((field_num, 1, val))
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while pos < n:
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            val = data[pos:pos + length]
            pos += length
            res.append((field_num, 2, val))
        elif wire_type == 5:  # 32-bit
            val = data[pos:pos + 4]
            pos += 4
            res.append((field_num, 5, val))
        else:
            # Unknown wire type or stream corrupted; break to avoid infinite loop
            break
    return res


def _find_field(fields: list[tuple[int, int, int | bytes]], field_num: int):
    """Return the first value matching field_num, or None."""
    for f, _, val in fields:
        if f == field_num:
            return val
    return None


def _find_all_fields(fields: list[tuple[int, int, int | bytes]], field_num: int) -> list:
    """Return all values matching field_num."""
    return [val for f, _, val in fields if f == field_num]


def _decode_timestamp(meta_blob: bytes | None) -> float | None:
    """Extract unix timestamp (seconds as float) from google.protobuf.Timestamp blob."""
    if not meta_blob:
        return None
    fields = _parse_proto_fields(meta_blob)
    ts_blob = _find_field(fields, 1)
    if not isinstance(ts_blob, bytes):
        return None
    ts_fields = _parse_proto_fields(ts_blob)
    sec = _find_field(ts_fields, 1) or 0
    nanos = _find_field(ts_fields, 2) or 0
    if isinstance(sec, int) and isinstance(nanos, int):
        return sec + nanos / 1e9
    return None


def _format_timestamp(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Step payload decoders
# --------------------------------------------------------------------------- #

def _decode_gen_metadata(data: bytes) -> tuple[str, int, int, int, int]:
    """Return (model, uncached_input, total_output, cached_input, reasoning_tokens)."""
    fields = _parse_proto_fields(data)
    inner_blob = _find_field(fields, 1)
    if not isinstance(inner_blob, bytes):
        return ("", 0, 0, 0, 0)
    inner_fields = _parse_proto_fields(inner_blob)

    # model in field 19
    model_bytes = _find_field(inner_fields, 19)
    model = ""
    if isinstance(model_bytes, bytes):
        try:
            model = model_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass

    # usage in field 4
    usage_blob = _find_field(inner_fields, 4)
    if not isinstance(usage_blob, bytes):
        return (model, 0, 0, 0, 0)

    u_fields = _parse_proto_fields(usage_blob)
    uncached_input = _find_field(u_fields, 2) or 0
    total_output = _find_field(u_fields, 3) or 0
    cached_input = _find_field(u_fields, 5) or 0
    reasoning = _find_field(u_fields, 9) or 0

    return (
        model,
        int(uncached_input) if isinstance(uncached_input, int) else 0,
        int(total_output) if isinstance(total_output, int) else 0,
        int(cached_input) if isinstance(cached_input, int) else 0,
        int(reasoning) if isinstance(reasoning, int) else 0,
    )


def _extract_user_text(payload: bytes | None) -> str:
    """Extract user prompt text from a step_type == 14 step_payload."""
    if not payload:
        return ""
    fields = _parse_proto_fields(payload)
    f19 = _find_field(fields, 19)
    if isinstance(f19, bytes):
        sub_fields = _parse_proto_fields(f19)
        txt = _find_field(sub_fields, 2)
        if isinstance(txt, bytes):
            return txt.decode("utf-8", errors="ignore").strip()

    # Fallback: scan length-delimited fields for non-proto plain text
    for _, wt, val in fields:
        if wt == 2 and isinstance(val, bytes):
            sub = _parse_proto_fields(val)
            for _, swt, sval in sub:
                if swt == 2 and isinstance(sval, bytes):
                    try:
                        s = sval.decode("utf-8", errors="ignore").strip()
                        if (
                            len(s) > 1
                            and not s.startswith("{")
                            and not s.startswith("type.googleapis.com")
                            and not s.startswith("file:///")
                        ):
                            return s
                    except Exception:
                        pass
    return ""


def _extract_tool_call(payload: bytes | None) -> tuple[str, str, str]:
    """Extract (tool_name, tool_args_json, call_id) from a step_type == 15 payload."""
    if not payload:
        return ("", "", "")
    fields = _parse_proto_fields(payload)
    f20 = _find_field(fields, 20)
    if isinstance(f20, bytes):
        sub20 = _parse_proto_fields(f20)
        f7 = _find_field(sub20, 7)
        if isinstance(f7, bytes):
            sf7 = _parse_proto_fields(f7)
            call_id_b = _find_field(sf7, 1)
            tool_name_b = _find_field(sf7, 2)
            tool_args_b = _find_field(sf7, 3)
            call_id = call_id_b.decode("utf-8", errors="ignore") if isinstance(call_id_b, bytes) else ""
            tool_name = tool_name_b.decode("utf-8", errors="ignore") if isinstance(tool_name_b, bytes) else ""
            tool_args = tool_args_b.decode("utf-8", errors="ignore") if isinstance(tool_args_b, bytes) else ""
            return (tool_name, tool_args, call_id)
    return ("", "", "")


def _extract_tool_result(payload: bytes | None) -> tuple[str, int]:
    """Extract (output_text, exit_code) from a step_type == 132 payload."""
    if not payload:
        return ("", 0)
    fields = _parse_proto_fields(payload)
    f140 = _find_field(fields, 140)
    out_text = ""
    exit_code = 0
    if isinstance(f140, bytes):
        sf140 = _parse_proto_fields(f140)
        f2 = _find_field(sf140, 2)
        if isinstance(f2, bytes):
            sf2 = _parse_proto_fields(f2)
            out_b = _find_field(sf2, 1)
            if isinstance(out_b, bytes):
                out_text = out_b.decode("utf-8", errors="ignore")
                m = re.search(r"exited with code (\d+)", out_text)
                if m:
                    exit_code = int(m.group(1))
    return (out_text, exit_code)


# --------------------------------------------------------------------------- #
# Workspace URI & Project Resolution
# --------------------------------------------------------------------------- #

def _normalize_workspace_uri(uri: str) -> str:
    """Normalize file:/// URI to a local directory path."""
    if not uri:
        return ""
    raw = uri
    if raw.startswith("file:///"):
        raw = raw[8:]
    elif raw.startswith("file://"):
        raw = raw[7:]
    unquoted = urllib.parse.unquote(raw)
    p = Path(unquoted)
    try:
        # Resolve to handle drive letters and backslashes uniformly
        return str(p.resolve())
    except Exception:
        return str(p)


def _project_display_name(workspace_dir: str) -> str:
    """Return a concise display name for a workspace path."""
    if not workspace_dir:
        return "Antigravity 项目"
    p = Path(workspace_dir)
    return p.name or str(p)


def _connect_ro(path: Path | str) -> sqlite3.Connection:
    """Open SQLite connection in immutable read-only mode."""
    p = Path(path).resolve()
    # Use URI filename for read-only connection
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


# --------------------------------------------------------------------------- #
# Project Discovery
# --------------------------------------------------------------------------- #

def list_project_refs() -> list[ProjectRef]:
    """Enumerate Antigravity projects by grouping conversations by workspace."""
    base_dir = antigravity_dir()
    if not base_dir.is_dir():
        return []

    summaries_db = base_dir / "conversation_summaries.db"
    conv_dir = antigravity_conversations_dir()
    if not conv_dir.is_dir() and not summaries_db.is_file():
        return []

    workspace_groups: dict[str, list[str]] = {}  # norm_workspace -> [conv_id, ...]

    if summaries_db.is_file():
        try:
            with _connect_ro(summaries_db) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT conversation_id, workspace_uris FROM conversation_summaries "
                    "ORDER BY last_modified_time DESC"
                )
                for cid, uris_raw in cur.fetchall():
                    if not uris_raw:
                        continue
                    try:
                        uris = json.loads(uris_raw)
                    except Exception:
                        uris = [uris_raw]
                    for u in uris:
                        ws = _normalize_workspace_uri(u)
                        if ws:
                            workspace_groups.setdefault(ws, []).append(cid)
        except Exception:
            pass

    # If summaries_db is missing or empty, fallback to inspecting individual .db files
    if not workspace_groups and conv_dir.is_dir():
        for db_file in conv_dir.glob("*.db"):
            cid = db_file.stem
            try:
                with _connect_ro(db_file) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'")
                    row = cur.fetchone()
                    if row and row[0]:
                        # Quick extract file:///... from bytes
                        m = re.search(rb"file:///[^\x00-\x1f\x7f-\xff]+", row[0])
                        if m:
                            ws = _normalize_workspace_uri(m.group(0).decode("utf-8", errors="ignore"))
                            if ws:
                                workspace_groups.setdefault(ws, []).append(cid)
            except Exception:
                continue

    refs: list[ProjectRef] = []
    for ws, cids in workspace_groups.items():
        disp = _project_display_name(ws)
        s_paths = tuple(conv_dir / f"{cid}.db" for cid in cids if (conv_dir / f"{cid}.db").is_file())
        refs.append(
            ProjectRef(
                source="antigravity",
                key=disp,
                display_name=disp,
                cwd=ws,
                path=conv_dir,
                session_paths=s_paths,
            )
        )

    return sorted(refs, key=lambda r: r.display_name.lower())


def resolve_project(project: str) -> ProjectRef | None:
    """Resolve an Antigravity project key/display substring to a project ref."""
    refs = list_project_refs()
    for ref in refs:
        if ref.key == project or ref.display_name == project:
            return ref
    needle = project.lower()
    matches = [
        r for r in refs
        if needle in r.key.lower()
        or needle in r.display_name.lower()
        or (r.cwd and needle in r.cwd.lower())
    ]
    return matches[0] if len(matches) == 1 else None


def discover_sessions(project: str | ProjectRef) -> list[Path]:
    """Return all session .db paths belonging to the given ProjectRef or project string."""
    if isinstance(project, str):
        ref = resolve_project(project)
        if ref is None:
            return []
    else:
        ref = project

    conv_dir = antigravity_conversations_dir()
    if not conv_dir.is_dir():
        return []

    if ref.session_paths:
        return [p for p in ref.session_paths if p.is_file()]

    summaries_db = antigravity_dir() / "conversation_summaries.db"
    cids: list[str] = []
    target_ws = ref.cwd or ""

    if summaries_db.is_file():
        try:
            with _connect_ro(summaries_db) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT conversation_id, workspace_uris FROM conversation_summaries "
                    "ORDER BY last_modified_time DESC"
                )
                for cid, uris_raw in cur.fetchall():
                    if not uris_raw:
                        continue
                    try:
                        uris = json.loads(uris_raw)
                    except Exception:
                        uris = [uris_raw]
                    for u in uris:
                        if _normalize_workspace_uri(u) == target_ws:
                            cids.append(cid)
                            break
        except Exception:
            pass

    res: list[Path] = []
    if cids:
        for cid in cids:
            p = conv_dir / f"{cid}.db"
            if p.is_file():
                res.append(p)
    else:
        # Fallback: check each db file in conv_dir
        for db_file in sorted(conv_dir.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                with _connect_ro(db_file) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'")
                    row = cur.fetchone()
                    if row and row[0]:
                        m = re.search(rb"file:///[^\x00-\x1f\x7f-\xff]+", row[0])
                        if m and _normalize_workspace_uri(m.group(0).decode("utf-8", errors="ignore")) == target_ws:
                            res.append(db_file)
            except Exception:
                continue

    return res


# --------------------------------------------------------------------------- #
# Metadata, Usage, and LOC Parsing
# --------------------------------------------------------------------------- #

def read_session_meta(path: Path) -> SessionMeta:
    """Read session metadata from SQLite conversation database."""
    session_id = path.stem
    title = None
    started_at = None
    cwd = None

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()

            # Read cwd from trajectory_metadata_blob
            try:
                cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'")
                row = cur.fetchone()
                if row and row[0]:
                    m = re.search(rb"file:///[^\x00-\x1f\x7f-\xff]+", row[0])
                    if m:
                        cwd = _normalize_workspace_uri(m.group(0).decode("utf-8", errors="ignore"))
            except Exception:
                pass

            # Read started_at timestamp from first step
            try:
                cur.execute("SELECT metadata, step_type, step_payload FROM steps ORDER BY idx ASC LIMIT 5")
                rows = cur.fetchall()
                for r in rows:
                    if started_at is None and r[0]:
                        ts = _decode_timestamp(r[0])
                        if ts is not None:
                            started_at = _format_timestamp(ts)
                    if title is None and r[1] == 14 and r[2]:
                        txt = _extract_user_text(r[2])
                        if txt:
                            title = txt[:80]
            except Exception:
                pass
    except Exception:
        pass

    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=path,
        is_subagent=False,
        entrypoint="agy",
        source="antigravity",
    )


def aggregate_usage(
    path: Path,
    cancel_check: Callable[[], None] | None = None,
) -> TokenUsage:
    """Aggregate token usage, tool metrics, and turn timelines from an Antigravity .db."""
    u = TokenUsage()
    if not path.is_file():
        return u

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()

            # 1. gen_metadata: Authoritative token usages per generation
            cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC")
            gen_rows = cur.fetchall()
            decoded_gens = []
            for _, data in gen_rows:
                if cancel_check:
                    cancel_check()
                if not data:
                    continue
                model, uncached_in, total_out, cached_in, reasoning = _decode_gen_metadata(data)
                total_in = uncached_in + cached_in
                decoded_gens.append((model, uncached_in, total_out, cached_in, reasoning))

                u.input_tokens += total_in
                u.output_tokens += total_out
                u.cache_read_input_tokens += cached_in
                u.reasoning_output_tokens += reasoning
                u.peak_input_tokens = max(u.peak_input_tokens, total_in)

                if model:
                    norm_m = pricing.normalize(model)
                    u.models.add(norm_m)
                    mu = u.bucket(norm_m)
                    mu.input_tokens += total_in
                    mu.output_tokens += total_out
                    mu.cache_read_input_tokens += cached_in

            # 2. steps: Timelines, user messages, tool calls, errors
            cur.execute(
                "SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx ASC"
            )
            step_rows = cur.fetchall()

            first_ts = None
            last_ts = None
            current_turn = 0

            for _, stype, status, meta, payload in step_rows:
                if cancel_check:
                    cancel_check()

                ts = _decode_timestamp(meta)
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                ts_ms = int(ts * 1000) if ts is not None else None

                if stype == 14:  # User input
                    u.user_msgs += 1
                    txt = _extract_user_text(payload)
                    if u.first_prompt_chars == 0 and txt:
                        u.first_prompt_chars = len(txt)
                    if _is_slash_command(txt):
                        u.slash_command_count += 1
                    if _is_correction(txt):
                        u.correction_msg_count += 1

                elif stype == 15:  # Assistant turn / tool call
                    current_turn += 1
                    u.assistant_msgs += 1
                    tool_name, tool_args_json, _ = _extract_tool_call(payload)
                    t_calls = 0
                    if tool_name:
                        t_calls = 1
                        canon_tool = _ANTIGRAVITY_TOOL_MAP.get(tool_name, tool_name)
                        u.tool_calls[canon_tool] = u.tool_calls.get(canon_tool, 0) + 1
                        # Extract file path hint for tool_ops
                        fp_hint = ""
                        if tool_args_json:
                            try:
                                args = json.loads(tool_args_json)
                                fp_hint = (
                                    args.get("TargetFile")
                                    or args.get("AbsolutePath")
                                    or args.get("file_path")
                                    or args.get("path")
                                    or ""
                                )
                            except Exception:
                                pass
                        u.tool_ops.append(ToolOp(current_turn, canon_tool, fp_hint))

                    # Add turn stat entry
                    u.turn_stats.append(
                        TurnStat(
                            turn=current_turn,
                            ts=ts_ms,
                            tool_calls=t_calls,
                            errors=0,
                        )
                    )

                elif stype == 132:  # Tool result
                    _, exit_code = _extract_tool_result(payload)
                    is_err = (exit_code != 0) or (status != 3 and status != 0)
                    if is_err:
                        u.tool_errors += 1
                        if u.turn_stats:
                            u.turn_stats[-1].errors += 1

            # 3. Map decoded generation tokens to turn_stats
            for i, (m, u_in, t_out, c_in, _) in enumerate(decoded_gens):
                if i < len(u.turn_stats):
                    stat = u.turn_stats[i]
                    stat.input_tokens = u_in
                    stat.cache_read = c_in
                    stat.output_tokens = t_out
                    if m:
                        stat.model = pricing.normalize(m)

            # Calculate inter-turn duration
            for i in range(len(u.turn_stats) - 1):
                cur_t = u.turn_stats[i].ts
                nxt_t = u.turn_stats[i + 1].ts
                if cur_t is not None and nxt_t is not None and nxt_t >= cur_t:
                    u.turn_stats[i].duration_ms = nxt_t - cur_t

            if first_ts is not None and last_ts is not None and last_ts >= first_ts:
                u.started_at = int(first_ts * 1000)
                u.ended_at = int(last_ts * 1000)
                u.session_duration_ms = int((last_ts - first_ts) * 1000)

    except Exception:
        pass

    return u


def session_loc_full(
    path: Path,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[loc.SessionLoc, bool]:
    """Replay file-modifying tools to compute git-free LOC and rework metrics.

    Returns (SessionLoc, has_loc_signal).
    """
    acc = loc._LocAccumulator()
    has_signal = False

    if not path.is_file():
        return acc.finish(), False

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT idx, step_type, step_payload FROM steps ORDER BY idx ASC"
            )
            step_rows = cur.fetchall()

            current_turn = 0
            for _, stype, payload in step_rows:
                if cancel_check:
                    cancel_check()

                if stype == 15:
                    current_turn += 1
                    tool_name, tool_args_json, _ = _extract_tool_call(payload)
                    if not tool_name:
                        continue

                    canon_tool = _ANTIGRAVITY_TOOL_MAP.get(tool_name, tool_name)

                    if canon_tool == "Write":
                        try:
                            args = json.loads(tool_args_json)
                        except Exception:
                            args = {}
                        target = (
                            args.get("TargetFile")
                            or args.get("target_file")
                            or args.get("file_path")
                            or ""
                        )
                        content = (
                            args.get("CodeContent")
                            or args.get("code_content")
                            or args.get("content")
                            or ""
                        )
                        if target:
                            has_signal = True
                            acc.on_tool_use(
                                "Write",
                                {"file_path": target, "content": content},
                                turn=current_turn,
                            )

                    elif canon_tool == "Edit":
                        try:
                            args = json.loads(tool_args_json)
                        except Exception:
                            args = {}
                        target = (
                            args.get("TargetFile")
                            or args.get("target_file")
                            or args.get("file_path")
                            or ""
                        )
                        old_content = (
                            args.get("TargetContent")
                            or args.get("target_content")
                            or ""
                        )
                        new_content = (
                            args.get("ReplacementContent")
                            or args.get("replacement_content")
                            or ""
                        )
                        if target:
                            has_signal = True
                            acc.on_tool_use(
                                "Edit",
                                {
                                    "file_path": target,
                                    "old_string": old_content,
                                    "new_string": new_content,
                                },
                                turn=current_turn,
                            )

    except Exception:
        pass

    return acc.finish(), has_signal


def read_user_messages(path: Path) -> list[str]:
    """Extract all user prompts from the conversation."""
    msgs: list[str] = []
    if not path.is_file():
        return msgs

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT step_payload FROM steps WHERE step_type = 14 ORDER BY idx ASC")
            for (payload,) in cur.fetchall():
                txt = _extract_user_text(payload)
                if txt:
                    msgs.append(txt)
    except Exception:
        pass
    return msgs


def read_conversation(path: Path) -> list[dict]:
    """Reconstruct conversational timeline for full-transcript viewing or upload."""
    events: list[dict] = []
    if not path.is_file():
        return events

    try:
        with _connect_ro(path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx ASC"
            )
            for idx, stype, meta, payload in cur.fetchall():
                ts = _decode_timestamp(meta)
                iso_ts = _format_timestamp(ts)
                if stype == 14:
                    txt = _extract_user_text(payload)
                    events.append({
                        "role": "user",
                        "content": txt,
                        "timestamp": iso_ts,
                        "idx": idx,
                    })
                elif stype == 15:
                    tool_name, tool_args, call_id = _extract_tool_call(payload)
                    events.append({
                        "role": "assistant",
                        "tool_call": tool_name,
                        "tool_args": tool_args,
                        "call_id": call_id,
                        "timestamp": iso_ts,
                        "idx": idx,
                    })
                elif stype == 132:
                    out_text, exit_code = _extract_tool_result(payload)
                    events.append({
                        "role": "tool_result",
                        "content": out_text,
                        "exit_code": exit_code,
                        "timestamp": iso_ts,
                        "idx": idx,
                    })
    except Exception:
        pass
    return events
