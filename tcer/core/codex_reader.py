"""Codex local-session reader.

Codex stores local sessions as JSONL under ``~/.codex/sessions/YYYY/MM/DD``.
This module maps that event stream into TCER's existing ``TokenUsage`` /
``SessionMeta`` shapes without touching Codex's SQLite state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tcer.core import pricing
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp
from tcer.core.paths import codex_dir, codex_sessions_dir, encode_hash
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_NO_CWD_KEY = "__codex_no_cwd__"
_NO_CWD_LABEL = "Codex 无工作目录"
_EXIT_RE = re.compile(r"Process exited with code\s+(-?\d+)")


def iter_events(path: Path):
    """Yield parsed Codex JSONL events, skipping malformed lines."""
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
    """Recursively collect Codex session JSONL files."""
    base = codex_sessions_dir()
    if not base.is_dir():
        return []
    return sorted(base.rglob("*.jsonl"))


def _index_titles() -> dict[str, str]:
    """Read ``session_index.jsonl`` as session id -> thread title."""
    p = codex_dir() / "session_index.jsonl"
    if not p.is_file():
        return {}
    titles: dict[str, str] = {}
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = obj.get("id")
            title = obj.get("thread_name")
            if isinstance(sid, str) and isinstance(title, str) and title.strip():
                titles[sid] = title.strip()
    return titles


def list_project_refs() -> list[ProjectRef]:
    """Group Codex sessions by cwd for the unified project list."""
    groups: dict[str, list[Path]] = {}
    cwd_by_key: dict[str, str | None] = {}
    for p in discover_sessions():
        meta = read_session_meta(p)
        cwd = meta.cwd
        key = encode_hash(cwd) if cwd else _NO_CWD_KEY
        groups.setdefault(key, []).append(p)
        cwd_by_key.setdefault(key, cwd)

    refs: list[ProjectRef] = []
    for key, paths in groups.items():
        cwd = cwd_by_key.get(key)
        refs.append(ProjectRef(
            source="codex",
            key=key,
            display_name=_display_name_for_cwd(cwd),
            cwd=cwd,
            path=Path(cwd) if cwd else None,
            session_paths=tuple(sorted(paths)),
        ))
    return refs


def resolve_project(project: str) -> ProjectRef | None:
    """Resolve a Codex project key/display substring to a project ref."""
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
    """Return the Codex session files for a project ref or key."""
    if isinstance(project, ProjectRef):
        return list(project.session_paths)
    ref = resolve_project(project)
    return list(ref.session_paths) if ref else []


def read_session_meta(path: Path) -> SessionMeta:
    """Extract lightweight Codex session metadata."""
    session_id: str | None = None
    cwd: str | None = None
    originator: str | None = None
    source_label: str | None = None
    cli_version: str | None = None
    model_provider: str | None = None
    thread_source: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    git_repository: str | None = None
    approval_policy: str | None = None
    sandbox_policy: str | None = None
    permission_profile: str | None = None
    collaboration_mode: str | None = None
    reasoning_effort: str | None = None
    fallback_title: str | None = None
    started_at: int | None = None

    for obj in iter_events(path):
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if ts is not None and started_at is None:
            started_at = ts
        typ = obj.get("type")
        payload = obj.get("payload")
        if typ == "session_meta" and isinstance(payload, dict):
            session_id = _first_str(payload.get("session_id"), payload.get("id")) or session_id
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
            originator = payload.get("originator") if isinstance(payload.get("originator"), str) else originator
            source_label = payload.get("source") if isinstance(payload.get("source"), str) else source_label
            cli_version = payload.get("cli_version") if isinstance(payload.get("cli_version"), str) else cli_version
            model_provider = payload.get("model_provider") if isinstance(payload.get("model_provider"), str) else model_provider
            thread_source = payload.get("thread_source") if isinstance(payload.get("thread_source"), str) else thread_source
            git = payload.get("git")
            if isinstance(git, dict):
                git_branch = git.get("branch") if isinstance(git.get("branch"), str) else git_branch
                git_commit = git.get("commit_hash") if isinstance(git.get("commit_hash"), str) else git_commit
                git_repository = git.get("repository_url") if isinstance(git.get("repository_url"), str) else git_repository
        elif typ == "turn_context" and isinstance(payload, dict):
            approval_policy = _json_label(payload.get("approval_policy")) or approval_policy
            sandbox_policy = _json_label(payload.get("sandbox_policy")) or sandbox_policy
            permission_profile = _json_label(payload.get("permission_profile")) or permission_profile
            collaboration_mode = _json_label(payload.get("collaboration_mode")) or collaboration_mode
            reasoning_effort = _json_label(payload.get("effort")) or reasoning_effort
        elif typ == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "user_message" and fallback_title is None:
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    fallback_title = truncate_summary(msg.strip(), 80)

    if session_id is None:
        session_id = _session_id_from_filename(path)
    title = _index_titles().get(session_id or "") or fallback_title
    entrypoint = originator or source_label
    return SessionMeta(
        session_id=session_id,
        cwd=cwd,
        title=title,
        path=path,
        is_subagent=False,
        entrypoint=entrypoint,
        source="codex",
        cli_version=cli_version,
        model_provider=model_provider,
        thread_source=thread_source,
        git_branch=git_branch,
        git_commit=git_commit,
        git_repository=git_repository,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
        permission_profile=permission_profile,
        collaboration_mode=collaboration_mode,
        reasoning_effort=reasoning_effort,
    )


def aggregate_usage(path: Path) -> TokenUsage:
    """Aggregate Codex token and tool usage from one session JSONL."""
    u = TokenUsage()
    current_model = ""
    turn_idx = 0
    call_id_to_name: dict[str, str] = {}
    active_duration_ms = 0

    for obj in iter_events(path):
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if ts is not None:
            u.started_at = ts if u.started_at is None else min(u.started_at, ts)
            u.ended_at = ts if u.ended_at is None else max(u.ended_at, ts)

        typ = obj.get("type")
        payload = obj.get("payload")
        if typ == "turn_context" and isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str) and model:
                current_model = pricing.normalize(model)
                u.models.add(current_model)
            _set_max(u, "model_context_window", payload.get("model_context_window"))
            continue

        if typ == "event_msg" and isinstance(payload, dict):
            ptype = payload.get("type")
            if ptype == "user_message":
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    u.user_msgs += 1
                    # Privacy boundary: keep Codex message text out of the
                    # report object until the user explicitly opens the popup.
                u.image_count += _list_len(payload.get("images"))
                u.local_image_count += _list_len(payload.get("local_images"))
            elif ptype == "task_started":
                u.task_count += 1
                u.tool_calls["Task"] = u.tool_calls.get("Task", 0) + 1
                _set_max(u, "model_context_window", payload.get("model_context_window"))
                started = parse_timestamp_ms(payload.get("started_at"))
                if started is not None:
                    u.started_at = started if u.started_at is None else min(u.started_at, started)
            elif ptype == "task_complete":
                u.completed_task_count += 1
                completed = parse_timestamp_ms(payload.get("completed_at"))
                if completed is not None:
                    u.ended_at = completed if u.ended_at is None else max(u.ended_at, completed)
                active_duration_ms += _as_int(payload.get("duration_ms"))
                ttft = _as_int(payload.get("time_to_first_token_ms"))
                if ttft > 0:
                    u.time_to_first_token_ms = ttft if u.time_to_first_token_ms is None else min(u.time_to_first_token_ms, ttft)
            elif ptype == "turn_aborted":
                u.aborted_task_count += 1
                active_duration_ms += _as_int(payload.get("duration_ms"))
            elif ptype == "context_compacted":
                u.compaction_event_count += 1
            elif ptype == "web_search_end":
                u.web_search_end_count += 1
            elif ptype == "patch_apply_end":
                u.patch_apply_count += 1
                if payload.get("success") is True or payload.get("status") == "success":
                    u.patch_apply_success_count += 1
            elif ptype == "token_count":
                info = payload.get("info", {})
                if isinstance(info, dict):
                    _set_max(u, "model_context_window", info.get("model_context_window"))
                _add_rate_limit(u, payload.get("rate_limits"))
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if isinstance(usage, dict):
                    _add_token_usage(u, usage, current_model)
                    turn_idx += 1
            continue

        if typ != "response_item" or not isinstance(payload, dict):
            if typ == "compacted":
                u.compaction_count += 1
            continue
        ptype = payload.get("type")
        if ptype == "function_call":
            name = payload.get("name")
            if isinstance(name, str):
                tool_name, path_hint = _classify_tool(name, payload.get("arguments"))
                u.tool_calls[tool_name] = u.tool_calls.get(tool_name, 0) + 1
                cid = payload.get("call_id")
                if isinstance(cid, str):
                    call_id_to_name[cid] = tool_name
                u.tool_ops.append(ToolOp(turn_idx, tool_name, path_hint))
        elif ptype == "function_call_output":
            output = payload.get("output")
            code = _exit_code(output if isinstance(output, str) else "")
            if code is not None and code != 0:
                u.tool_errors += 1
                cid = payload.get("call_id")
                tname = call_id_to_name.get(cid) if isinstance(cid, str) else None
                if tname:
                    u.tool_errors_by_tool[tname] = u.tool_errors_by_tool.get(tname, 0) + 1
        elif ptype == "reasoning":
            u.thinking_count += 1
        elif ptype == "web_search_call":
            u.web_search_count += 1
        elif ptype == "custom_tool_call":
            name = payload.get("name")
            tool_name = str(name) if isinstance(name, str) and name else "CustomTool"
            u.tool_calls[tool_name] = u.tool_calls.get(tool_name, 0) + 1
        elif ptype == "custom_tool_call_output":
            output = payload.get("output")
            if isinstance(output, str) and ("error" in output.lower() or "failed" in output.lower()):
                u.tool_errors += 1

    if u.web_search_count == 0 and u.web_search_end_count:
        u.web_search_count = u.web_search_end_count
    if u.compaction_count == 0 and u.compaction_event_count:
        u.compaction_count = u.compaction_event_count

    if active_duration_ms > 0:
        u.session_duration_ms = active_duration_ms
    elif u.started_at and u.ended_at:
        u.session_duration_ms = u.ended_at - u.started_at
    return u


def read_user_messages(path: Path) -> list[str]:
    """Extract Codex user-message text on demand for the popup."""
    messages: list[str] = []
    for obj in iter_events(path):
        payload = obj.get("payload")
        if obj.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "user_message":
            continue
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            messages.append(truncate_summary(msg.strip(), 500))
    return messages


def session_loc_full(path: Path):
    """Return LOC from parseable Codex apply_patch calls only."""
    from tcer.core.loc import SessionLoc, _is_code

    added = deleted = 0
    file_edit_counts: dict[str, int] = {}
    test_added = test_deleted = doc_added = doc_deleted = 0
    for obj in iter_events(path):
        payload = obj.get("payload")
        if obj.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "function_call" or payload.get("name") != "apply_patch":
            continue
        patch = _extract_patch(payload.get("arguments"))
        if not patch:
            continue
        for fp, a, d in _patch_file_deltas(patch):
            if not _is_code(fp):
                continue
            added += a
            deleted += d
            file_edit_counts[fp] = file_edit_counts.get(fp, 0) + 1
            norm = fp.replace("\\", "/").lower()
            if "/test/" in norm or "/tests/" in norm or norm.endswith("_test.py") or ".test." in norm:
                test_added += a
                test_deleted += d
            elif norm.endswith(".md") or "/doc/" in norm or "/docs/" in norm or "readme" in norm:
                doc_added += a
                doc_deleted += d
    return SessionLoc(
        added=added,
        deleted=deleted,
        unseen_writes=0,
        rework_deleted=0,
        high_churn_files=sum(1 for c in file_edit_counts.values() if c >= 3),
        test_added=test_added,
        test_deleted=test_deleted,
        doc_added=doc_added,
        doc_deleted=doc_deleted,
        file_edit_counts=file_edit_counts,
    )


def has_loc_signal(path: Path) -> bool:
    """True if the session contains a parseable apply_patch call."""
    for obj in iter_events(path):
        payload = obj.get("payload")
        if obj.get("type") == "response_item" and isinstance(payload, dict):
            if payload.get("type") == "function_call" and payload.get("name") == "apply_patch":
                if _extract_patch(payload.get("arguments")):
                    return True
    return False


def _add_token_usage(u: TokenUsage, usage: dict, model: str) -> None:
    cached = _as_int(usage.get("cached_input_tokens"))
    raw_input = _as_int(usage.get("input_tokens"))
    i = max(0, raw_input - cached)
    cr = cached
    cw = 0
    o = _as_int(usage.get("output_tokens"))
    reasoning = _as_int(usage.get("reasoning_output_tokens"))
    if i + cr + o == 0:
        u.empty_usage_skipped += 1
        return
    u.assistant_msgs += 1
    u.input_tokens += i
    u.cache_read_input_tokens += cr
    u.output_tokens += o
    u.reasoning_output_tokens += reasoning
    key = model or ""
    u.bucket(key).add(i, cw, cr, o)


def _add_rate_limit(u: TokenUsage, rate_limits) -> None:
    if not isinstance(rate_limits, dict):
        return
    u.rate_limit_snapshots += 1
    name = _first_str(rate_limits.get("limit_name"), rate_limits.get("limit_id"))
    if name:
        u.rate_limit_names.add(name)
    if rate_limits.get("rate_limit_reached_type"):
        u.rate_limit_reached_count += 1


def _set_max(u: TokenUsage, attr: str, value) -> None:
    n = _as_int(value)
    if n <= 0:
        return
    current = getattr(u, attr)
    setattr(u, attr, n if current is None else max(current, n))


def _classify_tool(name: str, arguments) -> tuple[str, str]:
    if name == "apply_patch":
        return "Edit", ""
    if name != "exec_command":
        return name, _path_hint(arguments)
    cmd = ""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if isinstance(args, dict):
            cmd = str(args.get("cmd") or "")
    except json.JSONDecodeError:
        pass
    lowered = cmd.strip().lower()
    first = lowered.split(maxsplit=1)[0] if lowered else ""
    if "apply_patch" in lowered or "applypatch" in lowered or "*** begin patch" in lowered:
        return "Edit", ""
    if first in {"rg", "grep", "select-string"}:
        return "Grep", ""
    if first in {"find", "get-childitem", "dir", "ls"} or "rg --files" in lowered:
        return "Glob", ""
    if first in {"cat", "type", "get-content", "sed", "head", "tail"}:
        return "Read", _path_hint(arguments)
    return "Bash", _path_hint(arguments)


def _path_hint(arguments) -> str:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "path", "workdir"):
        val = args.get(key)
        if isinstance(val, str):
            return val
    return ""


def _extract_patch(arguments) -> str:
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments if "*** Begin Patch" in arguments else ""
    else:
        args = arguments
    if isinstance(args, dict):
        for key in ("patch", "input", "cmd"):
            val = args.get(key)
            if isinstance(val, str) and "*** Begin Patch" in val:
                return val
    return ""


def _patch_file_deltas(patch: str) -> list[tuple[str, int, int]]:
    deltas: list[tuple[str, int, int]] = []
    current: str | None = None
    added = deleted = 0
    for line in patch.splitlines():
        if line.startswith("*** Add File: "):
            if current is not None:
                deltas.append((current, added, deleted))
            current = line.removeprefix("*** Add File: ").strip()
            added = deleted = 0
        elif line.startswith("*** Update File: "):
            if current is not None:
                deltas.append((current, added, deleted))
            current = line.removeprefix("*** Update File: ").strip()
            added = deleted = 0
        elif line.startswith("*** Delete File: "):
            if current is not None:
                deltas.append((current, added, deleted))
            current = line.removeprefix("*** Delete File: ").strip()
            added = deleted = 0
        elif current is not None:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1
    if current is not None:
        deltas.append((current, added, deleted))
    return deltas


def _exit_code(output: str) -> int | None:
    m = _EXIT_RE.search(output)
    return int(m.group(1)) if m else None


def _as_int(v) -> int:
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _list_len(v) -> int:
    return len(v) if isinstance(v, list) else 0


def _json_label(v) -> str | None:
    if isinstance(v, str) and v:
        return v
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for key in ("mode", "type", "kind", "name"):
            val = v.get(key)
            if isinstance(val, str) and val:
                return val
        try:
            return json.dumps(v, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(v)
    return str(v)


def _first_str(*values) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _session_id_from_filename(path: Path) -> str | None:
    m = re.search(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl$", path.name)
    return m.group(1) if m else path.stem


def _display_name_for_cwd(cwd: str | None) -> str:
    if not cwd:
        return _NO_CWD_LABEL
    return Path(cwd).name or cwd
