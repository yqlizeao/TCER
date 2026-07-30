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
from tcer.core.models import ProjectRef, SessionMeta, TokenUsage, ToolOp, TurnStat
from tcer.core.parse_util import as_int as _as_int, first_str as _first_str
from tcer.core.paths import codex_dir, codex_sessions_dir, encode_hash
from tcer.core.reader import parse_timestamp_ms, truncate_summary

_NO_CWD_KEY = "__codex_no_cwd__"
_NO_CWD_LABEL = "Codex 无工作目录"
# 两种实测格式：function_call_output 用 "Process exited with code N"，
# custom_tool_call_output 用 "Exit code: N"。
_EXIT_RES = (
    re.compile(r"Process exited with code\s+(-?\d+)"),
    re.compile(r"\bExit code:\s*(-?\d+)"),
)


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


# Process cache for session_index.jsonl, keyed by (path, mtime, size) so a
# new CODEX_HOME (tests) or an index rewritten by Codex invalidates it. Avoids
# re-reading the index once per session during analyze (once per file).
_INDEX_TITLES_CACHE: tuple[str, int, int, dict[str, str]] | None = None


def _index_titles() -> dict[str, str]:
    """Read ``session_index.jsonl`` as session id -> thread title (cached)."""
    global _INDEX_TITLES_CACHE
    p = codex_dir() / "session_index.jsonl"
    pstr = str(p)
    try:
        st = p.stat()
        sig = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        _INDEX_TITLES_CACHE = None
        return {}
    cached = _INDEX_TITLES_CACHE
    if cached is not None and cached[0] == pstr and cached[1] == sig[0] and cached[2] == sig[1]:
        return cached[3]
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
    _INDEX_TITLES_CACHE = (pstr, sig[0], sig[1], titles)
    return titles


def _normalize_cwd(cwd: str | None) -> str | None:
    """Normalize cwd path to avoid duplicates from drive-letter case differences.

    On Windows ``c:\\GitHub`` and ``C:\\GitHub`` resolve to the same path;
    ``Path.resolve()`` normalises the drive letter to uppercase.
    """
    if not cwd:
        return cwd
    try:
        return str(Path(cwd).resolve())
    except (OSError, ValueError):
        return cwd


def _session_head_meta(
    path: Path, max_lines: int = 16,
) -> tuple[str | None, str | None] | None:
    """Read ``(session_id, cwd)`` from the first ``session_meta`` event only.

    Codex always writes ``session_meta`` as a rollout's first line, so grouping
    / dedup only needs line 1 — walking a whole (multi-MB) rollout end-to-end
    just to read it made startup ``O(total rollout bytes)`` for heavy Codex
    users. Returns None when no ``session_meta`` shows up in the first
    ``max_lines`` lines (caller falls back to the full scan). cwd is normalized.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    pl = obj.get("payload")
                    if isinstance(pl, dict):
                        sid = _first_str(pl.get("session_id"), pl.get("id"))
                        cwd = pl.get("cwd") if isinstance(pl.get("cwd"), str) else None
                        return sid, _normalize_cwd(cwd)
    except OSError:
        return None
    return None


def _dedupe_by_session_id(
    paths: list[Path], sid_by_path: dict[Path, str | None],
) -> list[Path]:
    """Collapse Codex rollout files that share one ``session_id`` to the latest.

    Codex ``resume`` writes a *new* rollout file that reuses the same
    ``session_id`` and replays the full conversation history — every later file
    is a superset of the earlier ones. Listing each rollout file as its own
    session therefore shows the same session once per resume (and summing their
    tokens at the aggregate would double-count). We keep only the most recently
    written file per ``session_id``: the authoritative, most-complete snapshot.

    Files without a ``session_id`` are never grouped (each stays distinct).
    """
    best: dict[str, Path] = {}
    best_rank: dict[str, tuple[int, int]] = {}
    for p in paths:
        sid = (sid_by_path.get(p) or f"__noid__{p}")
        try:
            st = p.stat()
            rank = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            rank = (0, 0)
        prev = best_rank.get(sid)
        if prev is None or rank > prev:
            best[sid] = p
            best_rank[sid] = rank
    return list(best.values())


def list_project_refs() -> list[ProjectRef]:
    """Group Codex sessions by cwd for the unified project list.

    Project grouping only needs each file's ``session_id`` + ``cwd`` — both live
    in the ``session_meta`` header (line 1), so we read the head cheaply instead
    of scanning every (potentially multi-MB) rollout. Rollout files sharing a
    ``session_id`` (Codex ``resume``) are collapsed to their latest snapshot
    (see :func:`_dedupe_by_session_id`); survivors are bucketed by cwd so one
    project = one card with one entry per real session.
    """
    paths = discover_sessions()
    sid_by_path: dict[Path, str | None] = {}
    cwd_by_path: dict[Path, str | None] = {}
    for p in paths:
        head = _session_head_meta(p)
        if head is None:  # rare: no session_meta in head → full scan
            meta = read_session_meta(p)
            sid_by_path[p] = meta.session_id
            cwd_by_path[p] = _normalize_cwd(meta.cwd)
        else:
            sid_by_path[p], cwd_by_path[p] = head

    deduped = _dedupe_by_session_id(paths, sid_by_path)

    groups: dict[str, list[Path]] = {}
    cwd_by_key: dict[str, str | None] = {}
    for p in deduped:
        cwd = cwd_by_path.get(p)
        key = encode_hash(cwd) if cwd else _NO_CWD_KEY
        groups.setdefault(key, []).append(p)
        cwd_by_key.setdefault(key, cwd)

    refs: list[ProjectRef] = []
    for key, group_paths in groups.items():
        cwd = cwd_by_key.get(key)
        refs.append(ProjectRef(
            source="codex",
            key=key,
            display_name=_display_name_for_cwd(cwd),
            cwd=cwd,
            path=Path(cwd) if cwd else None,
            session_paths=tuple(sorted(group_paths)),
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
    got_meta = got_turn_ctx = got_title = False

    # Codex 把所有会话级元数据写在前几行（session_meta 在第 1 行、turn_context ~5、
    # 首条 user_message ~7）。取齐即停——否则每次重新分析都要为读前 7 行而扫遍
    # 整个多 MB 的 rollout（实测 3383 行 / 8 MB 文件，约 480× 过度读取）。上限兜底
    # 从未发出其中某类事件的会话（如用户输入前就中断）。
    for n, obj in enumerate(iter_events(path), 1):
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
            got_meta = True
        elif typ == "turn_context" and isinstance(payload, dict):
            approval_policy = _json_label(payload.get("approval_policy")) or approval_policy
            sandbox_policy = _json_label(payload.get("sandbox_policy")) or sandbox_policy
            permission_profile = _json_label(payload.get("permission_profile")) or permission_profile
            collaboration_mode = _json_label(payload.get("collaboration_mode")) or collaboration_mode
            reasoning_effort = _json_label(payload.get("effort")) or reasoning_effort
            got_turn_ctx = True
        elif typ == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "user_message" and fallback_title is None:
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    fallback_title = truncate_summary(msg.strip(), 80)
                    got_title = True
        if (got_meta and got_turn_ctx and got_title) or n >= 512:
            break

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
    prev_total: dict[str, int] = {}  # running total_token_usage baseline for deltas
    # 权威回合分组：task_started = 一个 agent 回合开始。出现过 task_started 时，
    # turn_idx 由回合边界驱动（该回合内的工具调用与 token 差分步共享同一回合号）；
    # 旧格式（无 task_started）回退为每个 token 差分步 +1 的人造计数。
    saw_task_started = False

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
                # Codex turn lifecycle (agent turn began) — NOT Claude's Task
                # subagent tool. Track via task_count only; never invent a
                # tool_calls["Task"] entry (that inflated tool totals and
                # zero-token sessions looked like they had tools).
                u.task_count += 1
                if saw_task_started:
                    turn_idx += 1
                saw_task_started = True
                _set_max(u, "model_context_window", payload.get("model_context_window"))
                started = parse_timestamp_ms(payload.get("started_at"))
                if started is not None:
                    u.started_at = started if u.started_at is None else min(u.started_at, started)
            elif ptype == "task_complete":
                u.completed_task_count += 1
                completed = parse_timestamp_ms(payload.get("completed_at"))
                if completed is not None:
                    u.ended_at = completed if u.ended_at is None else max(u.ended_at, completed)
                dur = _as_int(payload.get("duration_ms"))
                active_duration_ms += dur
                # 回合权威耗时回填到该 task 的最后一个 token 步。
                if dur and u.turn_stats and u.turn_stats[-1].duration_ms is None:
                    u.turn_stats[-1].duration_ms = dur
                ttft = _as_int(payload.get("time_to_first_token_ms"))
                if ttft > 0:
                    u.time_to_first_token_ms = ttft if u.time_to_first_token_ms is None else min(u.time_to_first_token_ms, ttft)
                    u.ttft_ms_samples.append(ttft)  # 全样本保留（p95 用）
            elif ptype == "turn_aborted":
                u.aborted_task_count += 1
                active_duration_ms += _as_int(payload.get("duration_ms"))
                reason = _first_str(payload.get("reason")) or "unknown"
                u.abort_reasons[reason] = u.abort_reasons.get(reason, 0) + 1
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
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                # Prefer the delta of the authoritative, monotonic
                # ``total_token_usage``: Codex re-emits the same
                # ``last_token_usage`` on some token_count events (observed
                # 1.5–2.5% overcount on real sessions), so summing ``last``
                # double-counts. A negative delta (total reset) falls back to
                # ``last`` and re-bases.
                if isinstance(total, dict):
                    delta = {k: _as_int(total.get(k)) - prev_total.get(k, 0)
                             for k in _TOKEN_FIELDS}
                    if all(v >= 0 for v in delta.values()):
                        prev_total = {k: _as_int(total.get(k)) for k in _TOKEN_FIELDS}
                        if any(delta.values()):
                            _add_token_usage_with_stat(u, delta, current_model,
                                                       turn_idx, ts)
                            if not saw_task_started:
                                turn_idx += 1
                        continue
                    prev_total = {k: _as_int(total.get(k)) for k in _TOKEN_FIELDS}
                if isinstance(usage, dict):
                    _add_token_usage_with_stat(u, usage, current_model,
                                               turn_idx, ts)
                    if not saw_task_started:
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
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            out_s = output if isinstance(output, str) else ""
            code = _exit_code(out_s)
            # exit code 权威；无码时按显式失败前缀兜底（见 _output_is_error）。
            is_err = (code != 0) if code is not None else _output_is_error(out_s)
            if is_err:
                u.tool_errors += 1
                cid = payload.get("call_id") or payload.get("id")
                tname = call_id_to_name.get(cid) if isinstance(cid, str) else None
                if tname:
                    u.tool_errors_by_tool[tname] = u.tool_errors_by_tool.get(tname, 0) + 1
        elif ptype == "reasoning":
            u.thinking_count += 1
        elif ptype == "web_search_call":
            u.web_search_count += 1
        elif ptype == "custom_tool_call":
            # Same naming map as function_call (apply_patch → Edit, etc.).
            name = payload.get("name")
            if isinstance(name, str) and name:
                tool_name, path_hint = _classify_tool(name, payload.get("input") or payload.get("arguments"))
            else:
                tool_name, path_hint = "CustomTool", ""
            u.tool_calls[tool_name] = u.tool_calls.get(tool_name, 0) + 1
            cid = payload.get("call_id") or payload.get("id")
            if isinstance(cid, str):
                call_id_to_name[cid] = tool_name
            u.tool_ops.append(ToolOp(turn_idx, tool_name, path_hint))

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


def read_conversation(path: Path) -> list[dict]:
    """Extract the full ordered conversation from a Codex session JSONL.

    Mirrors :func:`tcer.core.reader.read_conversation`'s output shape so the web
    session view can render every source uniformly. Codex stores the turn stream
    as ``{"type","payload"}`` events rather than Claude's ``message`` objects, so
    the mapping is:

      * ``response_item.message`` (role=user)      -> user text
      * ``response_item.message`` (role=assistant) -> assistant text
      * ``response_item.reasoning``                -> thinking (only when a plain
        ``summary``/``content`` text exists; encrypted-only blocks are skipped)
      * ``response_item.function_call`` /
        ``custom_tool_call``                       -> tool_use (name + input)
      * ``response_item.function_call_output`` /
        ``custom_tool_call_output``                -> tool_result

    The ``developer`` role (system/permission preamble) is skipped. Assistant
    text is taken from ``response_item.message`` — NOT ``event_msg.agent_message``
    — because the two duplicate each other and only ``response_item`` carries the
    canonical, non-truncated content.
    """
    convo: list[dict] = []
    for obj in iter_events(path):
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue  # skip developer/system preamble
            text = _message_text(payload.get("content")).strip()
            if text:
                convo.append({"role": role, "type": "text", "text": text, "ts": ts})
        elif ptype == "reasoning":
            text = _reasoning_text(payload).strip()
            if text:
                convo.append({"role": "assistant", "type": "thinking",
                              "text": text, "ts": ts})
        elif ptype in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            tool_name = (_classify_tool(name, payload.get("arguments"))[0]
                         if ptype == "function_call" and isinstance(name, str)
                         else (str(name) if isinstance(name, str) and name else "CustomTool"))
            raw_args = payload.get("arguments") if ptype == "function_call" else payload.get("input")
            convo.append({
                "role": "assistant", "type": "tool_use",
                "name": tool_name,
                "id": payload.get("call_id"),
                "input": _tool_input(raw_args),
                "ts": ts,
            })
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            code = _exit_code(text) if isinstance(text, str) else None
            convo.append({
                "role": "tool", "type": "tool_result",
                "tool_use_id": payload.get("call_id"),
                "is_error": bool(code is not None and code != 0),
                "text": text,
                "ts": ts,
            })
    return convo


def _message_text(content) -> str:
    """Flatten a Codex ``message.content`` array (``input_text``/``output_text``)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for it in content:
        if not isinstance(it, dict):
            continue
        for key in ("text", "input_text", "output_text"):
            v = it.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
                break
    return "\n".join(parts)


def _reasoning_text(payload: dict) -> str:
    """Plain-text reasoning from a Codex ``reasoning`` item, if any.

    Codex usually ships reasoning as ``encrypted_content`` (opaque); only the
    optional ``summary`` list / ``content`` field carry human-readable text.
    """
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts = [s.get("text", "") if isinstance(s, dict) else str(s)
                 for s in summary]
        joined = "\n".join(p for p in parts if p)
        if joined.strip():
            return joined
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _message_text(content)
    return ""


def _tool_input(arguments):
    """Parse a Codex tool ``arguments``/``input`` into a dict when it's JSON."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return {"raw": arguments}
    return {}


def _loc_scan(path: Path):
    """Single pass over events returning ``(SessionLoc, has_signal)``.

    ``has_signal`` is True if any parseable apply_patch exists (independent of
    code-file filtering, matching ``has_loc_signal``). Combining the LOC tally
    and the signal check into one scan avoids walking the file twice — the
    analyze loop needs both for every session.

    Self-rework mirrors Claude ``_LocAccumulator``: lines this session added
    to a file, later removed by a subsequent patch on the same path, count as
    ``rework_deleted`` (not deletions of pre-session code).
    """
    from tcer.core.loc import SessionLoc, _is_code, _is_test_file, _is_doc_file

    added = deleted = rework = 0
    has_signal = False
    file_edit_counts: dict[str, int] = {}
    # Lines this session has authored per path (never includes pre-session code).
    session_authored: dict[str, int] = {}
    test_added = test_deleted = doc_added = doc_deleted = 0
    for obj in iter_events(path):
        payload = obj.get("payload")
        if obj.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        name = payload.get("name")
        # Live Codex often records apply_patch as custom_tool_call with the
        # patch text in ``input`` (not function_call / arguments).
        if name != "apply_patch":
            continue
        if ptype == "function_call":
            patch = _extract_patch(payload.get("arguments"))
        elif ptype == "custom_tool_call":
            patch = _extract_patch(payload.get("input") or payload.get("arguments"))
        else:
            continue
        if not patch:
            continue
        has_signal = True
        for fp, a, d in _patch_file_deltas(patch):
            if not _is_code(fp):
                continue
            added += a
            deleted += d
            auth_before = session_authored.get(fp, 0)
            rework_part = min(d, auth_before)
            rework += rework_part
            session_authored[fp] = max(0, auth_before - rework_part + a)
            file_edit_counts[fp] = file_edit_counts.get(fp, 0) + 1
            if _is_test_file(fp):
                test_added += a
                test_deleted += d
            elif _is_doc_file(fp):
                doc_added += a
                doc_deleted += d
    sloc = SessionLoc(
        added=added,
        deleted=deleted,
        unseen_writes=0,
        rework_deleted=rework,
        high_churn_files=sum(1 for c in file_edit_counts.values() if c >= 3),
        test_added=test_added,
        test_deleted=test_deleted,
        doc_added=doc_added,
        doc_deleted=doc_deleted,
        file_edit_counts=file_edit_counts,
    )
    return sloc, has_signal


def session_loc_full(path: Path):
    """Return LOC from parseable Codex apply_patch calls only."""
    return _loc_scan(path)[0]


def has_loc_signal(path: Path) -> bool:
    """True if the session contains a parseable apply_patch call."""
    return _loc_scan(path)[1]


# Billing fields shared by ``last_token_usage`` / ``total_token_usage``.
_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
                 "reasoning_output_tokens")


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
    # Codex input_tokens already includes cache; i+cr restores full turn input.
    u.peak_input_tokens = max(u.peak_input_tokens, i + cr + cw)
    key = model or ""
    u.bucket(key).add(i, cw, cr, o)


def _add_token_usage_with_stat(
    u: TokenUsage, usage: dict, model: str, turn: int, ts: int | None,
) -> None:
    """``_add_token_usage`` + 逐步 TurnStat（时间线用；耗时由 task_complete 回填）。"""
    before = (u.input_tokens, u.cache_read_input_tokens, u.output_tokens)
    _add_token_usage(u, usage, model)
    after = (u.input_tokens, u.cache_read_input_tokens, u.output_tokens)
    if after != before:
        u.turn_stats.append(TurnStat(
            turn=turn, ts=ts,
            input_tokens=after[0] - before[0],
            cache_read=after[1] - before[1],
            output_tokens=after[2] - before[2],
            model=model or "",
        ))


def _add_rate_limit(u: TokenUsage, rate_limits) -> None:
    if not isinstance(rate_limits, dict):
        return
    u.rate_limit_snapshots += 1
    name = _first_str(rate_limits.get("limit_name"), rate_limits.get("limit_id"))
    if name:
        u.rate_limit_names.add(name)
    if rate_limits.get("rate_limit_reached_type"):
        u.rate_limit_reached_count += 1
    # 配额水位：primary/secondary.used_percent（0–100）→ 峰值占用 0..1。
    for key in ("primary", "secondary"):
        win = rate_limits.get(key)
        if isinstance(win, dict):
            pct = win.get("used_percent")
            if isinstance(pct, (int, float)) and pct > 0:
                frac = float(pct) / 100.0
                if u.rate_limit_peak_used is None or frac > u.rate_limit_peak_used:
                    u.rate_limit_peak_used = frac


def _set_max(u: TokenUsage, attr: str, value) -> None:
    n = _as_int(value)
    if n <= 0:
        return
    current = getattr(u, attr)
    setattr(u, attr, n if current is None else max(current, n))


# Codex function names that should appear under TCER-canonical tool keys so
# exploration / edit / read-write ratios stay comparable across sources.
_CODEX_NAME_MAP = {
    "apply_patch": "Edit",
    "shell_command": "Bash",
    "write_stdin": "Bash",
    "update_plan": "TodoWrite",
    "request_user_input": "AskUserQuestion",
    "view_image": "Read",
}


def _classify_tool(name: str, arguments) -> tuple[str, str]:
    mapped = _CODEX_NAME_MAP.get(name)
    if mapped is not None:
        if mapped == "Edit":
            # apply_patch has no file_path arg — path lives inside the patch body.
            return mapped, _path_from_apply_patch(arguments)
        return mapped, _path_hint(arguments)
    if name not in ("exec_command", "shell_command"):
        return name, _path_hint(arguments)
    cmd = ""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if isinstance(args, dict):
            cmd = str(args.get("cmd") or args.get("command") or "")
    except json.JSONDecodeError:
        pass
    lowered = cmd.strip().lower()
    first = lowered.split(maxsplit=1)[0] if lowered else ""
    # Only a *real* apply_patch invocation classifies as Edit — the command
    # must start with apply_patch or carry the ``*** Begin Patch`` marker.
    # A bare ``"apply_patch" in cmd`` substring misclassified shell commands
    # that merely *mention* apply_patch (e.g. ``Select-String -Pattern
    # 'apply_patch|...'`` grepping session logs) as edits.
    if lowered.startswith("apply_patch") or lowered.startswith("applypatch") or "*** begin patch" in lowered:
        return "Edit", _path_from_apply_patch(arguments)
    if first in {"rg", "grep", "select-string"}:
        return "Grep", ""
    if first in {"find", "get-childitem", "dir", "ls"} or "rg --files" in lowered:
        return "Glob", ""
    if first in {"cat", "type", "get-content", "sed", "head", "tail"}:
        return "Read", _path_hint(arguments)
    return "Bash", _path_hint(arguments)


def _path_hint(arguments) -> str:
    """First file-path argument (``file_path`` / ``path``).

    ``workdir`` is deliberately excluded: it is the command's working
    *directory*, never a touched file. Recording it as ``ToolOp.path``
    polluted the 涉及文件 popup with the project root (no extension) once
    per shell call — e.g. a TCER session showed ``C:\\GitHub\\TCER`` with
    105 ops dominating the real files.
    """
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "path"):
        val = args.get(key)
        if isinstance(val, str):
            return val
    return ""


def _path_from_apply_patch(arguments) -> str:
    """First file path mentioned in an apply_patch body (for ToolOp hints).

    Multi-file patches only expose the first path on ``ToolOp.path``; full
    per-file tallies still come from ``session_loc_full`` / ``_patch_file_deltas``.
    """
    patch = _extract_patch(arguments)
    if not patch:
        return _path_hint(arguments)
    for line in patch.splitlines():
        for prefix in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
            if line.startswith(prefix):
                fp = line.removeprefix(prefix).strip()
                if fp:
                    return fp
    return ""


def _extract_patch(arguments) -> str:
    """Pull unified-diff patch text from function_call or custom_tool_call payloads.

    - ``function_call.arguments``: JSON string/object with a ``patch`` field
    - ``custom_tool_call.input``: often the raw ``*** Begin Patch`` text itself
    """
    if isinstance(arguments, str):
        # Raw patch body (custom_tool_call.input) — prefer before JSON parse
        # so a non-JSON patch is not discarded.
        if "*** Begin Patch" in arguments:
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                return arguments
            # JSON string that embeds Begin Patch only inside a field — fall through
            if not isinstance(args, dict):
                return arguments
        else:
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                return ""
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
    for rx in _EXIT_RES:
        m = rx.search(output)
        if m:
            return int(m.group(1))
    return None


def _output_is_error(output: str) -> bool:
    """无 exit code 时的兜底：只认显式失败前缀。

    旧逻辑对 output 全文做 "error"/"failed" 子串匹配，正常输出提到这两个词
    也会被记为工具错误（误报），而 "execution error: Io(...)" 这类无退出码的
    真失败又因发生在 function_call_output 分支而漏报。前缀匹配两头都修。
    """
    head = output.lstrip().lower()
    return head.startswith((
        "execution error", "error:", "failed:",
        "traceback (most recent call last)",
    ))



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



def _session_id_from_filename(path: Path) -> str | None:
    m = re.search(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl$", path.name)
    return m.group(1) if m else path.stem


def _display_name_for_cwd(cwd: str | None) -> str:
    if not cwd:
        return _NO_CWD_LABEL
    return Path(cwd).name or cwd
