"""JSONL discovery, parsing and token-usage aggregation.

Structure ported from cc-switch's ``session_manager/providers/claude.rs`` and
``utils.rs``; the usage aggregation is TCER's own addition (cc-switch only renders
conversations and never reads ``message.usage``).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from tcer.core.models import SessionMeta, ToolOp, TokenUsage
from tcer.core.paths import projects_dir
from tcer.core import pricing

# Title-extraction noise to skip, matching cc-switch's filters.
_TITLE_NOISE_PREFIXES = ("<local-command-caveat", "<command-name>", "<ide_opened_file>",
                         "<command-message>", "/clear")
# After tag removal, skip these system-generated phrases
_TITLE_NOISE_AFTER_CLEAN = ("The user opened the file", "You are an expert")
TITLE_MAX_CHARS = 80

_TAG_RE = re.compile(r'<[^>]+>')


def _strip_tags(txt: str) -> str:
    """Remove XML/HTML-like tags (e.g. ``<ide_opened_file>…</ide_opened_file>``)."""
    return _TAG_RE.sub('', txt).strip()


def discover_jsonl(project_hash: str | None = None) -> list[Path]:
    """Recursively collect every ``*.jsonl`` under a project (or all projects)."""
    base = projects_dir()
    if project_hash:
        base = base / project_hash
    if not base.is_dir():
        return []
    return sorted(base.rglob("*.jsonl"))


def is_subagent(path: Path) -> bool:
    """True if the jsonl lives under a ``subagents/`` directory."""
    return "subagents" in path.parts


def parent_session_id(path: Path) -> str:
    """Return the parent session id a jsonl belongs to.

    Subagent files live at ``<sessionId>/subagents/agent-*.jsonl`` — their parent
    is the directory segment just before ``subagents``. Main session files map to
    their own stem. Used to fold subagent data into the owning session.
    """
    parts = path.parts
    if "subagents" in parts:
        idx = parts.index("subagents")
        if idx > 0:
            return parts[idx - 1]
    return path.stem


def iter_messages(path: Path):
    """Yield each parsed JSON object in a session jsonl, skipping meta/garbage lines."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("isMeta") is True:
                continue
            yield obj


def aggregate_usage(path: Path) -> TokenUsage:
    """Sum token usage across all assistant turns in one session file.

    **Dedup by ``message.id``**: one assistant API response is often split across
    several JSONL lines — one per content block (thinking / text / each tool_use) —
    and *every* line repeats the same ``message.usage``. Counting each line would
    multi-count both tokens and turns (observed up to 6× on Bedrock-routed
    sessions). We count each ``message.id`` once. Lines without an id fall back to
    being counted individually. (ccusage / token-stats dedup the same way.)

    Turns whose usage is entirely zero (e.g. pure-thinking stubs) are counted in
    ``empty_usage_skipped`` and their tokens are not accumulated.  They are NOT
    included in ``assistant_msgs`` — only turns with real token usage count as
    assistant turns, ensuring consistent turn counts across models (one API
    response = one turn, regardless of how many JSONL lines it spans).
    ``effective_turns`` equals ``assistant_msgs`` (no subtraction needed).

    **Time window**: tracks ``started_at`` / ``ended_at`` from *all* assistant turns
    (including zero-usage ones) so sessions with only zero-usage replies still get
    timestamps (needed for accurate git-ground-truth in calibration and for GUI time
    sorting).

    **Tool calls**: counts each tool_use block by name (NOT deduped by message.id,
    since multiple tool_use blocks in one response are genuine separate calls).
    """
    u = TokenUsage()
    seen: set[str] = set()
    call_id_to_name: dict[str, str] = {}  # tool_use_id → tool_name for error attribution
    turn_idx = 0  # assistant message sequence for temporal analysis
    for obj in iter_messages(path):
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue

        role = msg.get("role")

        # Count user messages and extract text
        if role == "user":
            content = msg.get("content")
            # Only count real user messages (with text), not tool_result returns.
            # In JSONL, tool results are sent as role="user" but contain only
            # tool_result blocks — real user input has text blocks.
            is_real_user = False
            if isinstance(content, str) and content.strip():
                is_real_user = True
            elif isinstance(content, list):
                is_real_user = any(
                    isinstance(it, dict) and it.get("type") == "text"
                    for it in content
                )
            if is_real_user:
                u.user_msgs += 1
                # Extract user message text for popup display
                txt = extract_text(content).strip()
                if txt:
                    txt = _strip_tags(txt)
                if txt and not txt.startswith(_TITLE_NOISE_PREFIXES):
                    u.user_message_texts.append(txt[:500])
            # Count tool_result errors (from ALL user-role messages)
            if isinstance(content, list):
                for item in content:
                    if (isinstance(item, dict)
                            and item.get("type") == "tool_result"
                            and item.get("is_error")):
                        u.tool_errors += 1
                        # Attribute error to specific tool via call_id mapping
                        tid = item.get("tool_use_id")
                        if isinstance(tid, str):
                            tname = call_id_to_name.get(tid)
                            if tname:
                                u.tool_errors_by_tool[tname] = u.tool_errors_by_tool.get(tname, 0) + 1

        # Process assistant messages: dedup by message.id for token counting
        if role == "assistant":
            mid = msg.get("id")
            if isinstance(mid, str) and mid:  # skip empty string → treat as no id
                if mid in seen:
                    continue  # same API response, already counted for usage
                seen.add(mid)
            # Track time window from all assistant turns (even zero-usage ones).
            ts = parse_timestamp_ms(obj.get("timestamp"))
            if ts is not None:
                u.started_at = ts if u.started_at is None else min(u.started_at, ts)
                u.ended_at = ts if u.ended_at is None else max(u.ended_at, ts)
            usage = msg.get("usage") or {}
            i = _as_int(usage.get("input_tokens"))
            cw = _as_int(usage.get("cache_creation_input_tokens"))
            cr = _as_int(usage.get("cache_read_input_tokens"))
            o = _as_int(usage.get("output_tokens"))
            # Count assistant turns: only lines with real usage count as turns.
            # Zero-usage stubs (mimo thinking blocks, synthetic stubs) are tracked
            # separately in empty_usage_skipped and do not inflate assistant_msgs.
            # This ensures consistent turn counts across models: one API response
            # = one turn, regardless of how many JSONL lines it spans.
            if i + cw + cr + o == 0:
                u.empty_usage_skipped += 1
                # Release the id lock so a later line with the same message.id
                # can contribute real tokens.  ccswitch writes mimo messages as
                # two JSONL lines: first a thinking-only stub (usage=0), then
                # the real response with actual token counts — same id.
                if isinstance(mid, str) and mid:
                    seen.discard(mid)
            else:
                u.assistant_msgs += 1
                u.input_tokens += i
                u.cache_creation_input_tokens += cw
                u.cache_read_input_tokens += cr
                u.output_tokens += o
            model = msg.get("model")
            # Skip synthetic stubs (ccswitch 429 errors, "No response requested")
            # — they use the same message.model field but are not real model turns.
            if isinstance(model, str) and model and model != "<synthetic>":
                u.models.add(model)
                bucket_key = pricing.normalize(model)
            else:
                bucket_key = ""
            u.bucket(bucket_key).add(i, cw, cr, o)

            # Extract tool_use / thinking blocks from content
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "tool_use":
                        tool_name = item.get("name")
                        if isinstance(tool_name, str):
                            u.tool_calls[tool_name] = u.tool_calls.get(tool_name, 0) + 1
                            # Map call id → tool name for error attribution
                            cid = item.get("id")
                            if isinstance(cid, str):
                                call_id_to_name[cid] = tool_name
                            # Record tool op for temporal analysis
                            inp = item.get("input")
                            if isinstance(inp, dict):
                                fp = inp.get("file_path") or inp.get("notebook_path")
                            else:
                                fp = None
                            u.tool_ops.append(ToolOp(
                                turn=turn_idx,
                                tool=tool_name,
                                path=fp if isinstance(fp, str) else "",
                            ))
                    elif item_type == "thinking":
                        u.thinking_count += 1
            turn_idx += 1

    # Compute session_duration_ms from the time window
    if u.started_at and u.ended_at:
        u.session_duration_ms = u.ended_at - u.started_at

    return u


def read_session_meta(path: Path) -> SessionMeta:
    """Extract session metadata cheaply via head/tail sampling (for list views).

    Ports cc-switch's ``read_head_tail_lines`` + ``parse_session``: read the first
    ``head_n`` lines and last ``tail_n`` lines only, so listing hundreds of sessions
    doesn't require scanning whole files.

    **Title source = the AI-generated title** (matches VSCode Claude Code's session
    list). Claude Code rewrites the ``ai-title`` line repeatedly as the conversation
    grows, so the *newest* title lives furthest down the file — i.e. in the tail. We
    therefore pick by priority: last ``aiTitle`` in the tail, else last ``aiTitle``
    in the head, else the first real user message (VSCode's pending-title behaviour).
    Tail must outrank head — head lines are older, so a stale head title must never
    overwrite a fresher tail one.
    """
    head, tail = _read_head_tail_lines(path, head_n=20, tail_n=30)
    session_id: str | None = None
    cwd: str | None = None
    entrypoint: str | None = None

    # Newest ai-title in the tail wins. Keep overwriting → the last non-empty
    # aiTitle in the tail is the freshest the file has.
    tail_title: str | None = None
    for line in tail:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "ai-title":
            t = obj.get("aiTitle")
            if isinstance(t, str) and t.strip():
                tail_title = t.strip()

    head_title: str | None = None
    fallback_title: str | None = None
    for line in head:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "ai-title":
            t = obj.get("aiTitle")
            if isinstance(t, str) and t.strip():
                head_title = t.strip()
        if session_id is None:
            sid = obj.get("sessionId")
            if isinstance(sid, str):
                session_id = sid
        if cwd is None:
            c = obj.get("cwd")
            if isinstance(c, str):
                cwd = c
        if entrypoint is None:
            ep = obj.get("entrypoint")
            if isinstance(ep, str):
                entrypoint = ep
        # First real user message — only used when no ai-title exists at all.
        if fallback_title is None:
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("role") == "user":
                txt = extract_text(msg.get("content")).strip()
                if txt and not txt.startswith(_TITLE_NOISE_PREFIXES):
                    # Remove all XML-like tags (e.g. <ide_opened_file>...</ide_opened_file>)
                    txt = _strip_tags(txt)
                    # Skip system-generated phrases after cleaning
                    if txt and not txt.startswith(_TITLE_NOISE_AFTER_CLEAN):
                        fallback_title = txt

    # Priority: newest tail ai-title > head ai-title > first user message.
    title = tail_title or head_title or fallback_title
    if title:
        title = truncate_summary(title, TITLE_MAX_CHARS)
    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=path,
        is_subagent=is_subagent(path),
        entrypoint=entrypoint,
    )


# --------------------------------------------------------------------------- #
# Helpers ported from cc-switch utils.rs
# --------------------------------------------------------------------------- #
def _read_head_tail_lines(path: Path, head_n: int, tail_n: int) -> tuple[list[str], list[str]]:
    """Read the first ``head_n`` and last ``tail_n`` lines efficiently.

    For small files (<16 KiB) reads everything once; for larger files seeks to the
    last ~16 KiB for the tail to avoid scanning the whole file.
    """
    size = path.stat().st_size
    if size < 16_384:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        head = all_lines[:head_n]
        skip = max(0, len(all_lines) - tail_n)
        tail = all_lines[skip:]
        return [l.rstrip("\n") for l in head], [l.rstrip("\n") for l in tail]

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        head = [fh.readline().rstrip("\n") for _ in range(head_n)]

    seek_pos = max(0, size - 16_384)
    with path.open("rb") as fb:
        fb.seek(seek_pos)
        if seek_pos > 0:
            fb.readline()  # discard the possibly-partial first line
        raw = fb.read().decode("utf-8", errors="replace")
    tail_lines = raw.splitlines()
    skip = max(0, len(tail_lines) - tail_n)
    tail = tail_lines[skip:]
    return head, tail


def parse_timestamp_ms(value) -> int | None:
    """Normalize a timestamp to epoch milliseconds.

    Accepts integers/floats (ms if >1e12, else seconds), numeric strings, and
    RFC3339 strings, matching cc-switch's ``parse_timestamp_to_ms``. Returns None
    for anything unparseable (OSError is possible on Windows for out-of-range dates).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _ms_from_number(int(value))
    if isinstance(value, str):
        s = value.strip()
        # numeric string?
        try:
            return _ms_from_number(int(s))
        except ValueError:
            pass
        try:
            return _ms_from_number(int(float(s)))
        except ValueError:
            pass
        # RFC3339
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _ms_from_number(n: int) -> int:
    return n if n > 1_000_000_000_000 else n * 1000


def extract_text(content) -> str:
    """Extract a flat text string from a message ``content`` field.

    Handles string / array / object shapes and surfaces tool_use/tool_result, as
    cc-switch's ``extract_text`` does.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            t = _extract_text_from_item(item)
            if t and t.strip():
                parts.append(t)
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text", "") or ""
    return ""


def _extract_text_from_item(item: dict) -> str | None:
    item_type = item.get("type", "")
    if item_type == "tool_use":
        return f"[Tool: {item.get('name', 'unknown')}]"
    if item_type == "tool_result":
        nested = extract_text(item.get("content"))
        return nested or None
    for key in ("text", "input_text", "output_text"):
        v = item.get(key)
        if isinstance(v, str):
            return v
    nested = extract_text(item.get("content"))
    return nested or None


def truncate_summary(text: str, max_chars: int) -> str:
    trimmed = text.strip()
    if not trimmed:
        return ""
    if len(trimmed) <= max_chars:
        return trimmed
    return trimmed[:max_chars] + "..."


def _as_int(v) -> int:
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
