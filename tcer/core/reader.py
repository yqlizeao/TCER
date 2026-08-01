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
from tcer.core.parse_util import (
    as_int as _as_int,
    is_correction as _is_correction,
    is_slash_command as _is_slash_command,
)
from tcer.core.paths import claude_config_dirs
from tcer.core import pricing

# User-role texts that Claude Code injects (not real human input). These
# ``<…>`` tags / markers are matched on the RAW text BEFORE :func:`_strip_tags`
# removes the tags — matching after stripping erases the tags and lets them
# through (observed ~17% of popup rows were injections before this fix).
_TITLE_NOISE_PREFIXES = (
    "<local-command-caveat", "<local-command-stdout", "<local-command-stderr",
    "<command-name>", "<command-message>",
    "<ide_opened_file>", "<ide_selection>",
    "<task-notification>", "<system-reminder>",
    "/clear",
    "[Request interrupted",
)
# After tag removal, skip these system-generated phrases (covers bodies whose
# opening tag was consumed elsewhere, plus plain-text injections such as the
# compact-continuation preamble).
_TITLE_NOISE_AFTER_CLEAN = (
    "The user opened the file", "The user selected the lines",
    "This session is being continued from a previous conversation",
    "You are an expert",
)
TITLE_MAX_CHARS = 80

_TAG_RE = re.compile(r'<[^>]+>')

# 纠正措辞正则与 slash/纠正判定移至 parse_util（跨源 SSOT，Claude/omp/pi 共用）。


def _strip_tags(txt: str) -> str:
    """Remove XML/HTML-like tags (e.g. ``<ide_opened_file>…</ide_opened_file>``)."""
    return _TAG_RE.sub('', txt).strip()


def _is_user_noise(txt: str) -> bool:
    """Whether a user-role text is Claude-Code-injected, not real human input.

    Two-stage check mirroring title extraction: ``<…>`` noise prefixes are
    matched on the RAW text *before* tag stripping — the prefixes ARE tags, so
    stripping first would delete them and always miss (the pre-fix bug).
    Then the cleaned text is checked against post-strip phrases.
    """
    if txt.startswith(_TITLE_NOISE_PREFIXES):
        return True
    cleaned = _strip_tags(txt)
    return cleaned.startswith(_TITLE_NOISE_AFTER_CLEAN)


def discover_jsonl(project_hash: str | None = None, *,
                   roots: list[Path] | None = None) -> list[Path]:
    """Recursively collect every ``*.jsonl`` under a project (or all projects).

    By default searches every Claude config root (see
    :func:`paths.claude_config_dirs`) so a project hash present in multiple custom
    profiles (e.g. ``.claude`` and ``.zclaude``) yields the union of its session
    files across roots. Pass *roots* to restrict the search to specific config
    roots only — used by per-root analysis now that cross-root dedup is dropped
    (a project that lives under both ``.claude`` and ``.claude-proxy`` is listed
    once per root, each scoped to its own sessions).

    On Windows, also unions folders whose names match *project_hash* case-
    insensitively (``C--GitHub-X`` vs ``c--GitHub-X`` drive-letter variants).
    """
    import sys

    from tcer.core.paths import project_hash_key

    scan_roots = roots if roots is not None else claude_config_dirs()
    files: list[Path] = []
    for root in scan_roots:
        projs = root / "projects"
        if not projs.is_dir():
            continue
        if project_hash:
            targets: list[Path] = []
            exact = projs / project_hash
            if exact.is_dir():
                targets.append(exact)
            if sys.platform == "win32":
                want = project_hash_key(project_hash)
                try:
                    for d in projs.iterdir():
                        if (
                            d.is_dir()
                            and project_hash_key(d.name) == want
                            and d not in targets
                        ):
                            targets.append(d)
                except OSError:
                    pass
            for base in targets:
                try:
                    files.extend(base.rglob("*.jsonl"))
                except OSError:
                    continue
        else:
            try:
                files.extend(projs.rglob("*.jsonl"))
            except OSError:
                continue
    # Dedupe by resolved path (same file via two casings is rare but possible).
    out: list[Path] = []
    seen: set[Path] = set()
    for f in sorted(files):
        try:
            rp = f.resolve()
        except OSError:
            rp = f
        if rp in seen:
            continue
        seen.add(rp)
        out.append(f)
    return out


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


def session_artifacts(any_path: Path) -> tuple[str, Path, Path]:
    """Resolve a session's on-disk identity from *any* of its JSONL paths.

    A session lives on disk as two artifacts under the project hash directory:
      * ``<project>/<sid>.jsonl``  — the main conversation file
      * ``<project>/<sid>/``       — a directory holding ``subagents/`` (each
        ``agent-*.jsonl`` plus its ``.meta.json``) and ``tool-results/``

    Given the main file *or* one of the subagent files, return
    ``(session_id, main_jsonl, session_dir)``. The identity is taken from the
    **path** (folder/stem), not the JSONL ``sessionId`` field — the two can
    differ (e.g. resumed sessions), and only the path reflects what is actually
    on disk, so deletion must key off it. Neither returned path is guaranteed to
    exist (an orphan-subagent session has no main file).
    """
    if is_subagent(any_path):
        session_dir = any_path.parents[1]      # <project>/<sid>/subagents/x.jsonl → <project>/<sid>
        proj_dir = session_dir.parent          # <project>
        sid = session_dir.name
        main = proj_dir / f"{sid}.jsonl"
    else:
        proj_dir = any_path.parent             # <project>
        sid = any_path.stem
        main = any_path
        session_dir = proj_dir / sid
    return sid, main, session_dir


def delete_session(any_path: Path) -> list[Path]:
    """Permanently delete a session and all its residue from disk.

    Removes the main ``<sid>.jsonl`` and the entire ``<sid>/`` directory tree
    (subagents + their ``.meta.json`` sidecars + tool-results), so no subagent
    data is left orphaned. Accepts any of the session's file paths (see
    :func:`session_artifacts`). Returns the list of paths actually removed
    (missing artifacts are silently skipped). Raises ``OSError`` on failure.
    """
    import shutil

    _, main, session_dir = session_artifacts(any_path)
    removed: list[Path] = []
    if main.is_file():
        main.unlink()
        removed.append(main)
    if session_dir.is_dir():
        shutil.rmtree(session_dir)
        removed.append(session_dir)
    return removed


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


# Tools whose bare name hides what was actually run; the interesting identity
# lives in one of these input keys. Order matters: first key present wins.
_VARIANT_KEYS: dict[str, tuple[str, ...]] = {
    "Skill": ("skill", "name", "command"),
    "Task": ("subagent_type", "agent_type"),
    "Agent": ("subagent_type", "agent_type"),
}


def record_tool_variant(u: TokenUsage, tool_name: str, inp) -> None:
    """Record ``"<Tool>:<variant>"`` for tools whose name alone isn't identifying.

    ``Skill``/``Task``/``Agent`` all appear in ``tool_calls`` under one key no
    matter which skill or subagent ran, so the Skill / subagent dimensions have
    to come from the call input. Anything unparseable is silently skipped —
    this is an enrichment, never a reason to fail a scan.
    """
    keys = _VARIANT_KEYS.get(tool_name)
    if not keys or not isinstance(inp, dict):
        return
    for key in keys:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            label = f"{tool_name}:{val.strip()}"
            u.tool_variants[label] = u.tool_variants.get(label, 0) + 1
            return


def aggregate_usage(
    path: Path,
    *,
    include_user_texts: bool = True,
) -> TokenUsage:
    """Sum token usage across all assistant turns in one session file.

    See :func:`scan_session` for dedup / empty-usage / tool-call semantics.
    ``include_user_texts`` defaults True for backward-compatible callers; the
    analyze path uses False and loads bodies via :func:`read_user_messages`.
    """
    u, _ = scan_session(
        path, with_loc=False, include_user_texts=include_user_texts,
    )
    return u


def read_user_messages(path: Path) -> list[str]:
    """Extract Claude user-message text on demand (popup / upload).

    Mirrors Codex privacy boundary: analysis keeps only ``user_msgs`` counts;
    full bodies are read when the user opens the popup.
    """
    messages: list[str] = []
    for obj in iter_messages(path):
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        is_real_user = False
        if isinstance(content, str) and content.strip():
            is_real_user = True
        elif isinstance(content, list):
            is_real_user = any(
                isinstance(it, dict) and it.get("type") == "text" for it in content
            )
        if not is_real_user:
            continue
        txt = extract_text(content).strip()
        if not txt or _is_user_noise(txt):
            continue
        txt = _strip_tags(txt)
        if txt:
            messages.append(txt[:500])
    return messages


def scan_session(
    path: Path,
    *,
    with_loc: bool = True,
    include_user_texts: bool = True,
    cancel_check=None,
    use_cache: bool = True,
) -> tuple[TokenUsage, "SessionLoc | None"]:
    """Single-pass scan: token usage and (optionally) git-free LOC.

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
    timestamps (needed for GUI time sorting).

    **Tool calls**: counts each tool_use block by name (NOT deduped by message.id,
    since multiple tool_use blocks in one response are genuine separate calls).

    When ``with_loc=True``, Write/Edit/MultiEdit/NotebookEdit are also replayed
    into a :class:`~tcer.core.loc.SessionLoc` in the same pass (avoids a second
    full JSONL walk). ``cancel_check`` if set is a zero-arg callable that raises
    when the caller wants to abort mid-file (e.g. GUI generation cancel).

    ``include_user_texts``: when False, only ``user_msgs`` is counted (cheaper /
    smaller reports); use :func:`read_user_messages` for bodies.

    ``use_cache``: process-level mtime/size cache (see ``file_cache``). Safe with
    ``cancel_check``: a cancel raises inside the factory, so a partial scan never
    reaches the cache — only completed scans are stored.
    """
    from tcer.core import file_cache

    can_cache = use_cache
    extra = ("scan_session", bool(with_loc), bool(include_user_texts))

    def _compute():
        return _scan_session_uncached(
            path,
            with_loc=with_loc,
            include_user_texts=include_user_texts,
            cancel_check=cancel_check,
        )

    if can_cache:
        return file_cache.get_or_compute(path, extra, _compute)
    return _compute()


def _scan_session_uncached(
    path: Path,
    *,
    with_loc: bool,
    include_user_texts: bool,
    cancel_check,
) -> tuple[TokenUsage, "SessionLoc | None"]:
    # Lazy import: loc imports reader at module level.
    from tcer.core.loc import SessionLoc, _is_code, _LocAccumulator
    from tcer.core.models import TurnStat

    u = TokenUsage()
    loc_acc = _LocAccumulator() if with_loc else None
    seen: set[str] = set()
    call_id_to_name: dict[str, str] = {}  # tool_use_id → tool_name for error attribution
    turn_idx = 0  # next turn number to assign to a new response
    # 子代理文件（subagents/*.jsonl）的 user 消息是 Task 工具派发的 prompt，非真人
    # 输入 → 不计入 user_msgs / slash / correction / first_prompt / user_texts。
    # token / LOC / tool_errors 等真实成本照常累计（子代理成本并入父会话）。
    is_sub = is_subagent(path)
    current_turn = 0  # turn number of the response whose lines we are currently reading
    for obj in iter_messages(path):
        if cancel_check is not None:
            cancel_check()

        # 用户在 AI 运行时排队输入（打断/并行输入信号）。
        if obj.get("type") == "queue-operation":
            u.queued_input_count += 1
            continue

        # attachment 子类型：计划模式使用、读取截断（上下文浪费）。
        if obj.get("type") == "attachment":
            att = obj.get("attachment")
            sub = att.get("type") if isinstance(att, dict) else None
            if sub == "plan_mode":
                u.plan_mode_count += 1
            elif sub == "read_truncation_notice":
                u.read_truncation_count += 1
            continue

        # 非 message 行：system 子类型携带真实回合耗时 / 限流 / 压缩信号。
        if obj.get("type") == "system":
            sub = obj.get("subtype")
            if sub == "stop_hook_summary":
                u.hook_run_count += _as_int(obj.get("hookCount"))
                errs = obj.get("hookErrors")
                u.hook_error_count += (len(errs) if isinstance(errs, list)
                                       else _as_int(errs))
                infos = obj.get("hookInfos")
                if isinstance(infos, list):
                    for info in infos:
                        if isinstance(info, dict):
                            u.hook_duration_ms_total += _as_int(info.get("durationMs"))
            elif sub == "turn_duration":
                # 权威的每回合耗时（不含用户暂停），回填到最近一个回合。
                dur = _as_int(obj.get("durationMs"))
                if dur and u.turn_stats and u.turn_stats[-1].duration_ms is None:
                    u.turn_stats[-1].duration_ms = dur
            elif sub == "api_error":
                err = obj.get("error")
                status = _as_int(err.get("status")) if isinstance(err, dict) else 0
                if status == 429:
                    u.rate_limit_reached_count += 1
                    u.rate_limit_names.add("api-429")
            elif sub == "compact_boundary":
                u.compaction_count += 1
            continue

        # 工具结果行的 toolUseResult：originalFile → LOC F1 修正；
        # userModified → 「AI 写完后被人改过」采纳信号。
        tur = obj.get("toolUseResult")
        if isinstance(tur, dict):
            # MCP 精确归因：结果行携带 attributionMcpServer/Tool（每次调用一行）。
            srv = obj.get("attributionMcpServer")
            if isinstance(srv, str) and srv:
                tool_attr = obj.get("attributionMcpTool")
                key_attr = f"{srv}/{tool_attr}" if isinstance(tool_attr, str) and tool_attr else srv
                u.mcp_calls_by_attr[key_attr] = u.mcp_calls_by_attr.get(key_attr, 0) + 1
            if loc_acc is not None and "originalFile" in tur:
                loc_acc.note_write_original(tur.get("filePath"),
                                            tur.get("originalFile"))
            if tur.get("userModified") is True:
                u.user_modified_count += 1
            # structuredPatch：Claude 自算 diff 的 +/- 行数，作为回放 LOC 的
            # 独立交叉校验（同样只计代码后缀文件）。
            sp = tur.get("structuredPatch")
            fp_sp = tur.get("filePath")
            if (isinstance(sp, list) and isinstance(fp_sp, str)
                    and _is_code(fp_sp)):
                for hunk in sp:
                    lines = hunk.get("lines") if isinstance(hunk, dict) else None
                    if not isinstance(lines, list):
                        continue
                    for ln in lines:
                        if isinstance(ln, str) and ln:
                            if ln[0] == "+":
                                u.patch_diff_added += 1
                            elif ln[0] == "-":
                                u.patch_diff_deleted += 1

        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue

        role = msg.get("role")

        # Count user messages and (optionally) extract text
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
            if is_real_user and not is_sub:
                # prompt 行为信号：只计数，不存正文（隐私边界与懒加载一致）。
                txt = extract_text(content).strip()
                if _is_slash_command(txt):
                    u.slash_command_count += 1
                elif _is_correction(txt):
                    u.correction_msg_count += 1
                # user_msgs 只计真实用户消息：排除 Claude Code 注入（task-
                # notification / 命令输出 / IDE 选区等有 text 块但非真人输入）。
                if txt and not _is_user_noise(txt):
                    u.user_msgs += 1
                    if u.first_prompt_chars == 0:
                        u.first_prompt_chars = len(txt)
                    if include_user_texts:
                        cleaned = _strip_tags(txt)
                        if cleaned:
                            u.user_message_texts.append(cleaned[:500])
            # Count tool_result errors (from ALL user-role messages)
            if isinstance(content, list):
                for item in content:
                    if (isinstance(item, dict)
                            and item.get("type") == "tool_result"
                            and item.get("is_error")):
                        u.tool_errors += 1
                        if u.turn_stats:
                            u.turn_stats[-1].errors += 1
                        # Attribute error to specific tool via call_id mapping
                        tid = item.get("tool_use_id")
                        if isinstance(tid, str):
                            tname = call_id_to_name.get(tid)
                            if tname:
                                u.tool_errors_by_tool[tname] = u.tool_errors_by_tool.get(tname, 0) + 1

        # Process assistant messages: dedup by message.id for token counting.
        # A single assistant response is split across multiple JSONL lines — one
        # per content block (thinking / text / each tool_use) — all sharing the
        # same message.id. We dedup TOKENS by id (count each response once), but
        # the tool_use blocks live on those continuation lines, so content must
        # be extracted from EVERY line, not just the first. The turn number is
        # frozen when a response starts so all of its blocks share it (temporal
        # analysis compares turns across responses, not within one).
        if role == "assistant":
            mid = msg.get("id")
            is_dup = isinstance(mid, str) and mid and mid in seen
            if not is_dup:
                current_turn = turn_idx  # freeze turn for this whole response
                turn_idx += 1
                if isinstance(mid, str) and mid:  # skip empty string → treat as no id
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
                # 缓存写 TTL 分档：1h 写单价是 5m 的 1.6 倍，计价时加溢价。
                cc = usage.get("cache_creation")
                cw1h = (min(_as_int(cc.get("ephemeral_1h_input_tokens")), cw)
                        if isinstance(cc, dict) else 0)
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
                    u.cache_write_1h_tokens += cw1h
                    u.peak_input_tokens = max(u.peak_input_tokens, i + cw + cr)
                    model_raw = msg.get("model")
                    u.turn_stats.append(TurnStat(
                        turn=current_turn, ts=ts,
                        input_tokens=i, cache_write=cw, cache_read=cr,
                        output_tokens=o,
                        model=(pricing.normalize(model_raw)
                               if isinstance(model_raw, str) and model_raw
                               and model_raw != "<synthetic>" else ""),
                    ))
                # Claude 的网页搜索/抓取计数在 usage.server_tool_use 里（每响应一次）。
                stu = usage.get("server_tool_use")
                if isinstance(stu, dict):
                    u.web_search_count += (_as_int(stu.get("web_search_requests"))
                                           + _as_int(stu.get("web_fetch_requests")))
                model = msg.get("model")
                # Skip synthetic stubs (ccswitch 429 errors, "No response requested")
                # — they use the same message.model field but are not real model turns.
                if isinstance(model, str) and model and model != "<synthetic>":
                    u.models.add(model)
                    bucket_key = pricing.normalize(model)
                else:
                    bucket_key = ""
                u.bucket(bucket_key).add(i, cw, cr, o, cw1h)

            # Extract tool_use / thinking from content on EVERY line of the
            # response — continuation lines (dedup duplicates) carry the tool_use
            # blocks, so extracting only on the first line loses most tool calls.
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
                                inp = {}
                            u.tool_ops.append(ToolOp(
                                turn=current_turn,
                                tool=tool_name,
                                path=fp if isinstance(fp, str) else "",
                            ))
                            record_tool_variant(u, tool_name, inp)
                            if u.turn_stats and u.turn_stats[-1].turn == current_turn:
                                u.turn_stats[-1].tool_calls += 1
                            if loc_acc is not None and isinstance(inp, dict):
                                loc_acc.on_tool_use(tool_name, inp)
                    elif item_type == "thinking":
                        u.thinking_count += 1

    # Compute session_duration_ms from the time window
    if u.started_at and u.ended_at:
        u.session_duration_ms = u.ended_at - u.started_at

    sloc: SessionLoc | None = loc_acc.finish() if loc_acc is not None else None
    return u, sloc


def read_conversation(path: Path) -> list[dict]:
    """Extract the full ordered conversation from a session jsonl.

    Returns a flat list of message blocks in file order, capturing everything a
    reviewer would want to see — not just user text:

      * ``{"role": "user", "type": "text", "text": ...}``            — real user input
      * ``{"role": "assistant", "type": "text", "text": ...}``       — model replies
      * ``{"role": "assistant", "type": "thinking", "text": ...}``   — reasoning blocks
      * ``{"role": "assistant", "type": "tool_use", "name": ...,
            "input": {...}, "id": ...}``                              — tool calls (full input)
      * ``{"role": "tool", "type": "tool_result", "tool_use_id": ...,
            "is_error": bool, "text": ...}``                         — tool outputs

    Each block carries a ``ts`` (epoch ms) when the source line had a timestamp.
    Assistant responses split across several JSONL lines (one per content block,
    sharing a ``message.id``) are NOT deduplicated here — every content block is
    a distinct conversational event, so all are emitted in order.
    """
    convo: list[dict] = []
    for obj in iter_messages(path):
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        ts = parse_timestamp_ms(obj.get("timestamp"))
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                txt = content.strip()
                if txt:
                    convo.append({"role": "user", "type": "text", "text": txt, "ts": ts})
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    it = item.get("type")
                    if it == "text":
                        txt = (item.get("text") or "").strip()
                        if txt:
                            convo.append({"role": "user", "type": "text",
                                          "text": txt, "ts": ts})
                    elif it == "tool_result":
                        convo.append({
                            "role": "tool", "type": "tool_result",
                            "tool_use_id": item.get("tool_use_id"),
                            "is_error": bool(item.get("is_error")),
                            "text": extract_text(item.get("content")),
                            "ts": ts,
                        })
        elif role == "assistant":
            if isinstance(content, str):
                txt = content.strip()
                if txt:
                    convo.append({"role": "assistant", "type": "text", "text": txt, "ts": ts})
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    it = item.get("type")
                    if it == "text":
                        txt = (item.get("text") or "").strip()
                        if txt:
                            convo.append({"role": "assistant", "type": "text",
                                          "text": txt, "ts": ts})
                    elif it == "thinking":
                        txt = (item.get("thinking") or item.get("text") or "").strip()
                        if txt:
                            convo.append({"role": "assistant", "type": "thinking",
                                          "text": txt, "ts": ts})
                    elif it == "tool_use":
                        convo.append({
                            "role": "assistant", "type": "tool_use",
                            "name": item.get("name"),
                            "id": item.get("id"),
                            "input": item.get("input"),
                            "ts": ts,
                        })
    return convo


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
    cli_version: str | None = None
    git_branch: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None

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
        # 行级元数据：每条 assistant/user 行都携带（cc 2.x）。取首个非空值。
        if cli_version is None and isinstance(obj.get("version"), str):
            cli_version = obj["version"] or None
        if git_branch is None and isinstance(obj.get("gitBranch"), str):
            git_branch = obj["gitBranch"] or None
        if reasoning_effort is None and isinstance(obj.get("effort"), str):
            reasoning_effort = obj["effort"] or None
        if permission_mode is None and isinstance(obj.get("permissionMode"), str):
            permission_mode = obj["permissionMode"] or None
        # First real user message — only used when no ai-title exists at all.
        if fallback_title is None:
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("role") == "user":
                txt = extract_text(msg.get("content")).strip()
                if txt and not _is_user_noise(txt):
                    # Remove all XML-like tags (e.g. <ide_opened_file>...</ide_opened_file>)
                    txt = _strip_tags(txt)
                    if txt:
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
        cli_version=cli_version,
        git_branch=git_branch,
        reasoning_effort=reasoning_effort,
        permission_profile=permission_mode,
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

