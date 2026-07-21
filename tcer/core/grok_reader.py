"""Grok (grok build CLI) local-session reader.

Grok persists sessions under ``~/.grok/sessions/<url-encoded-cwd>/<uuid>/``.
The authoritative conversation log is ``updates.jsonl`` — an ACP / JSON-RPC
notification stream where each line is ``{"timestamp", "method",
"params": {"sessionId", "update": {...}, "_meta": {...}}}``.

Token usage lives in ``turn_completed`` updates (one per turn, carrying a
``usage`` object with per-model ``modelUsage`` — there is no Claude-style
multi-line duplication). Tool calls live in ``tool_call`` updates, whose
``_meta["x.ai/tool"]`` gives the canonical tool name and ``kind``; the edit
tool ``search_replace`` is structurally identical to Claude's ``Edit``
(``file_path`` / ``old_string`` / ``new_string``), so LOC reuses the same
old/new line-delta logic.

This module maps that stream onto TCER's existing ``TokenUsage`` /
``SessionMeta`` shapes, mirroring ``codex_reader`` so ``analyze`` and the GUI
need no special-casing beyond a ``source == "grok"`` branch.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

from tcer.core import pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp
from tcer.core.paths import encode_hash, grok_sessions_dir
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_NO_CWD_KEY = "__grok_no_cwd__"
_NO_CWD_LABEL = "Grok 无工作目录"

# Grok tool name -> TCER canonical tool name (matches the Claude/Codex set so
# ratios like read/write and search/edit compare across sources).
_GROK_TOOL_MAP = {
    "read_file": "Read",
    "search_replace": "Edit",
    "write": "Write",
    "grep_search": "Grep",
    # Live grok build sessions sometimes emit short names (observed 2026-07).
    "grep": "Grep",
    "list_dir": "Glob",
    "bash": "Bash",
    "run_terminal_command": "Bash",
    "task": "Task",
    "kill_task": "KillTask",
    "get_task_output": "GetTaskOutput",
    # Alternate spawn API names (same semantics as get_task_output / kill_task).
    "get_command_or_subagent_output": "GetTaskOutput",
    "kill_command_or_subagent": "KillTask",
    "web_search": "WebSearch",
    "websearch": "WebSearch",
    "web_fetch": "WebFetch",
    "webfetch": "WebFetch",
    "todo_write": "TodoWrite",
    "memory_search": "MemorySearch",
    "memory_get": "MemoryGet",
    "lsp": "LSP",
    "search_tool": "SearchTool",
    "use_tool": "UseTool",
    "image_gen": "ImageGen",
    "image_edit": "ImageEdit",
    "scheduler_create": "SchedulerCreate",
    "scheduler_delete": "SchedulerDelete",
}

# ACP ``kind`` field when ``x.ai/tool`` is missing (backend WebSearch etc.).
_GROK_KIND_MAP = {
    "search": "WebSearch",
    "fetch": "WebFetch",
    "edit": "Edit",
    "read": "Read",
    "execute": "Bash",
    "think": "Thinking",
}


def iter_updates(path: Path):
    """Yield parsed Grok ``updates.jsonl`` lines, skipping malformed ones."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _update_of(obj: dict) -> dict:
    """Return ``params.update`` from a notification (``{}`` if absent)."""
    params = obj.get("params")
    if isinstance(params, dict):
        upd = params.get("update")
        if isinstance(upd, dict):
            return upd
    return {}


def _meta_of(obj: dict) -> dict:
    """Return ``params._meta`` from a notification (``{}`` if absent)."""
    params = obj.get("params")
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict):
            return meta
    return {}


def discover_sessions() -> list[Path]:
    """Recursively collect Grok ``updates.jsonl`` files (one per session)."""
    base = grok_sessions_dir()
    if not base.is_dir():
        return []
    return sorted(base.rglob("updates.jsonl"))


def _read_summary(session_dir: Path) -> dict:
    """Parse a session's ``summary.json`` (``{}`` if missing/unparseable)."""
    p = session_dir / "summary.json"
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _decode_cwd(dir_name: str | None) -> str | None:
    """URL-decode a Grok cwd folder name.

    ``C%3A%5Cplayground%5Clangfuse`` -> ``C:\\playground\\langfuse``.
    """
    if not dir_name:
        return None
    return unquote(dir_name)


def _normalize_cwd(cwd: str | None) -> str | None:
    """Normalize a cwd path to avoid drive-letter case duplicates.

    Mirrors ``codex_reader._normalize_cwd``: ``Path.resolve()`` uppercases the
    Windows drive letter so ``c:\\GitHub`` and ``C:\\GitHub`` share a group.
    """
    if not cwd:
        return cwd
    try:
        return str(Path(cwd).resolve())
    except (OSError, ValueError):
        return cwd


def list_project_refs() -> list[ProjectRef]:
    """Group Grok sessions by cwd for the unified project list."""
    groups: dict[str, list[Path]] = {}
    cwd_by_key: dict[str, str | None] = {}
    for p in discover_sessions():
        meta = read_session_meta(p)
        cwd = _normalize_cwd(meta.cwd)
        key = encode_hash(cwd) if cwd else _NO_CWD_KEY
        groups.setdefault(key, []).append(p)
        cwd_by_key.setdefault(key, cwd)

    refs: list[ProjectRef] = []
    for key, paths in groups.items():
        cwd = cwd_by_key.get(key)
        refs.append(ProjectRef(
            source="grok",
            key=key,
            display_name=_display_name_for_cwd(cwd),
            cwd=cwd,
            path=Path(cwd) if cwd else None,
            session_paths=tuple(sorted(paths)),
        ))
    return refs


def resolve_project(project: str) -> ProjectRef | None:
    """Resolve a Grok project key/display substring to a project ref."""
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
    """Return the Grok session files for a project ref or key."""
    if isinstance(project, ProjectRef):
        return list(project.session_paths)
    ref = resolve_project(project)
    return list(ref.session_paths) if ref else []


def read_session_meta(path: Path) -> SessionMeta:
    """Extract lightweight Grok session metadata from ``summary.json``.

    Falls back to the URL-encoded parent folder for cwd when ``info.cwd`` is
    absent. Stays cheap (reads only ``summary.json``) since the project list
    calls this once per session.
    """
    session_dir = path.parent
    summary = _read_summary(session_dir)
    info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
    session_id = _first_str(info.get("id")) or session_dir.name
    cwd = info.get("cwd") if isinstance(info.get("cwd"), str) else _decode_cwd(session_dir.parent.name)
    title = _first_str(summary.get("generated_title"), summary.get("session_summary"))
    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=path,
        is_subagent=False,
        entrypoint=_first_str(summary.get("agent_name")),
        source="grok",
        sandbox_policy=_json_label(summary.get("sandbox_profile")),
        reasoning_effort=_json_label(summary.get("reasoning_effort")),
    )


def aggregate_usage(path: Path) -> TokenUsage:
    """Aggregate Grok token and tool usage from one session's ``updates.jsonl``."""
    u = TokenUsage()
    turn_idx = 0
    current_model = ""
    api_duration_ms = 0
    call_id_to_name: dict[str, str] = {}
    in_user_run = False  # coalesce consecutive user chunks into one message

    for obj in iter_updates(path):
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if ts is not None:
            u.started_at = ts if u.started_at is None else min(u.started_at, ts)
            u.ended_at = ts if u.ended_at is None else max(u.ended_at, ts)

        meta = _meta_of(obj)
        update = _update_of(obj)
        su = update.get("sessionUpdate")
        if su != "user_message_chunk":
            in_user_run = False  # any other update ends the user-chunk run

        if su == "turn_completed":
            api_duration_ms += _add_turn_usage(u, update.get("usage"), current_model)
            continue

        if su == "user_message_chunk":
            text = _chunk_text(update)
            if text and text.strip():
                # One user message may arrive as several chunks (and grok build
                # occasionally re-emits a chunk); a run of consecutive user
                # chunks is ONE message — same coalescing as read_conversation.
                if not in_user_run:
                    turn_idx += 1
                    u.user_msgs += 1
                in_user_run = True
                mid = meta.get("modelId")
                if isinstance(mid, str) and mid:
                    current_model = pricing.normalize(mid)
            continue

        if su == "agent_thought_chunk":
            u.thinking_count += 1
            continue

        if su == "tool_call":
            canonical = _resolve_grok_tool_name(update)
            raw_input = update.get("rawInput")
            u.tool_calls[canonical] = u.tool_calls.get(canonical, 0) + 1
            u.tool_ops.append(ToolOp(turn_idx, canonical, _path_hint(raw_input)))
            cid = update.get("toolCallId")
            if isinstance(cid, str):
                call_id_to_name[cid] = canonical
            if canonical in ("WebSearch", "WebFetch"):
                u.web_search_count += 1
            if canonical == "Task":
                u.task_count += 1
            continue

        if su == "tool_call_update":
            raw_output = update.get("rawOutput")
            code = raw_output.get("exit_code") if isinstance(raw_output, dict) else None
            if code is not None and _as_int(code) != 0:
                u.tool_errors += 1
                cid = update.get("toolCallId")
                tname = call_id_to_name.get(cid) if isinstance(cid, str) else None
                if tname:
                    u.tool_errors_by_tool[tname] = u.tool_errors_by_tool.get(tname, 0) + 1
            status = update.get("status")
            if status == "completed" and isinstance(raw_output, dict):
                # A finished Task subagent counts as a completed task.
                if call_id_to_name.get(update.get("toolCallId")) == "Task":
                    u.completed_task_count += 1
            continue

    if api_duration_ms > 0:
        u.session_duration_ms = api_duration_ms
    elif u.started_at and u.ended_at:
        u.session_duration_ms = u.ended_at - u.started_at
    return u


def read_user_messages(path: Path) -> list[str]:
    """Extract Grok user-message text on demand for the popup."""
    messages: list[str] = []
    for obj in iter_updates(path):
        update = _update_of(obj)
        if update.get("sessionUpdate") != "user_message_chunk":
            continue
        text = _chunk_text(update)
        if text and text.strip():
            messages.append(truncate_summary(text.strip(), 500))
    return messages


def read_conversation(path: Path) -> list[dict]:
    """Extract the full ordered conversation from a Grok ``updates.jsonl``.

    Mirrors :func:`tcer.core.reader.read_conversation`'s output shape. Grok
    streams the turn as ACP notifications where text arrives in many
    ``*_chunk`` updates, so consecutive chunks of the same kind are coalesced
    into one block:

      * ``user_message_chunk``    -> user text
      * ``agent_message_chunk``   -> assistant text
      * ``agent_thought_chunk``   -> assistant thinking
      * ``tool_call``             -> tool_use (canonical name + rawInput)
      * ``tool_call_update`` (status=completed) -> tool_result (rawOutput)

    ``tool_call_update`` interleaves with chunks, so its result is attached to
    the tool call by ``toolCallId`` when it completes.
    """
    convo: list[dict] = []
    # Coalesce a run of same-kind chunks into one block, flushing on any change.
    pending_role: str | None = None
    pending_type: str | None = None
    pending_parts: list[str] = []
    pending_ts = None

    def _flush() -> None:
        nonlocal pending_role, pending_type, pending_parts, pending_ts
        if pending_parts:
            text = "".join(pending_parts).strip()
            if text:
                convo.append({"role": pending_role, "type": pending_type,
                              "text": text, "ts": pending_ts})
        pending_role = pending_type = None
        pending_parts = []
        pending_ts = None

    def _accumulate(role: str, typ: str, text: str, ts) -> None:
        nonlocal pending_role, pending_type, pending_parts, pending_ts
        if pending_type != typ or pending_role != role:
            _flush()
            pending_role, pending_type, pending_ts = role, typ, ts
        pending_parts.append(text)

    _CHUNK_KINDS = {
        "user_message_chunk": ("user", "text"),
        "agent_message_chunk": ("assistant", "text"),
        "agent_thought_chunk": ("assistant", "thinking"),
    }

    for obj in iter_updates(path):
        ts = parse_timestamp_ms(obj.get("timestamp"))
        update = _update_of(obj)
        su = update.get("sessionUpdate")

        if su in _CHUNK_KINDS:
            role, typ = _CHUNK_KINDS[su]
            _accumulate(role, typ, _chunk_text(update), ts)
            continue

        if su == "tool_call":
            _flush()
            tool_meta = update.get("_meta", {})
            xt = tool_meta.get("x.ai/tool") if isinstance(tool_meta, dict) else None
            name = xt.get("name") if isinstance(xt, dict) else None
            convo.append({
                "role": "assistant", "type": "tool_use",
                "name": _classify_grok_tool(name),
                "id": update.get("toolCallId"),
                "input": update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {},
                "ts": ts,
            })
            continue

        if su == "tool_call_update" and update.get("status") == "completed":
            _flush()
            raw_output = update.get("rawOutput")
            if isinstance(raw_output, dict):
                code = raw_output.get("exit_code")
                text = _first_str(raw_output.get("output"), raw_output.get("stdout")) \
                    or json.dumps(raw_output, ensure_ascii=False, default=str)
            else:
                code = None
                text = raw_output if isinstance(raw_output, str) else ""
            convo.append({
                "role": "tool", "type": "tool_result",
                "tool_use_id": update.get("toolCallId"),
                "is_error": bool(code is not None and _as_int(code) != 0),
                "text": text,
                "ts": ts,
            })
            continue

    _flush()
    return convo


def _loc_scan(
    path: Path,
    *,
    cwd: str | Path | None = None,
    disk_prior: bool = False,
):
    """Single pass over updates returning ``(SessionLoc, has_signal)``.

    ``has_signal`` is True if any parseable ``search_replace``/``write`` edit
    exists (independent of code-file filtering). LOC is replayed through the
    same ``_LocAccumulator`` as Claude so self-rework (churn) and high-churn
    files match Edit/Write semantics — not a permanent rework_deleted=0 stub.
    """
    from tcer.core.loc import SessionLoc, _LocAccumulator, _is_code

    acc = _LocAccumulator(cwd=cwd, disk_prior=disk_prior)
    has_signal = False

    for obj in iter_updates(path):
        update = _update_of(obj)
        if update.get("sessionUpdate") != "tool_call":
            continue
        tool_meta = update.get("_meta", {})
        xt = tool_meta.get("x.ai/tool") if isinstance(tool_meta, dict) else None
        name = xt.get("name") if isinstance(xt, dict) else None
        raw_input = update.get("rawInput")
        if not isinstance(raw_input, dict):
            continue

        if name == "search_replace":
            fp = _first_str(raw_input.get("file_path")) or ""
            if not fp:
                continue
            # Signal even for non-code paths (has_loc_signal); LOC filters inside.
            has_signal = True
            if not _is_code(fp):
                continue
            acc.on_tool_use("Edit", {
                "file_path": fp,
                "old_string": raw_input.get("old_string"),
                "new_string": raw_input.get("new_string"),
            })
        elif name == "write":
            fp = _first_str(raw_input.get("file_path")) or ""
            if not fp:
                continue
            has_signal = True
            if not _is_code(fp):
                continue
            content = _first_str(raw_input.get("content"), raw_input.get("file_text"))
            acc.on_tool_use("Write", {
                "file_path": fp,
                "content": content if content is not None else "",
            })

    # has_signal may be True with only non-code paths → empty SessionLoc.
    sloc = acc.finish() if has_signal else SessionLoc(added=0, deleted=0)
    return sloc, has_signal


def session_loc_full(path: Path, *, cwd: str | Path | None = None, disk_prior: bool = False):
    """Return LOC from parseable Grok ``search_replace``/``write`` calls only."""
    return _loc_scan(path, cwd=cwd, disk_prior=disk_prior)[0]


def has_loc_signal(path: Path) -> bool:
    """True if the session contains a parseable edit tool call."""
    return _loc_scan(path)[1]


# --------------------------------------------------------------------------- helpers

def _add_turn_usage(u: TokenUsage, usage, default_model: str) -> int:
    """Fold one ``turn_completed.usage`` into ``u``; return its ``apiDurationMs``.

    Each ``turn_completed`` is one authoritative, billable API completion (Grok
    emits exactly one per turn — no Claude-style multi-line duplication), so we
    simply sum them. Prefers the per-model ``modelUsage`` breakdown; falls back
    to the top-level counts attributed to ``default_model``.
    """
    if not isinstance(usage, dict):
        u.empty_usage_skipped += 1
        return 0
    added_any = False
    turn_input = 0
    model_usage = usage.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        for model, mu in model_usage.items():
            if isinstance(mu, dict):
                key = pricing.normalize(model) if isinstance(model, str) and model else default_model
                added, tin = _bucket_add(u, mu, key)
                if added:
                    added_any = True
                    turn_input += tin
    else:
        added, tin = _bucket_add(u, usage, default_model)
        if added:
            added_any = True
            turn_input += tin
    if added_any:
        u.assistant_msgs += 1
        if turn_input > 0:
            u.peak_input_tokens = max(u.peak_input_tokens, turn_input)
    else:
        u.empty_usage_skipped += 1
    return _as_int(usage.get("apiDurationMs"))


def _bucket_add(u: TokenUsage, mu: dict, model: str) -> tuple[bool, int]:
    """Add one model's token counts to ``u``.

    Returns ``(added, turn_input)`` where *turn_input* is non-cached + cache-read
    for peak window tracking. Grok splits cached input out as
    ``cachedReadTokens``; non-cached input is ``inputTokens - cachedReadTokens``.
    """
    cached = _as_int(mu.get("cachedReadTokens"))
    raw_input = _as_int(mu.get("inputTokens"))
    i = max(0, raw_input - cached)
    cr = cached
    o = _as_int(mu.get("outputTokens"))
    reasoning = _as_int(mu.get("reasoningTokens"))
    if i + cr + o == 0:
        # Still fold reasoning tokens (billable) even when the visible
        # counters are zero, so an error turn with reasoning-only usage
        # is not silently dropped.
        u.reasoning_output_tokens += reasoning
        return False, 0
    u.input_tokens += i
    u.cache_read_input_tokens += cr
    u.output_tokens += o
    u.reasoning_output_tokens += reasoning
    if model:
        u.models.add(model)
    u.bucket(model or "").add(i, 0, cr, o)
    return True, i + cr


def _resolve_grok_tool_name(update: dict) -> str:
    """Map a ``tool_call`` update to a TCER-canonical tool name.

    Prefer ``_meta["x.ai/tool"].name``; fall back to ``rawInput.variant``,
    ACP ``kind``, then title heuristics. Backend WebSearch often has no
    ``x.ai/tool`` block — only ``kind=search`` + ``rawInput.variant=WebSearch``.
    """
    tool_meta = update.get("_meta", {})
    xt = tool_meta.get("x.ai/tool") if isinstance(tool_meta, dict) else None
    name = xt.get("name") if isinstance(xt, dict) else None
    if isinstance(name, str) and name.strip():
        return _classify_grok_tool(name)

    raw_input = update.get("rawInput")
    if isinstance(raw_input, dict):
        variant = raw_input.get("variant") or raw_input.get("name")
        if isinstance(variant, str) and variant.strip():
            return _classify_grok_tool(variant)

    kind = update.get("kind")
    if isinstance(kind, str) and kind.strip():
        mapped = _GROK_KIND_MAP.get(kind.strip().lower())
        if mapped:
            return mapped

    title = update.get("title")
    if isinstance(title, str):
        t = title.strip().lower()
        if t.startswith("web search"):
            return "WebSearch"
        if t.startswith("web fetch") or t.startswith("fetch "):
            return "WebFetch"

    return "Tool"


def _classify_grok_tool(name) -> str:
    if not isinstance(name, str) or not name:
        return "Tool"
    if name in _GROK_TOOL_MAP:
        return _GROK_TOOL_MAP[name]
    # Case-insensitive fallback (e.g. ``Grep`` / ``GREP`` already canonical).
    low = name.lower()
    if low in _GROK_TOOL_MAP:
        return _GROK_TOOL_MAP[low]
    # Identity for already-canonical TCER names (Read/Edit/…).
    for canon in (
        "Read", "Edit", "Write", "Grep", "Glob", "Bash", "Task",
        "WebSearch", "WebFetch", "TodoWrite",
    ):
        if name == canon or low == canon.lower():
            return canon
    return name


def _path_hint(raw_input) -> str:
    if not isinstance(raw_input, dict):
        return ""
    for key in ("file_path", "target_file", "path", "directory", "workdir", "cwd"):
        val = raw_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _chunk_text(update: dict) -> str:
    """Extract text from a ``*_message_chunk`` / ``*_thought_chunk`` update."""
    content = update.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("text")
            if isinstance(t, str) and t:
                parts.append(t)
            else:
                inner = item.get("content")
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    parts.append(inner["text"])
        return "\n".join(parts)
    return ""


def _as_int(v) -> int:
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _json_label(v) -> str | None:
    if isinstance(v, str) and v:
        return v
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return None


def _first_str(*values) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _display_name_for_cwd(cwd: str | None) -> str:
    if not cwd:
        return _NO_CWD_LABEL
    return Path(cwd).name or cwd
