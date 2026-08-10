"""Oh My Pi (omp) local-session reader.

omp stores local sessions as JSONL under
``~/.omp/agent/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl``. Each file is an
ordered event log; the relevant line types are:

  * ``session``        — one per file: authoritative ``cwd`` / ``id`` / ``title``
  * ``title`` / ``title_change`` — session title (auto or user)
  * ``model_change``   — active model id
  * ``thinking_level_change`` — reasoning effort (``thinkingLevel``, e.g. "high")
  * ``message``        — the turn stream. ``message.role`` is one of
    ``user`` / ``assistant`` / ``toolResult``.

Assistant messages carry a single, non-duplicated ``usage`` block
(``{input, output, cacheRead, cacheWrite, totalTokens, cost}``) — one per API
response, so unlike Claude (message.id dedup) or Codex (total_token_usage
diff) the counts accumulate directly. ``contextSnapshot.promptTokens`` is the
full single-turn input (peak-window signal).

Content blocks inside an assistant message: ``thinking`` / ``text`` /
``toolCall`` (``{id, name, arguments}``). Tool results arrive as
separate ``toolResult`` messages (``{toolCallId, toolName, content, details,
isError}``); the ``edit`` tool's ``details`` carry authoritative
``oldText`` / ``newText`` so LOC needs no hashline-patch parsing.

This maps that stream into TCER's ``TokenUsage`` / ``SessionMeta`` shapes
without touching omp's SQLite state (``agent.db`` etc.). Read-only.
"""
from __future__ import annotations

import json
from pathlib import Path

from tcer.core import pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import (
    as_int as _as_int,
    first_str as _first_str,
    is_correction as _is_correction,
    is_slash_command as _is_slash_command,
)
from tcer.core.paths import encode_hash, omp_sessions_dir
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_NO_CWD_KEY = "__omp_no_cwd__"
_NO_CWD_LABEL = "omp 无工作目录"

# omp tool name -> TCER canonical tool name (matches the Claude/Codex/Grok set
# so ratios like read/write and search/edit compare across sources). Names not
# in this map pass through unchanged (like Codex/Grok specialised tools).
_OMP_TOOL_MAP = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "ast_edit": "Edit",
    "grep": "Grep",
    "search": "Grep",
    "glob": "Glob",
    "find": "Glob",
    "bash": "Bash",
    "eval": "Bash",
    "ssh": "Bash",
    "todo": "TodoWrite",
    "task": "Task",
    "web_search": "WebSearch",
    "ask": "AskUserQuestion",
}

# omp tool names that mutate files (LOC-bearing).
_EDIT_TOOLS = {"write", "edit", "ast_edit"}


def _is_agent_attributed(mm: dict) -> bool:
    """True if a ``user`` message is an agent-internal injection, not real human input.

    omp's advisor (and pi's upstream equivalent) reviews each turn on its own
    model and injects "session update" review prompts back as ``role:"user"``
    deltas in its transcript. omp records those with authoritative markers
    ``synthetic: true`` + ``attribution: "agent"`` precisely so stats never
    count them as user messages (see advisor/transcript-recorder.ts).

    The advisor transcript lives at ``<session>/__advisor*.jsonl`` and is folded
    into the parent like a subagent, so without this guard its review prompts
    inflate ``user_msgs`` and leak into the user-message popup. We trust the
    source markers rather than the ``__advisor`` filename convention: it is the
    authoritative signal and generalises to any future agent-attributed delta.
    """
    return mm.get("synthetic") is True or mm.get("attribution") == "agent"


def iter_events(path: Path):
    """Yield parsed omp session JSONL lines, skipping malformed ones."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def discover_sessions() -> list[Path]:
    """Recursively collect omp session JSONL files (one per session)."""
    base = omp_sessions_dir()
    if not base.is_dir():
        return []
    return sorted(base.rglob("*.jsonl"))


def _subagent_dir(main_file: Path) -> Path:
    """Sibling directory holding a main session's subagent transcripts.

    omp writes each subagent session under ``<sessions-dir>/<stem>/`` where
    ``<stem>`` is the main file's name without ``.jsonl`` (main
    ``<ts>_<uuid>.jsonl`` -> subagents ``<ts>_<uuid>/<name>.jsonl``).
    """
    return main_file.with_suffix("")  # <stem>.jsonl -> <stem>


def _subagent_files(main_file: Path) -> list[Path]:
    """Sorted subagent JSONL files nested under a main session (empty if none)."""
    d = _subagent_dir(main_file)
    if not d.is_dir():
        return []
    try:
        return sorted(d.glob("*.jsonl"))
    except OSError:
        return []


def _is_subagent_file(f: Path) -> bool:
    """True if *f* is a nested subagent transcript (its parent dir = a main stem)."""
    return (f.parent.parent / (f.parent.name + ".jsonl")).is_file()


def _session_line(path: Path) -> dict:
    """Return the ``session`` header line as a dict (``{}`` if absent)."""
    for obj in iter_events(path):
        if obj.get("type") == "session":
            return obj
    return {}


def _normalize_cwd(cwd: str | None) -> str | None:
    """Normalize a cwd path to avoid drive-letter case duplicates (Windows)."""
    if not cwd:
        return cwd
    try:
        return str(Path(cwd).resolve())
    except (OSError, ValueError):
        return cwd


def list_project_refs() -> list[ProjectRef]:
    """Group omp sessions by cwd for the unified project list."""
    groups: dict[str, list[Path]] = {}
    cwd_by_key: dict[str, str | None] = {}
    for p in discover_sessions():
        cwd = _normalize_cwd(_session_line(p).get("cwd"))
        key = encode_hash(cwd) if cwd else _NO_CWD_KEY
        groups.setdefault(key, []).append(p)
        cwd_by_key.setdefault(key, cwd)

    refs: list[ProjectRef] = []
    for key, paths in groups.items():
        cwd = cwd_by_key.get(key)
        refs.append(ProjectRef(
            source="omp",
            key=key,
            display_name=_display_name_for_cwd(cwd),
            cwd=cwd,
            path=Path(cwd) if cwd else None,
            session_paths=tuple(sorted(p for p in paths if not _is_subagent_file(p))),
        ))
    return refs


def resolve_project(project: str) -> ProjectRef | None:
    """Resolve an omp project key/display substring to a project ref."""
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
    """Return the omp *main* session files for a project ref or key.

    Subagent transcripts (nested under ``<stem>/``) are folded into their parent
    by :func:`aggregate_usage` / :func:`_loc_scan`, so they are excluded here to
    avoid double-counting them as separate sessions (AGENTS.md rule #5).
    """
    if isinstance(project, ProjectRef):
        paths = list(project.session_paths)
    else:
        ref = resolve_project(project)
        paths = list(ref.session_paths) if ref else []
    return sorted(p for p in paths if not _is_subagent_file(p))


def read_session_meta(path: Path) -> SessionMeta:
    """Extract lightweight omp session metadata from the event stream."""
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    cli_version: str | None = None
    model_provider: str | None = None
    reasoning_effort: str | None = None
    started_at: int | None = None
    fallback_title: str | None = None

    for obj in iter_events(path):
        typ = obj.get("type")
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if ts is not None:
            started_at = ts if started_at is None else min(started_at, ts)
        if typ == "session":
            session_id = _first_str(obj.get("id")) or session_id
            if isinstance(obj.get("cwd"), str):
                cwd = obj["cwd"]
            title = _first_str(obj.get("title")) or title
            ver = obj.get("version")
            if ver is not None:
                cli_version = f"session v{ver}"
        elif typ in ("title", "title_change"):
            title = _first_str(obj.get("title")) or title
        elif typ == "thinking_level_change":
            level = _first_str(obj.get("thinkingLevel"))
            if level:
                reasoning_effort = level
        elif typ == "message":
            mm = obj.get("message")
            if not isinstance(mm, dict):
                continue
            role = mm.get("role")
            if role == "assistant" and model_provider is None:
                model_provider = _first_str(mm.get("provider"))
            elif role == "user" and fallback_title is None:
                text = _message_text(mm.get("content")).strip()
                if text:
                    fallback_title = truncate_summary(text, 80)

    return SessionMeta(
        session_id=session_id or path.stem,
        cwd=cwd,
        title=title or fallback_title,
        path=path,
        is_subagent=False,
        entrypoint="omp",
        source="omp",
        cli_version=cli_version,
        model_provider=model_provider,
        reasoning_effort=reasoning_effort,
    )


def aggregate_usage(path: Path) -> TokenUsage:
    """Aggregate omp token/tool usage from a main session, folding its subagents.

    omp nests subagent transcripts under a sibling directory named after the
    main file's stem. Their tokens are real cost, so they are merged into the
    parent (AGENTS.md rule #5) rather than counted as separate sessions.
    """
    u = _aggregate_single(Path(path))
    for sub in _subagent_files(Path(path)):
        u = u.merge(_aggregate_single(sub, is_subagent=True))
    return u


def _aggregate_single(path: Path, *, is_subagent: bool = False) -> TokenUsage:
    """Aggregate omp token/tool usage from ONE session JSONL (no subagent merge).

    ``is_subagent`` gates prompt-behaviour signals (slash / correction /
    first_prompt_chars): a subagent's user messages are Task-dispatch prompts,
    not real human input — mirrors the Claude reader's ``is_sub`` guard.
    """
    u = TokenUsage()
    current_model = ""
    turn_idx = 0
    call_id_to_name: dict[str, str] = {}
    active_duration_ms = 0
    total_cost = 0.0
    saw_cost = False

    for obj in iter_events(path):
        typ = obj.get("type")
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if ts is not None:
            u.started_at = ts if u.started_at is None else min(u.started_at, ts)
            u.ended_at = ts if u.ended_at is None else max(u.ended_at, ts)

        if typ == "model_change":
            model = obj.get("model")
            if isinstance(model, str) and model:
                current_model = pricing.normalize(model)
                u.models.add(current_model)
            continue

        if typ != "message":
            continue
        mm = obj.get("message")
        if not isinstance(mm, dict):
            continue
        role = mm.get("role")

        if role == "user":
            # advisor（omp）/上游 pi 的审查 prompt 以 role:"user" 写入其 transcript，
            # 但带 synthetic/attribution=agent 标记 —— 非真人输入，一律不计入任何
            # 用户消息指标（user_msgs / 信号 / 图片）。
            if _is_agent_attributed(mm):
                continue
            text = _message_text(mm.get("content")).strip()
            if text:
                u.user_msgs += 1
                # prompt 行为信号：只计数，不存正文（隐私边界与懒加载一致）。
                # 子代理的 user 消息是 Task 派发 prompt，非真人输入 → 不计。
                if not is_subagent:
                    if _is_slash_command(text):
                        u.slash_command_count += 1
                    elif _is_correction(text):
                        u.correction_msg_count += 1
                    if u.first_prompt_chars == 0:
                        u.first_prompt_chars = len(text)
            # Inline base64 image blocks ({type:"image", mimeType, data}) are
            # multimodal user inputs — counted like Codex/OpenCode image inputs.
            u.image_count += _count_image_blocks(mm.get("content"))
            continue

        if role == "toolResult":
            if mm.get("isError") is True:
                u.tool_errors += 1
                cid = mm.get("toolCallId")
                tname = call_id_to_name.get(cid) if isinstance(cid, str) else None
                if tname:
                    u.tool_errors_by_tool[tname] = u.tool_errors_by_tool.get(tname, 0) + 1
            continue

        if role != "assistant":
            continue

        # One assistant message == one API response == one turn.
        model = _first_str(mm.get("model"))
        if model:
            current_model = pricing.normalize(model)
            u.models.add(current_model)

        # A turn interrupted by the user or the runtime ends with
        # stopReason == "aborted" (analogous to Codex's turn_aborted event).
        # errorMessage carries a clean reason ("Interrupted by user",
        # "Operation aborted"); fall back to a generic key when absent.
        if mm.get("stopReason") == "aborted":
            u.aborted_task_count += 1
            reason = _first_str(mm.get("errorMessage")) or "aborted"
            u.abort_reasons[reason] = u.abort_reasons.get(reason, 0) + 1

        for block in mm.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                u.thinking_count += 1
            elif btype == "toolCall":
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                tool_name = _classify_omp_tool(name)
                u.tool_calls[tool_name] = u.tool_calls.get(tool_name, 0) + 1
                if tool_name == "WebSearch":
                    u.web_search_count += 1
                cid = block.get("id")
                if isinstance(cid, str):
                    call_id_to_name[cid] = tool_name
                u.tool_ops.append(ToolOp(turn_idx, tool_name, _path_hint(block.get("arguments"))))

        usage = mm.get("usage")
        if isinstance(usage, dict):
            _add_turn_usage(u, usage, current_model, turn_idx, ts)
            cost = usage.get("cost")
            if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
                total_cost += float(cost["total"])
                saw_cost = True

        snap = mm.get("contextSnapshot")
        if isinstance(snap, dict):
            prompt = _as_int(snap.get("promptTokens"))
            if prompt > u.peak_input_tokens:
                u.peak_input_tokens = prompt

        dur = _as_int(mm.get("duration"))
        if dur > 0:
            active_duration_ms += dur
            if u.turn_stats and u.turn_stats[-1].duration_ms is None:
                u.turn_stats[-1].duration_ms = dur
        ttft = _as_int(mm.get("ttft"))
        if ttft > 0:
            u.time_to_first_token_ms = (
                ttft if u.time_to_first_token_ms is None
                else min(u.time_to_first_token_ms, ttft))
            u.ttft_ms_samples.append(ttft)

        turn_idx += 1

    if saw_cost:
        u.reported_cost_usd = total_cost
    if active_duration_ms > 0:
        u.session_duration_ms = active_duration_ms
    elif u.started_at and u.ended_at:
        u.session_duration_ms = u.ended_at - u.started_at
    return u


def read_user_messages(path: Path) -> list[str]:
    """Extract omp user-message text on demand (main session + folded subagents)."""
    messages = _read_user_messages_single(Path(path))
    for sub in _subagent_files(Path(path)):
        messages.extend(_read_user_messages_single(sub))
    return messages


def _read_user_messages_single(path: Path) -> list[str]:
    """Extract omp user-message text from ONE session JSONL."""
    messages: list[str] = []
    for obj in iter_events(path):
        if obj.get("type") != "message":
            continue
        mm = obj.get("message")
        if not isinstance(mm, dict) or mm.get("role") != "user":
            continue
        # advisor / 上游 pi 审查 prompt（synthetic/attribution=agent）不是真人消息，
        # 不能出现在用户消息弹窗里。
        if _is_agent_attributed(mm):
            continue
        text = _message_text(mm.get("content")).strip()
        if text:
            messages.append(truncate_summary(text, 500))
    return messages


def read_conversation(path: Path) -> list[dict]:
    """Extract the full ordered conversation from an omp session JSONL.

    Mirrors :func:`tcer.core.reader.read_conversation`'s output shape so the server
    session view can render every source uniformly.
    """
    convo: list[dict] = []
    for obj in iter_events(path):
        if obj.get("type") != "message":
            continue
        ts = parse_timestamp_ms(obj.get("timestamp"))
        mm = obj.get("message")
        if not isinstance(mm, dict):
            continue
        role = mm.get("role")
        if role == "user":
            # advisor / 上游 pi 审查 prompt 非真人输入 → 不并入会话视图的用户消息。
            if _is_agent_attributed(mm):
                continue
            text = _message_text(mm.get("content")).strip()
            if text:
                convo.append({"role": "user", "type": "text", "text": text, "ts": ts})
        elif role == "assistant":
            for block in mm.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = _first_str(block.get("text"))
                    if text:
                        convo.append({"role": "assistant", "type": "text",
                                      "text": text, "ts": ts})
                elif btype == "thinking":
                    text = _first_str(block.get("thinking"), block.get("text"))
                    if text:
                        convo.append({"role": "assistant", "type": "thinking",
                                      "text": text, "ts": ts})
                elif btype == "toolCall":
                    name = block.get("name")
                    convo.append({
                        "role": "assistant", "type": "tool_use",
                        "name": _classify_omp_tool(name) if isinstance(name, str) else "Tool",
                        "id": block.get("id"),
                        "input": block.get("arguments") if isinstance(block.get("arguments"), dict) else {},
                        "ts": ts,
                    })
        elif role == "toolResult":
            convo.append({
                "role": "tool", "type": "tool_result",
                "tool_use_id": mm.get("toolCallId"),
                "is_error": bool(mm.get("isError")),
                "text": _toolresult_text(mm.get("content")),
                "ts": ts,
            })
    return convo


def _loc_scan(path: Path):
    """Single pass returning ``(SessionLoc, has_signal)`` for a main session + subagents.

    Subagent LOC is merged via :func:`tcer.core.loc.merge_session_locs` so
    self-rework / high-churn files match the Claude/Grok Edit/Write semantics
    across the whole folded session.
    """
    from tcer.core.loc import SessionLoc, merge_session_locs

    sloc, has_signal = _loc_scan_single(Path(path))
    subs = _subagent_files(Path(path))
    if not subs:
        return sloc, has_signal
    slocs = [sloc] if has_signal else []
    any_signal = has_signal
    for sub in subs:
        sl, hs = _loc_scan_single(sub)
        if hs:
            slocs.append(sl)
            any_signal = True
    if not slocs:
        return SessionLoc(added=0, deleted=0), False
    return merge_session_locs(slocs), any_signal


def _loc_scan_single(path: Path):
    """Single pass over ONE session's messages returning ``(SessionLoc, has_signal)``.

    ``has_signal`` is True if any parseable ``write`` / ``edit`` mutation exists
    (independent of code-file filtering). LOC is replayed through the shared
    ``_LocAccumulator`` so self-rework (churn) and high-churn files match the
    Claude/Grok Edit/Write semantics.

    Source of truth per tool:
      * ``write`` — toolCall ``arguments.content`` (full new file text). A first
        Write to an unseen path is charged old=0 (``unseen_writes`` exposure),
        matching Grok — omp emits no pre-write original snapshot.
      * ``edit`` / ``ast_edit`` — the toolResult ``details.oldText`` /
        ``details.newText`` (authoritative before/after), joined by
        ``toolCallId``. No hashline-patch parsing needed.
    """
    from tcer.core.loc import SessionLoc, _LocAccumulator, _is_code

    acc = _LocAccumulator()
    has_signal = False
    # toolCallId -> (omp tool name, arguments) captured from assistant toolCalls.
    pending: dict[str, tuple[str, dict]] = {}

    for obj in iter_events(path):
        if obj.get("type") != "message":
            continue
        mm = obj.get("message")
        if not isinstance(mm, dict):
            continue
        role = mm.get("role")

        if role == "assistant":
            for block in mm.get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                name = block.get("name")
                cid = block.get("id")
                if name in _EDIT_TOOLS and isinstance(cid, str):
                    args = block.get("arguments")
                    pending[cid] = (name, args if isinstance(args, dict) else {})
            continue

        if role != "toolResult" or mm.get("isError") is True:
            continue
        cid = mm.get("toolCallId")
        if not isinstance(cid, str) or cid not in pending:
            continue
        name, args = pending.pop(cid)
        details = mm.get("details") if isinstance(mm.get("details"), dict) else {}

        if name == "write":
            fp = _first_str(details.get("resolvedPath"), args.get("path"))
            if not fp:
                continue
            has_signal = True
            if not _is_code(fp):
                continue
            content = args.get("content")
            acc.on_tool_use("Write", {
                "file_path": fp,
                "content": content if isinstance(content, str) else "",
            })
        else:  # edit / ast_edit → authoritative before/after from details
            fp = _first_str(details.get("path"), details.get("resolvedPath"))
            old_text = details.get("oldText")
            new_text = details.get("newText")
            if not fp or not (isinstance(old_text, str) or isinstance(new_text, str)):
                continue
            has_signal = True
            if not _is_code(fp):
                continue
            acc.on_tool_use("Edit", {
                "file_path": fp,
                "old_string": old_text if isinstance(old_text, str) else "",
                "new_string": new_text if isinstance(new_text, str) else "",
            })

    sloc = acc.finish() if has_signal else SessionLoc(added=0, deleted=0)
    return sloc, has_signal


def session_loc_full(path: Path):
    """Return LOC from parseable omp write/edit calls only."""
    return _loc_scan(path)[0]


def has_loc_signal(path: Path) -> bool:
    """True if the session contains a parseable write/edit mutation."""
    return _loc_scan(path)[1]


# --------------------------------------------------------------------------- helpers

def _add_turn_usage(u: TokenUsage, usage: dict, model: str, turn: int, ts: int | None) -> None:
    """Fold one assistant ``usage`` block into ``u`` (+ a TurnStat step).

    omp/Pi usage semantics mirror Anthropic: ``input`` excludes cache, with
    ``cacheRead`` / ``cacheWrite`` reported separately (verified:
    input + output == totalTokens when cache is 0). Pi additionally exposes
    ``cacheWrite1h`` (the 1h-cache-write subset, priced via
    ``CACHE_1H_PREMIUM``) and ``reasoning`` (kept separately in
    ``reasoning_output_tokens``; ``output`` already includes it for cost, per
    the models.py convention — audit checks the token-consistency invariant).
    omp data has neither field, so both read 0 and omp behaviour is unchanged.
    """
    i = _as_int(usage.get("input"))
    cr = _as_int(usage.get("cacheRead"))
    cw = _as_int(usage.get("cacheWrite"))
    o = _as_int(usage.get("output"))
    cw1h = _as_int(usage.get("cacheWrite1h"))   # Pi 1h-cache-write subset
    reasoning = _as_int(usage.get("reasoning"))  # Pi reasoning output tokens
    if i + cr + cw + o + cw1h + reasoning == 0:
        u.empty_usage_skipped += 1
        return
    u.assistant_msgs += 1
    u.input_tokens += i
    u.cache_read_input_tokens += cr
    u.cache_creation_input_tokens += cw
    u.cache_write_1h_tokens += cw1h
    u.output_tokens += o
    u.reasoning_output_tokens += reasoning
    key = model or ""
    u.bucket(key).add(i, cw, cr, o, cw1h)
    u.turn_stats.append(TurnStat(
        turn=turn, ts=ts,
        input_tokens=i, cache_read=cr, output_tokens=o,
        model=key,
    ))


def _classify_omp_tool(name: str) -> str:
    """Map an omp tool name to a TCER-canonical name (pass through if unmapped)."""
    if not isinstance(name, str) or not name:
        return "Tool"
    return _OMP_TOOL_MAP.get(name, name)


def _path_hint(arguments) -> str:
    """Best-effort file path from a toolCall's arguments (``""`` if none)."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("file_path", "path", "workdir"):
        val = arguments.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _count_image_blocks(content) -> int:
    """Count inline image blocks in an omp/pi ``message.content`` list.

    omp/pi carry pasted images as ``{type:"image", mimeType, data}`` blocks on
    user messages (base64 payload). These are multimodal inputs, tallied into
    ``image_count`` like Codex ``images`` / OpenCode image file parts.
    """
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content
               if isinstance(b, dict)
               and b.get("type") in ("image", "image_url", "input_image"))


def _message_text(content) -> str:
    """Flatten an omp ``message.content`` (string or list of text blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for it in content:
        if isinstance(it, dict) and isinstance(it.get("text"), str) and it["text"]:
            parts.append(it["text"])
        elif isinstance(it, str) and it:
            parts.append(it)
    return "\n".join(parts)


def _toolresult_text(content) -> str:
    """Flatten a ``toolResult.content`` array to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for it in content:
        if isinstance(it, dict) and isinstance(it.get("text"), str):
            parts.append(it["text"])
    return "\n".join(parts)


def _display_name_for_cwd(cwd: str | None) -> str:
    if not cwd:
        return _NO_CWD_LABEL
    return Path(cwd).name or cwd