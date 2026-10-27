"""Google Antigravity CLI (agy) local-session reader.

Antigravity CLI stores sessions under ``~/.gemini/antigravity-cli`` (cc-switch isolated
terminals use ``~/.cc-switch/agy/<provider_id>/antigravity-cli`` with the same layout;
all roots are merged by workspace, see :func:`tcer.core.paths.antigravity_roots`):
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
      * step_type == 132: Tool execution results and exit codes; field 5.4 repeats the
        originating call (call_id / tool name / args, byte-identical to step 15).
      * step_type == 23: Context compaction (checkpoint summary). Its LLM usage lives
        only in step metadata field 28.2 and is NOT mirrored into ``gen_metadata``.
      * step status (field 4): 3 = done, 5 = done but payload pruned (step 15 loses
        its tool calls, step 14 keeps only the 19.3.1 copy of the prompt), 7 = failed.

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
from tcer.core.paths import antigravity_conversations_dir, antigravity_roots

# Antigravity tool name -> TCER canonical tool name
_ANTIGRAVITY_TOOL_MAP = {
    "view_file": "Read",
    "read_file": "Read",
    "write_to_file": "Write",
    "write_file": "Write",
    "replace_file_content": "Edit",
    "edit_file": "Edit",
    "run_command": "Bash",
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


def _canon_tool(tool_name: str, tool_args_json: str = "") -> str:
    """Map an agy tool name to the canonical name.

    ``call_mcp_tool`` is a generic MCP gateway (args ``ServerName``/``ToolName``);
    expose it as ``mcp__<server>__<tool>`` like the other sources (CLAUDE.md #28).
    """
    if tool_name == "call_mcp_tool" and tool_args_json:
        try:
            a = json.loads(tool_args_json)
            srv, tl = a.get("ServerName"), a.get("ToolName")
            if isinstance(srv, str) and isinstance(tl, str) and srv and tl:
                return f"mcp__{srv}__{tl}"
        except Exception:
            pass
    return _ANTIGRAVITY_TOOL_MAP.get(tool_name, tool_name)


def _arg(args: dict, *keys: str) -> str:
    """First non-empty string value among ``keys`` (agy arg names differ per tool/version)."""
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


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
        # status 5（payload 被裁剪）只剩 19.3.1 副本；未裁剪时二者逐字相同
        f3 = _find_field(sub_fields, 3)
        if isinstance(f3, bytes):
            txt = _find_field(_parse_proto_fields(f3), 1)
            if isinstance(txt, bytes):
                return txt.decode("utf-8", errors="ignore").strip()

    # Fallback: scan length-delimited fields for non-proto plain text
    for _, wt, val in fields:
        if wt == 2 and isinstance(val, bytes):
            sub = _parse_proto_fields(val)
            for _, swt, sval in sub:
                if swt == 2 and isinstance(sval, bytes):
                    try:
                        # 严格解码 + 拒控制字符：宽松解码会把 protobuf 片段当成提问正文
                        s = sval.decode("utf-8").strip()
                        if (
                            len(s) > 1
                            and not any(ord(ch) < 32 and ch not in "\n\r\t" for ch in s)
                            and not s.startswith("{")
                            and not s.startswith("type.googleapis.com")
                            and not s.startswith("file:///")
                        ):
                            return s
                    except Exception:
                        pass
    return ""


def _extract_tool_calls(payload: bytes | None) -> list[tuple[str, str, str]]:
    """All ``(tool_name, tool_args_json, call_id)`` of a step_type == 15 payload.

    Field 20 repeats field 7 once per call when the model issues parallel tool
    calls in one turn; reading only the first undercounts tools and LOC.
    """
    if not payload:
        return []
    f20 = _find_field(_parse_proto_fields(payload), 20)
    if not isinstance(f20, bytes):
        return []
    out: list[tuple[str, str, str]] = []
    for fn, wt, f7 in _parse_proto_fields(f20):
        if fn != 7 or wt != 2 or not isinstance(f7, bytes):
            continue
        sf7 = _parse_proto_fields(f7)
        name = _bstr(_find_field(sf7, 2))
        if name:
            out.append((name, _bstr(_find_field(sf7, 3)), _bstr(_find_field(sf7, 1))))
    return out


def _extract_result_call(payload: bytes | None) -> tuple[str, str, str]:
    """``(tool_name, tool_args_json, call_id)`` echoed in a step_type == 132 payload (field 5.4).

    Survives payload pruning (status 5) that empties the originating step 15.
    """
    if not payload:
        return ("", "", "")
    f5 = _find_field(_parse_proto_fields(payload), 5)
    if not isinstance(f5, bytes):
        return ("", "", "")
    f4 = _find_field(_parse_proto_fields(f5), 4)
    if not isinstance(f4, bytes):
        return ("", "", "")
    sf = _parse_proto_fields(f4)
    return (_bstr(_find_field(sf, 2)), _bstr(_find_field(sf, 3)), _bstr(_find_field(sf, 1)))


def _usage_fields(blob) -> tuple[int, int, int, int, str]:
    """``(uncached_input, total_output, cached_input, reasoning, response_id)`` of a usage blob."""
    if not isinstance(blob, bytes):
        return (0, 0, 0, 0, "")
    uf = _parse_proto_fields(blob)

    def _i(n: int) -> int:
        v = _find_field(uf, n)
        return v if isinstance(v, int) else 0

    return (_i(2), _i(3), _i(5), _i(9), _bstr(_find_field(uf, 7)))


def _gen_response_id(data: bytes | None) -> str:
    """Response id (usage field 7) of a ``gen_metadata`` row."""
    if not data:
        return ""
    inner = _find_field(_parse_proto_fields(data), 1)
    if not isinstance(inner, bytes):
        return ""
    return _usage_fields(_find_field(_parse_proto_fields(inner), 4))[4]


def _compaction_usage(payload: bytes | None) -> tuple[int, int, int, int, str]:
    """Usage of a step_type == 23 compaction call (step metadata field 5.28.2)."""
    if not payload:
        return (0, 0, 0, 0, "")
    f5 = _find_field(_parse_proto_fields(payload), 5)
    if not isinstance(f5, bytes):
        return (0, 0, 0, 0, "")
    f28 = _find_field(_parse_proto_fields(f5), 28)
    if not isinstance(f28, bytes):
        return (0, 0, 0, 0, "")
    return _usage_fields(_find_field(_parse_proto_fields(f28), 2))


def _bstr(b) -> str:
    return b.decode("utf-8", errors="ignore") if isinstance(b, bytes) else ""


def _result_ok(status: int, exit_code: int) -> bool:
    """Tool result succeeded: status 0/3 (done) or 5 (done, payload pruned) and exit code 0."""
    return exit_code == 0 and status in (0, 3, 5)


def _extract_tool_call(payload: bytes | None) -> tuple[str, str, str]:
    """Extract the first (tool_name, tool_args_json, call_id) from a step_type == 15 payload."""
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

def _root_entries(root: Path) -> list[tuple[str, list[str]]]:
    """``(conversation_id, [normalized_workspace, ...])`` for one data root, newest first.

    Prefers ``conversation_summaries.db``; falls back to scanning each session db's
    ``trajectory_metadata_blob`` when the catalog is missing or carries no workspaces.
    """
    entries: list[tuple[str, list[str]]] = []
    summaries_db = root / "conversation_summaries.db"
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
                    wss: list[str] = []
                    for u in uris:
                        ws = _normalize_workspace_uri(u)
                        if ws and ws not in wss:
                            wss.append(ws)
                    if wss:
                        entries.append((cid, wss))
        except Exception:
            pass
    if entries:
        return entries

    conv_dir = root / "conversations"
    if not conv_dir.is_dir():
        return entries
    for db_file in sorted(conv_dir.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True):
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
                            entries.append((db_file.stem, [ws]))
        except Exception:
            continue
    return entries


def _workspace_sessions() -> dict[str, list[Path]]:
    """Group session db files from every Antigravity root by normalized workspace.

    Roots = ``~/.gemini/antigravity-cli`` + cc-switch isolated profiles
    (:func:`tcer.core.paths.antigravity_roots`). The same workspace across roots
    merges into one group. A conversation id present in several roots (copied
    profile) is counted once -- the most recently modified file wins.
    """
    entries: list[tuple[str, list[str]]] = []
    best: dict[str, tuple[int, Path]] = {}
    for root in antigravity_roots():
        conv_dir = root / "conversations"
        for cid, wss in _root_entries(root):
            entries.append((cid, wss))
            p = conv_dir / f"{cid}.db"
            try:
                mt = p.stat().st_mtime_ns
            except OSError:
                continue
            if not p.is_file():
                continue
            prev = best.get(cid)
            if prev is None or mt > prev[0]:
                best[cid] = (mt, p)

    groups: dict[str, list[Path]] = {}
    for cid, wss in entries:
        chosen = best.get(cid)
        for ws in wss:
            lst = groups.setdefault(ws, [])
            if chosen is not None and chosen[1] not in lst:
                lst.append(chosen[1])
    return groups


def list_project_refs() -> list[ProjectRef]:
    """Enumerate Antigravity projects by grouping conversations by workspace (across all roots)."""
    refs: list[ProjectRef] = []
    for ws, paths in _workspace_sessions().items():
        disp = _project_display_name(ws)
        refs.append(
            ProjectRef(
                source="antigravity",
                key=disp,
                display_name=disp,
                cwd=ws,
                path=paths[0].parent if paths else antigravity_conversations_dir(),
                session_paths=tuple(paths),
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

    if ref.session_paths:
        return [p for p in ref.session_paths if p.is_file()]

    return [p for p in _workspace_sessions().get(ref.cwd or "", []) if p.is_file()]




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
            gen_ids: set[str] = set()
            for _, data in gen_rows:
                if cancel_check:
                    cancel_check()
                if not data:
                    continue
                rid = _gen_response_id(data)
                if rid:
                    gen_ids.add(rid)
                model, uncached_in, total_out, cached_in, reasoning = _decode_gen_metadata(data)
                total_in = uncached_in + cached_in
                if total_in == 0 and total_out == 0:
                    # 零 usage 错误桩（如请求前 400 失败）不产生 step_type=15，
                    # 若写入 decoded_gens 会使后续 turn_stats 整体错位 1 回合。
                    u.empty_usage_skipped += 1
                    continue
                decoded_gens.append((model, uncached_in, total_out, cached_in, reasoning))

                # 口径同其余源：input_tokens 只记未命中缓存部分，缓存命中单列
                # cache_read（total_input = 二者之和）。曾把 total_in 记进 input，
                # 缓存被计两遍且按全价计费（实测 Token ×2、成本 ×7.7）。
                u.input_tokens += uncached_in
                u.output_tokens += total_out
                u.cache_read_input_tokens += cached_in
                u.reasoning_output_tokens += reasoning
                u.peak_input_tokens = max(u.peak_input_tokens, total_in)

                if model:
                    norm_m = pricing.normalize(model)
                    u.models.add(norm_m)
                    mu = u.bucket(norm_m)
                    mu.input_tokens += uncached_in
                    mu.output_tokens += total_out
                    mu.cache_read_input_tokens += cached_in

            # 2. steps: Timelines, user messages, tool calls, errors
            cur.execute(
                "SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx ASC"
            )
            step_rows = cur.fetchall()

            first_ts = None
            last_ts = None
            # turn 0-based（与其余数据源一致；llm_prompts 里程碑提取与
            # ground_dynamics_user_turns 均按 0-based 键消费 turn_stats）
            current_turn = -1
            seen_calls: set[str] = set()

            def _record_call(name: str, args_json: str) -> None:
                canon_tool = _canon_tool(name, args_json)
                u.tool_calls[canon_tool] = u.tool_calls.get(canon_tool, 0) + 1
                # Extract file path hint for tool_ops
                fp_hint = ""
                if args_json:
                    try:
                        args = json.loads(args_json)
                        fp_hint = _arg(
                            args, "TargetFile", "AbsolutePath", "Path",
                            "file_path", "path",
                        )
                    except Exception:
                        pass
                u.tool_ops.append(ToolOp(max(current_turn, 0), canon_tool, fp_hint))

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
                    # 并行工具调用：字段 20 下多个字段 7，逐个计
                    calls = _extract_tool_calls(payload)
                    for tool_name, tool_args_json, call_id in calls:
                        if call_id:
                            seen_calls.add(call_id)
                        _record_call(tool_name, tool_args_json)

                    # Add turn stat entry
                    u.turn_stats.append(
                        TurnStat(
                            turn=current_turn,
                            ts=ts_ms,
                            tool_calls=len(calls),
                            errors=0,
                            user_turn=u.user_msgs if u.user_msgs > 0 else 1,
                        )
                    )

                elif stype == 132:  # Tool result
                    # status 5 裁剪掉了 step 15 的调用体，结果步骤字段 5.4 仍带原调用：补计
                    r_name, r_args, r_cid = _extract_result_call(payload)
                    if r_name and r_cid and r_cid not in seen_calls:
                        seen_calls.add(r_cid)
                        _record_call(r_name, r_args)
                        if u.turn_stats:
                            u.turn_stats[-1].tool_calls += 1
                    _, exit_code = _extract_tool_result(payload)
                    if not _result_ok(status, exit_code):
                        u.tool_errors += 1
                        if u.turn_stats:
                            u.turn_stats[-1].errors += 1

                elif stype == 23:  # Context compaction (checkpoint summary)
                    u.compaction_count += 1
                    u.compaction_turns.append(current_turn + 1)
                    # 压缩调用的 usage 只在步骤元数据里、不进 gen_metadata（按响应 id
                    # 去重以防上游日后补写）；记录不带模型，按前一次生成的模型计价（推断）。
                    c_unc, c_out, c_cached, c_reas, c_rid = _compaction_usage(payload)
                    if (c_unc or c_out or c_cached) and not (c_rid and c_rid in gen_ids):
                        if c_rid:
                            gen_ids.add(c_rid)
                        u.input_tokens += c_unc
                        u.output_tokens += c_out
                        u.cache_read_input_tokens += c_cached
                        u.reasoning_output_tokens += c_reas
                        u.peak_input_tokens = max(u.peak_input_tokens, c_unc + c_cached)
                        prev_model = ""
                        for gi in range(min(current_turn, len(decoded_gens) - 1), -1, -1):
                            if decoded_gens[gi][0]:
                                prev_model = decoded_gens[gi][0]
                                break
                        if not prev_model:
                            prev_model = next((g[0] for g in decoded_gens if g[0]), "")
                        if prev_model:
                            mu = u.bucket(pricing.normalize(prev_model))
                            mu.input_tokens += c_unc
                            mu.output_tokens += c_out
                            mu.cache_read_input_tokens += c_cached

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

    LOC 按结果裁决（CLAUDE.md #32）：编辑类调用等到 step 132 结果且成功
    （status 0/3/5）才回放；status 7 的失败编辑（实测 "Could not successfully
    apply"）不计。调用体优先取结果字段 5.4（裁剪后的 status 5 步骤只剩这份），
    缺失时按 step 15 调用的出现顺序配对（实测结果严格按调用顺序到达）。

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
                "SELECT idx, step_type, status, step_payload FROM steps ORDER BY idx ASC"
            )
            step_rows = cur.fetchall()

            # turn 0-based（与 aggregate_usage 同口径，turn_net_locs 键空间一致）
            current_turn = -1
            pending: list[tuple[str, str, str]] = []  # (name, args, call_id) awaiting result
            for _, stype, status, payload in step_rows:
                if cancel_check:
                    cancel_check()

                if stype == 15:
                    current_turn += 1
                    pending.extend(_extract_tool_calls(payload))
                    continue
                if stype != 132:
                    continue

                tool_name, tool_args_json, call_id = _extract_result_call(payload)
                if tool_name:
                    pending = [c for c in pending if not (call_id and c[2] == call_id)]
                elif pending:
                    tool_name, tool_args_json, call_id = pending.pop(0)
                if not tool_name or status not in (0, 3, 5):
                    continue

                canon_tool = _ANTIGRAVITY_TOOL_MAP.get(tool_name, tool_name)
                if canon_tool not in ("Write", "Edit"):
                    continue
                try:
                    args = json.loads(tool_args_json)
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                target = _arg(args, "TargetFile", "target_file", "Path", "file_path")
                if not target:
                    continue
                has_signal = True
                if canon_tool == "Write":
                    # write_to_file: TargetFile/CodeContent; write_file: Path/Content
                    content = _arg(args, "CodeContent", "code_content", "Content", "content")
                    acc.on_tool_use(
                        "Write",
                        {"file_path": target, "content": content},
                        turn=current_turn,
                    )
                else:
                    # replace_file_content: TargetFile/TargetContent/ReplacementContent;
                    # edit_file: Path/Target/Replacement
                    old_content = _arg(args, "TargetContent", "target_content", "Target")
                    new_content = _arg(
                        args, "ReplacementContent", "replacement_content", "Replacement"
                    )
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
