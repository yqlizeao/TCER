"""会话记录阅读器各数据源 adapter：把源文件解析成统一的 ``Message`` 列表。

只读主会话：Claude 不读 ``subagents/``、跳过 ``isSidechain``；omp/pi 不读 ``<stem>/`` 子目录；
Codex 子代理会话 / OpenCode 子会话不并入。子代理只以 agent 类步骤出现，派发 prompt 只在该步骤的
参数里。

「用户提问」判定：Claude / WorkBuddy 经 :mod:`tcer.core.user_input`（SSOT），其它源复用各 reader
既有过滤（codex ``_events_with_user_messages``、omp ``_is_agent_attributed``、opencode
synthetic/ignored、antigravity step_type 14），保证 ``Transcript.question_count`` 与
``user_input.read_user_messages`` 同口径。

Portions of this file are ported from cc-switch
(https://github.com/farion1231/cc-switch), Copyright (c) 2025 Jason Young,
licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
Sources (src-tauri/src/session_manager/providers/):
  claude.rs, codex.rs, codex_items.rs, opencode_blocks.rs, pi.rs, pi_blocks.rs, blocks.rs
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from functools import reduce
from pathlib import Path
from types import SimpleNamespace

from tcer.core import tool_summary as ts_
from tcer.core import user_input as ui_
from tcer.core.reader import parse_timestamp_ms
from tcer.core.tool_summary import (
    EV_ABORTED, EV_COMPACTION, EV_ERROR, EV_HOOK, EV_INFO, EV_MODEL_CHANGE, EV_OTHER,
    EV_PR_LINK, EV_SLASH, EV_SUB_AGENT, EV_THINKING_LEVEL,
    INLINE_TEXT_MAX_CHARS, INPUT_PREVIEW_CHARS, KIND_AGENT, KIND_EDIT, KIND_MCP, KIND_READ,
    KIND_SEARCH, KIND_SHELL, KIND_WEB, KIND_WRITE,
    ST_ERROR, ST_INTERRUPTED, ST_PENDING, ST_SUCCESS, ST_UNKNOWN,
    THINKING_PREVIEW_CHARS, EVENT_PREVIEW_CHARS,
)
from tcer.core.transcript import (
    ContentRef, EventBlock, ImageBlock, ImageRef, Message, MessageMeta, StepBlock,
    TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock, Transcript,
    TranscriptCancelled, assign_turn_ids, build_turns, is_question_message,
    resume_command,
)

_REMINDER_RE = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)


# --------------------------------------------------------------------------- #
# 通用块构造
# --------------------------------------------------------------------------- #
def _check(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise TranscriptCancelled()


def strip_system_reminders(text: str) -> str:
    if "<system-reminder>" not in text:
        return text
    return _REMINDER_RE.sub("", text).strip()


def _opt_str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _dumps(v) -> str:
    return ts_.dumps_compact(v)


def make_text_block(text: str, ref: ContentRef | None = None) -> TextBlock:
    """超过 8KB 且能给出引用时只放预览（12 行 / 1200 字）+ full。"""
    if ref is not None and len(text) > INLINE_TEXT_MAX_CHARS:
        return TextBlock(ts_.preview(text).text, ref)
    return TextBlock(text)


def make_summary_event(kind: str, text: str, ref: ContentRef | None) -> EventBlock:
    """带长说明的事件（压缩摘要）：超过 400 字符时预览 + 引用。"""
    text = text.strip()
    p = ts_.preview_chars(text, EVENT_PREVIEW_CHARS)
    if not p.truncated:
        return EventBlock(kind, p.text)
    if ref is not None:
        return EventBlock(kind, p.text, None, ref)
    return EventBlock(kind, p.text + "…")


def make_thinking(text: str, summary: str | None = None, duration_ms: int | None = None,
                  ref: ContentRef | None = None, redacted: bool | None = None) -> ThinkingBlock:
    p = ts_.preview_chars(text.rstrip(), THINKING_PREVIEW_CHARS)
    return ThinkingBlock(
        text=p.text, summary=summary,
        redacted=(not text.strip()) if redacted is None else redacted,
        duration_ms=duration_ms, full=ref if p.truncated else None)


def make_result(call_id: str, status: str, text: str, ref: ContentRef | None = None, *,
                exit_code=None, duration_ms=None, images=None, saved_path=None) -> ToolResultBlock:
    p = ts_.preview(text)
    return ToolResultBlock(
        call_id=call_id, status=status, preview=p.text, total_len=p.total_len,
        line_count=p.line_count, truncated=p.truncated, full=ref if p.truncated else None,
        exit_code=exit_code, duration_ms=duration_ms, images=images or [], saved_path=saved_path)


def make_tool_call(style: str, call_id: str, raw_name: str, inp, *,
                   input_ref: ContentRef | None = None, namespace: str | None = None,
                   with_diff: bool = True) -> ToolCallBlock:
    """通用工具调用块：归一化 kind、提炼标题、参数预览；Edit/Write 先按参数估算 diff。"""
    n = ts_.normalize_tool(style, raw_name, namespace)
    kind = n.kind
    title, detail = ts_.title_from_input(kind, raw_name, n.server, inp)
    if not title:
        title = ts_.one_line_title(raw_name)
    if kind == KIND_SHELL:
        cmd = ts_.str_field(inp, ("command", "cmd", "script"))
        if cmd:
            kind = ts_.refine_shell_kind(cmd)
    if inp is None:
        pj = ""
    elif isinstance(inp, str):
        pj = inp
    else:
        pj = _dumps(inp)
    p = ts_.preview_chars(pj, INPUT_PREVIEW_CHARS)
    diff = ts_.diff_from_input(kind, inp) if with_diff and isinstance(inp, dict) else None
    return ToolCallBlock(
        id=call_id, raw_name=raw_name, kind=kind, title=title, detail=detail, server=n.server,
        input_preview=p.text, input_total_len=p.total_len,
        input_full=input_ref if p.truncated else None, diff=diff)


def _image_ref_b64(data_len: int, media_type: str | None, ref: ContentRef | None,
                   alt: str | None = None) -> ImageRef:
    return ImageRef(media_type=media_type or "image/png", size=data_len // 4 * 3, alt=alt,
                    source="inline", ref=ref)


def _models_and_counts(messages: list[Message]):
    models: list[str] = []
    seen: set[str] = set()
    questions = calls = 0
    started = ended = None
    for m in messages:
        if m.ts is not None:
            started = m.ts if started is None else min(started, m.ts)
            ended = m.ts if ended is None else max(ended, m.ts)
        if m.injected:
            continue
        if m.role == "assistant" and m.meta and m.meta.model:
            if m.meta.model not in seen:
                seen.add(m.meta.model)
                models.append(m.meta.model)
        if m.role == "user" and any(b.type == "text" and b.text.strip() for b in m.blocks):
            questions += 1
        for b in m.blocks:
            if b.type == "tool_call" and not b.by_user:
                calls += 1
    last_model = None
    for m in reversed(messages):
        if m.role == "assistant" and not m.injected and m.meta and m.meta.model:
            last_model = m.meta.model
            break
    return models, last_model, questions, calls, started, ended


def finalize(src: str, path: Path, session_id: str | None, messages: list[Message], *,
             title: str | None, cwd: str | None, warnings: list[str] | None = None) -> Transcript:
    messages = [m for m in messages if not m.is_empty()]
    assign_turn_ids(messages)
    style = ts_.SOURCE_STYLE.get(src, "generic")
    turns = build_turns(messages, style)
    models, last_model, questions, calls, started, ended = _models_and_counts(messages)
    return Transcript(
        source=src, path=str(path), session_id=session_id, title=title, cwd=cwd,
        started_ts=started, ended_ts=ended, models=models, model=last_model,
        question_count=questions, tool_call_count=calls,
        resume_command=resume_command(src, session_id, path), style=style,
        messages=messages, turns=turns, warnings=warnings or [])


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def _iter_jsonl_offsets(path: Path):
    """逐行产出 ``(字节偏移, 对象)``；与 ``reader.iter_messages`` 同样容错（坏行跳过）。"""
    with open(path, "rb") as fh:
        off = 0
        for raw in fh:
            n = len(raw)
            line = raw.strip()
            if line:
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    obj = None
                if isinstance(obj, dict):
                    yield off, obj
            off += n


def _structured_patch_counts(sp) -> tuple[int, int]:
    added = removed = 0
    if isinstance(sp, list):
        for hunk in sp:
            lines = hunk.get("lines") if isinstance(hunk, dict) else None
            if not isinstance(lines, list):
                continue
            for ln in lines:
                if isinstance(ln, str) and ln:
                    if ln[0] == "+":
                        added += 1
                    elif ln[0] == "-":
                        removed += 1
    return added, removed


def _tag_content(text: str, tag: str) -> str | None:
    open_, close = f"<{tag}>", f"</{tag}>"
    i = text.find(open_)
    if i < 0:
        return None
    start = i + len(open_)
    j = text.find(close, start)
    return text[start: j if j >= 0 else len(text)]


def _merge_meta(target: MessageMeta | None, nxt: MessageMeta | None) -> MessageMeta | None:
    if nxt is None:
        return target
    if target is None:
        return nxt
    for f in nxt.__dataclass_fields__:
        v = getattr(nxt, f)
        if v is not None:
            setattr(target, f, v)
    return target


class _Claude:
    def __init__(self, path: Path, cancel):
        self.path = path
        self.cancel = cancel
        self.messages: list[Message] = []
        self.last_api_id: str | None = None      # 上一条 message 的 api id（相邻合并用）
        self.seen_api_ids: set[str] = set()
        self.seen_blocks: dict[str, set] = {}
        self.seen_tool_use: set[str] = set()
        self.seen_tool_result: set[str] = set()
        self.calls: dict[str, tuple] = {}        # id → (ToolCallBlock, ts)
        self.pr_urls: set[str] = set()
        self.last_user_bash: tuple | None = None  # (Message, ToolCallBlock)
        self.session_dir = None
        try:
            from tcer.core import reader
            self.session_dir = os.path.normcase(os.path.abspath(reader.session_artifacts(path)[2]))
        except Exception:   # noqa: BLE001
            self.session_dir = None

    # ---- 入口 ---------------------------------------------------------------- #
    def run(self) -> list[Message]:
        n = -1
        for off, obj in _iter_jsonl_offsets(self.path):
            n += 1
            if n % 512 == 0:
                _check(self.cancel)
            if obj.get("isSidechain") is True or obj.get("isMeta") is True:
                continue
            ts = parse_timestamp_ms(obj.get("timestamp"))
            typ = obj.get("type")
            if typ == "system":
                self.system(obj, ts, off)
            elif typ == "pr-link":
                self.pr_link(obj, ts)
            elif typ in ("user", "assistant", None):
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role") if isinstance(msg.get("role"), str) else typ
                if obj.get("isCompactSummary") is True:
                    self.compact_summary(msg.get("content"), ts, off)
                elif role == "user":
                    self.user(obj, msg, ts, off)
                elif role == "assistant":
                    self.assistant(obj, msg, ts, off)
        return self.messages

    # ---- 推送 ---------------------------------------------------------------- #
    def push(self, role, ts, mid, blocks, injected=False, api_id=None, meta=None) -> Message:
        m = Message(role=role, ts=ts, id=mid, injected=injected, blocks=blocks, meta=meta)
        self.messages.append(m)
        self.last_api_id = api_id
        return m

    def push_event(self, ts, kind, text=None, url=None):
        self.push("system", ts, None, [EventBlock(kind, text, url)])

    # ---- system / pr-link ----------------------------------------------------- #
    def system(self, obj, ts, off):
        sub = obj.get("subtype")
        if sub == "stop_hook_summary":
            errs = obj.get("hookErrors")
            items = []
            if isinstance(errs, list):
                for e in errs:
                    s = e.strip() if isinstance(e, str) else _dumps(e)
                    if s:
                        items.append(s)
            if items:
                self.push_event(ts, EV_HOOK, "\n".join(items))
        elif sub == "compact_boundary":
            c = obj.get("content")
            text = c.strip() if isinstance(c, str) and c.strip() else "Conversation compacted"
            self.push_event(ts, EV_COMPACTION, text)
        elif sub == "local_command":
            c = obj.get("content")
            if isinstance(c, str):
                # 只认斜杠命令 / 注入；其余一律当注入（避免凭空多出「提问」，口径同 read_user_messages）
                u = ui_.classify_text_blocks([c])
                if u.kind == ui_.SLASH:
                    self.push("user", ts, obj.get("uuid"), [EventBlock(EV_SLASH, u.text)])
                elif c.strip():
                    self.push("user", ts, obj.get("uuid"),
                              [make_text_block(c, ContentRef("jsonl", str(self.path), off, pointer="/content"))],
                              injected=True)

    def pr_link(self, obj, ts):
        url = obj.get("prUrl")
        if not isinstance(url, str) or not url.strip() or url in self.pr_urls:
            return
        self.pr_urls.add(url)
        n = obj.get("prNumber")
        text = f"PR #{n}" if isinstance(n, (int, str)) and not isinstance(n, bool) and str(n) else "PR"
        self.push_event(ts, EV_PR_LINK, text, url)

    def compact_summary(self, content, ts, off):
        if isinstance(content, str):
            summary = content
        elif isinstance(content, list):
            summary = "\n".join(it.get("text", "") for it in content
                                if isinstance(it, dict) and it.get("type") == "text"
                                and isinstance(it.get("text"), str))
        else:
            summary = ""
        if not summary.strip():
            return
        block = make_summary_event(EV_COMPACTION, summary,
                                   ContentRef("jsonl", str(self.path), off, pointer="/message/content"))
        last = self.messages[-1] if self.messages else None
        if (last is not None and last.role == "system" and len(last.blocks) == 1
                and last.blocks[0].type == "event" and last.blocks[0].kind == EV_COMPACTION):
            last.blocks[0] = block
            return
        self.push("system", ts, None, [block])

    # ---- assistant ------------------------------------------------------------ #
    def assistant(self, obj, msg, ts, off):
        mid = msg.get("id") if isinstance(msg.get("id"), str) and msg.get("id") else None
        model = msg.get("model") if isinstance(msg.get("model"), str) else None
        synthetic = model == "<synthetic>"
        thinking_ms = obj.get("thinkingDurationMs")
        thinking_ms = int(thinking_ms) if isinstance(thinking_ms, (int, float)) and not isinstance(thinking_ms, bool) and thinking_ms >= 0 else None
        replay = mid is not None and mid in self.seen_api_ids and mid != self.last_api_id
        seen_here = self.seen_blocks.setdefault(mid, set()) if mid else None
        content = msg.get("content")
        blocks: list = []
        events: list[str] = []

        def text_item(text):
            if not text.strip():
                return
            if seen_here is not None:
                key = ("t", hash(text))
                if replay and key in seen_here:
                    return
                seen_here.add(key)
            if synthetic:
                events.append(text)
            else:
                blocks.append(TextBlock(text))

        if isinstance(content, str):
            text_item(content)
        elif isinstance(content, list):
            for i, item in enumerate(content):
                if not isinstance(item, dict):
                    continue
                it = item.get("type")
                if it == "text":
                    t = item.get("text")
                    if isinstance(t, str):
                        text_item(t)
                elif it in ("thinking", "redacted_thinking"):
                    text = item.get("thinking") if isinstance(item.get("thinking"), str) else ""
                    has_sig = bool(_opt_str(item.get("signature")))
                    redacted = it == "redacted_thinking" or (not text.strip() and has_sig)
                    if not text.strip() and not redacted:
                        continue
                    if seen_here is not None:
                        key = ("k", hash(text), has_sig)
                        if replay and key in seen_here:
                            continue
                        seen_here.add(key)
                    blocks.append(make_thinking(
                        text, duration_ms=thinking_ms, redacted=redacted,
                        ref=ContentRef("jsonl", str(self.path), off,
                                       pointer=f"/message/content/{i}/thinking")))
                elif it in ("tool_use", "server_tool_use"):
                    cid = item.get("id") if isinstance(item.get("id"), str) else ""
                    if cid:
                        if cid in self.seen_tool_use:      # rewind 重放：同 id 只出现一次
                            continue
                        self.seen_tool_use.add(cid)
                    blocks.append(self.tool_call(item, i, off, ts))
        for text in events:   # <synthetic> 文本：info 事件，不当最终回复
            self.push_event(ts, EV_INFO, text)
        if not blocks:
            return
        meta = self.message_meta(msg, model if not synthetic else None)
        last = self.messages[-1] if self.messages else None
        if (mid is not None and last is not None and last.role == "assistant"
                and self.last_api_id == mid):
            last.blocks.extend(blocks)
            last.meta = _merge_meta(last.meta, meta)
        else:
            self.push("assistant", ts, obj.get("uuid"), blocks, api_id=mid, meta=meta)
        if mid:
            self.seen_api_ids.add(mid)
            self.last_api_id = mid

    @staticmethod
    def message_meta(msg, model) -> MessageMeta | None:
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}

        def tok(k):
            v = usage.get(k)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

        sr = msg.get("stop_reason")
        meta = MessageMeta(
            model=model if model else None,
            input_tokens=tok("input_tokens"), output_tokens=tok("output_tokens"),
            cache_read_tokens=tok("cache_read_input_tokens"),
            cache_write_tokens=tok("cache_creation_input_tokens"),
            stop_reason=sr if isinstance(sr, str) else None)
        return None if meta.is_empty() else meta

    def tool_call(self, item, index, off, ts) -> ToolCallBlock:
        cid = item.get("id") if isinstance(item.get("id"), str) else ""
        raw = item.get("name") if isinstance(item.get("name"), str) else "unknown"
        inp = item.get("input")
        pj = "" if inp is None else _dumps(inp)
        p = ts_.preview_chars(pj, INPUT_PREVIEW_CHARS)
        n = ts_.normalize_tool("claude", raw)
        kind, title, detail = ts_.claude_tool_title(raw, n.kind, n.server, inp)
        diff = self.input_diff(raw, inp)
        block = ToolCallBlock(
            id=cid, raw_name=raw, kind=kind, title=title, detail=detail, server=n.server,
            input_preview=p.text, input_total_len=p.total_len,
            input_full=(ContentRef("jsonl", str(self.path), off,
                                   pointer=f"/message/content/{index}/input")
                        if p.truncated else None),
            diff=diff)
        if cid:
            self.calls[cid] = (block, ts)
        return block

    @staticmethod
    def input_diff(raw, inp) -> dict | None:
        if not isinstance(inp, dict):
            return None
        path = inp.get("file_path") if isinstance(inp.get("file_path"), str) else ""

        def s(d, k):
            v = d.get(k) if isinstance(d, dict) else None
            return v if isinstance(v, str) else ""

        if raw == "Edit":
            a, r = ts_.line_change_counts(s(inp, "old_string"), s(inp, "new_string"))
            op = "update"
        elif raw == "MultiEdit":
            edits = inp.get("edits")
            if not isinstance(edits, list):
                return None
            a = r = 0
            for e in edits:
                x, y = ts_.line_change_counts(s(e, "old_string"), s(e, "new_string"))
                a += x
                r += y
            op = "update"
        elif raw == "Write":
            a, r, op = ts_.count_lines(s(inp, "content")), 0, "add"
        else:
            return None
        if not path:
            return None
        return ts_.single_file_diff(path, op, a, r)

    # ---- user ----------------------------------------------------------------- #
    def user(self, obj, msg, ts, off):
        content = msg.get("content")
        tur = obj.get("toolUseResult") if isinstance(obj.get("toolUseResult"), dict) else None
        texts: list[str] = []
        result_items: list[tuple[int, dict]] = []
        images: list[ImageBlock] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for i, it in enumerate(content):
                if not isinstance(it, dict):
                    continue
                t = it.get("type")
                if t == "text":
                    if isinstance(it.get("text"), str):
                        texts.append(it["text"])
                elif t == "image":
                    src = it.get("source") if isinstance(it.get("source"), dict) else {}
                    if src.get("type") == "base64" and isinstance(src.get("data"), str):
                        images.append(ImageBlock(_image_ref_b64(
                            len(src["data"]), src.get("media_type"),
                            ContentRef("jsonl", str(self.path), off,
                                       pointer=f"/message/content/{i}/source/data"))))
                elif t == "tool_result":
                    result_items.append((i, it))
        u = ui_.classify_claude_record(obj)
        if u is None:
            return
        single = tur if len(result_items) == 1 else None
        results = []
        for i, it in result_items:
            b = self.tool_result(it, i, single, ts, off)
            if b is not None:
                results.append(b)
        uuid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else None
        blocks: list = list(results)
        injected = False
        k = u.kind
        if k == ui_.QUESTION:
            blocks.append(TextBlock(u.text))
        elif k == ui_.SLASH:
            blocks.append(EventBlock(EV_SLASH, u.text))
        elif k == ui_.ABORTED:
            blocks.append(EventBlock(EV_ABORTED, u.text))
        elif k == ui_.BASH:
            first = next((t for t in texts if t.strip()), "")
            blocks.extend(self.user_bash(u.text, first, uuid))
        elif k == ui_.BASH_OUTPUT:
            if self.attach_bash_output("\n".join(texts)):
                if not blocks:
                    return
            else:
                injected = True
                blocks.append(TextBlock("\n\n".join(texts)))
        elif k == ui_.INJECTED:
            injected = True
            for ix, t in self._texts_with_index(content):
                ptr = "/message/content" if isinstance(content, str) else f"/message/content/{ix}/text"
                blocks.append(make_text_block(t, ContentRef("jsonl", str(self.path), off, pointer=ptr)))
        elif k == ui_.COMPACT:
            self.compact_summary(content, ts, off)
            if not blocks:
                return
        # TOOL_RESULT / EMPTY / SIDECHAIN：只有结果块
        blocks.extend(images)
        if not blocks:
            return
        role = "tool" if all(b.type == "tool_result" for b in blocks) else "user"
        m = self.push(role, ts, uuid, blocks, injected=injected)
        if k == ui_.BASH:
            for b in blocks:
                if b.type == "tool_call" and b.by_user:
                    self.last_user_bash = (m, b)
        elif k != ui_.BASH_OUTPUT:
            self.last_user_bash = None

    @staticmethod
    def _texts_with_index(content):
        if isinstance(content, str):
            return [(0, content)]
        out = []
        if isinstance(content, list):
            for i, it in enumerate(content):
                if isinstance(it, dict) and it.get("type") == "text" and isinstance(it.get("text"), str):
                    out.append((i, it["text"]))
        return out

    def user_bash(self, command: str, first: str, uuid) -> list:
        """用户 !cmd：by_user 的 Bash 调用 +（同记录里有 stdout/stderr 时）结果。"""
        cid = f"user-bash-{uuid or ''}"
        p = ts_.preview_chars(command, INPUT_PREVIEW_CHARS)
        call = ToolCallBlock(
            id=cid, raw_name="Bash", kind=ts_.refine_shell_kind(command),
            title=ts_.one_line_title(command), input_preview=p.text,
            input_total_len=p.total_len, by_user=True)
        blocks = [call]
        res = self.bash_result(cid, first)
        if res is not None:
            blocks.append(res)
        return blocks

    @staticmethod
    def bash_result(cid: str, text: str) -> ToolResultBlock | None:
        out = _tag_content(text, "bash-stdout")
        err = _tag_content(text, "bash-stderr")
        if out is None and err is None:
            return None
        out = (out or "").rstrip()
        err = (err or "").rstrip()
        output = out if not err else (err if not out else f"{out}\n{err}")
        # 只有 stderr 不代表失败，状态未知
        return make_result(cid, ST_SUCCESS if not err else ST_UNKNOWN, output)

    def attach_bash_output(self, text: str) -> bool:
        """``<bash-stdout>`` 独立成记录：挂到上一条用户 !cmd 的调用上。"""
        if self.last_user_bash is None:
            return False
        m, call = self.last_user_bash
        if any(b.type == "tool_result" and b.call_id == call.id for b in m.blocks):
            return False
        res = self.bash_result(call.id, text)
        if res is None:
            return False
        m.blocks.append(res)
        return True

    # ---- tool_result ---------------------------------------------------------- #
    def tool_result(self, item, index, tur, ts, off) -> ToolResultBlock | None:
        call_id = item.get("tool_use_id") if isinstance(item.get("tool_use_id"), str) else ""
        if call_id:
            if call_id in self.seen_tool_result:           # rewind 重放：同 id 只出现一次
                return None
            self.seen_tool_result.add(call_id)
        base = f"/message/content/{index}/content"
        content = item.get("content")
        images: list[ImageRef] = []
        pointer: str | None = None
        text = ""
        if isinstance(content, str):
            text, pointer = content, base
        elif isinstance(content, list):
            parts: list[tuple[int, str]] = []
            for j, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                t = part.get("type")
                if t == "text" and isinstance(part.get("text"), str):
                    parts.append((j, part["text"]))
                elif t == "image":
                    src = part.get("source") if isinstance(part.get("source"), dict) else {}
                    if src.get("type") == "base64" and isinstance(src.get("data"), str):
                        images.append(_image_ref_b64(
                            len(src["data"]), src.get("media_type"),
                            ContentRef("jsonl", str(self.path), off, pointer=f"{base}/{j}/source/data")))
            if len(parts) == 1:
                text, pointer = parts[0][1], f"{base}/{parts[0][0]}/text"
            elif parts:
                text, pointer = "\n".join(t for _, t in parts), base
        if "<system-reminder>" in text:
            text = strip_system_reminders(text)
        p = ts_.preview(text)
        is_error = item.get("is_error") is True
        interrupted = bool(tur and tur.get("interrupted") is True)
        status = ST_INTERRUPTED if interrupted else (ST_ERROR if is_error else ST_SUCCESS)
        slot = self.calls.get(call_id) if call_id else None
        is_shell = slot is not None and slot[0].raw_name == "Bash"
        exit_code = ts_.parse_exit_code(text)
        if exit_code is None:
            background = bool(tur and tur.get("backgroundTaskId") is not None)
            if is_shell and status == ST_SUCCESS and not background:
                exit_code = 0
        duration = None
        if is_shell and slot[1] is not None and ts is not None and ts >= slot[1]:
            duration = ts - slot[1]
        saved = tur.get("persistedOutputPath") if tur else None
        saved = saved if isinstance(saved, str) and saved else None
        full = None
        if p.truncated:
            if saved and self._in_session_dir(saved):
                full = ContentRef("file", saved)
            elif pointer:
                full = ContentRef("jsonl", str(self.path), off, pointer=pointer)
        if slot is not None:
            self.backfill(slot[0], status, tur)
        return ToolResultBlock(
            call_id=call_id, status=status, preview=p.text, total_len=p.total_len,
            line_count=p.line_count, truncated=p.truncated, full=full,
            exit_code=exit_code, duration_ms=duration, images=images, saved_path=saved)

    def _in_session_dir(self, saved: str) -> bool:
        if not self.session_dir:
            return False
        s = os.path.normcase(os.path.abspath(saved))
        return s.startswith(self.session_dir + os.sep) and os.path.isfile(saved)

    @staticmethod
    def backfill(call: ToolCallBlock, status: str, tur) -> None:
        """结果到来时回填调用块：Edit/Write 用 structuredPatch 修正 diff，失败的改动去掉 diff；
        Agent 用 resolvedModel 补 detail。"""
        raw = call.raw_name
        if raw in ("Edit", "MultiEdit", "Write"):
            if status != ST_SUCCESS:
                call.diff = None
                return
            if not tur:
                return
            op = {"create": "add", "update": "update"}.get(tur.get("type"))
            path = tur.get("filePath") if isinstance(tur.get("filePath"), str) else None
            a, r = _structured_patch_counts(tur.get("structuredPatch"))
            patch = (a, r) if a + r > 0 else None
            if op is None and patch is None:
                return
            if call.diff is None:
                call.diff = ts_.single_file_diff("", "update", 0, 0)
            f = call.diff["files"][0]
            if op:
                f["op"] = op
            if path:
                f["path"] = path
            if patch:
                f["added"], f["removed"] = patch
            call.diff["added"], call.diff["removed"] = f["added"], f["removed"]
        elif raw in ("Agent", "Task") and call.detail is None and tur:
            rm = tur.get("resolvedModel")
            if isinstance(rm, str) and rm:
                call.detail = rm


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
_CODEX_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>",
                            "<user_instructions>", "<turn_aborted>")
_VSCODE_CTX = "# Context from my IDE setup:"
_CODEX_MARKER = "my request for codex"
_EXEC_CMD_RE = re.compile(r'exec_command\(\s*\{\s*"?cmd"?\s*:\s*("(?:[^"\\]|\\.)*")')
_EXEC_WORKDIR_RE = re.compile(r'"?workdir"?\s*:\s*("(?:[^"\\]|\\.)*")')
_EXEC_MCP_RE = re.compile(r"tools\.(mcp__[A-Za-z0-9_\-]+)\s*\(")


def _is_wrapped_in_tag(text: str) -> bool:
    if not text.startswith("<"):
        return False
    rest = text[1:]
    m = re.match(r"[^>\s]*", rest)
    name = m.group(0) if m else ""
    return bool(name) and re.fullmatch(r"[A-Za-z0-9_\-]+", name) is not None \
        and text.endswith(f"</{name}>")


def _is_image_wrapper(text: str) -> bool:
    return text == "</image>" or (text.startswith("<image ") and text.endswith(">") and "\n" not in text)


def _codex_ide_prompt(text: str) -> str | None:
    lines = text.replace("\r\n", "\n").split("\n")
    prompt = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("#"):
            continue
        heading = s.lstrip("#").lstrip()
        if not heading.lower().startswith(_CODEX_MARKER):
            continue
        suffix = heading[len(_CODEX_MARKER):].lstrip()
        if not suffix:
            inline = ""
        elif suffix[0] in ":：-—":
            inline = suffix.lstrip(" \t:：-—").strip()
        else:
            continue
        if inline:
            prompt = inline
        else:
            following = "\n".join(lines[i + 1:]).strip()
            prompt = following or None
    return prompt


def _decode_js_string(lit: str) -> str:
    try:
        return json.loads(lit)
    except ValueError:
        pass
    try:
        return json.loads(lit.replace("\\'", "'"))
    except ValueError:
        return lit.strip('"')


def _is_shell_program(p: str) -> bool:
    return p.rsplit("/", 1)[-1] in ("zsh", "bash", "sh", "fish", "dash")


def codex_command(value) -> str:
    """CommandExecution.command / exec_command.cmd：数组 ``[zsh,-lc,script]`` 取 script；
    字符串去掉 shell 包装前缀。"""
    if isinstance(value, str):
        s = value.lstrip()
        parts = s.split(" ", 2)
        if len(parts) == 3 and _is_shell_program(parts[0]) and parts[1] in ("-lc", "-c"):
            rest = parts[2].strip()
            for q in ("'", '"'):
                if len(rest) >= 2 and rest.startswith(q) and rest.endswith(q):
                    return rest[1:-1]
            return rest
        return value
    if isinstance(value, list):
        parts = [p for p in value if isinstance(p, str)]
        if len(parts) == 3 and _is_shell_program(parts[0]) and parts[1] in ("-lc", "-c", "-l -c"):
            return parts[2]
        return " ".join(parts)
    return ""


def _strip_output_header(text: str) -> tuple[str, int | None, int | None, bool]:
    """去掉 ``Chunk ID… / Wall time: X seconds / Process exited with code N / Output:`` 这类头。
    返回 ``(正文, exit_code, duration_ms, failed)``；只有前几行全是已知头字段且以 ``Output:`` 结束才剥。"""
    exit_code = duration = None
    failed = False
    rest = text
    for _ in range(8):
        line, sep, after = rest.partition("\n")
        line = line.rstrip("\r")
        if line == "Output:":
            return after, exit_code, duration, failed
        if line.startswith("Script "):
            status = line[7:]
            if "fail" in status or "error" in status:
                failed = True
        elif line.startswith("Wall time"):
            s = line[9:].lstrip(":").strip()
            if s.endswith("seconds"):
                try:
                    duration = int(round(float(s[:-7].strip()) * 1000))
                except ValueError:
                    pass
        elif line.startswith("Process exited with code ") or line.startswith("Exit code: "):
            code = line.split("code ", 1)[1] if line.startswith("Process") else line[11:]
            try:
                exit_code = int(code.strip())
            except ValueError:
                pass
        elif not (line.startswith("Chunk ID:") or line.startswith("Original token count:")
                  or line.startswith("Process running with session ID")):
            break
        if not after:
            break
        rest = after
    return text, None, None, False


def _legacy_shell_output(text: str):
    if not text.lstrip().startswith('{"output"'):
        return None
    try:
        d = json.loads(text)
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("output"), str):
        return None
    meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    ec = meta.get("exit_code")
    ds = meta.get("duration_seconds")
    return (d["output"], ec if isinstance(ec, int) and not isinstance(ec, bool) else None,
            int(round(ds * 1000)) if isinstance(ds, (int, float)) and not isinstance(ds, bool) else None)


def _apply_patch_rejected(output: str) -> bool:
    o = output.lstrip()
    return o.startswith("apply_patch verification failed") or o.startswith("patch rejected")


def parse_apply_patch(patch: str) -> list[dict]:
    """apply_patch 文本（``*** Begin Patch`` 格式）→ 文件列表与增删行数。"""
    files: list[dict] = []
    for line in ts_._lines(patch):
        def hdr(prefix):
            return line[len(prefix):].strip() if line.startswith(prefix) else None
        p = hdr("*** Add File:")
        if p is not None:
            files.append({"path": p, "op": "add", "added": 0, "removed": 0})
            continue
        p = hdr("*** Update File:")
        if p is not None:
            files.append({"path": p, "op": "update", "added": 0, "removed": 0})
            continue
        p = hdr("*** Delete File:")
        if p is not None:
            files.append({"path": p, "op": "delete", "added": 0, "removed": 0})
            continue
        p = hdr("*** Move to:")
        if p is not None:
            if files:
                files[-1]["op"] = "rename"
                files[-1]["path"] = p
            continue
        if line.startswith("*** End Patch"):
            break
        if line.startswith("***"):
            continue
        if files:
            if line.startswith("+"):
                files[-1]["added"] += 1
            elif line.startswith("-") and files[-1]["op"] != "add":
                files[-1]["removed"] += 1
    return files


def _diff_summary(files: list[dict]) -> dict:
    return {"files": files, "added": sum(f["added"] for f in files),
            "removed": sum(f["removed"] for f in files), "full": None}


class _Codex:
    def __init__(self, path: Path, cancel):
        self.path = path
        self.cancel = cancel
        self.messages: list[Message] = []
        self.turn_id: str | None = None
        self.last_model: str | None = None
        self.pending_model: tuple | None = None
        self.calls: dict[str, tuple] = {}     # call_id → (ToolCallBlock, flavor)
        self.open: list[str] = []
        self.last_compaction: Message | None = None
        self.session_id: str | None = None
        self.cwd: str | None = None
        self.n = 0
        self.skipped_ide = 0

    def ref(self, pointer: str) -> ContentRef:
        return ContentRef("jsonl_idx", str(self.path), index=self.n, pointer=pointer, source="codex")

    # ---- 推送 ------------------------------------------------------------------ #
    def _flush_model(self):
        if self.pending_model is not None:
            model, ts = self.pending_model
            self.pending_model = None
            self.messages.append(Message("system", ts, None, self.turn_id, False,
                                         [EventBlock(EV_MODEL_CHANGE, model)]))

    def push(self, role, ts, mid, blocks, injected=False) -> Message:
        self._flush_model()
        self.last_compaction = None
        m = Message(role, ts, mid, self.turn_id, injected, blocks)
        self.messages.append(m)
        return m

    def push_assistant(self, ts, mid, blocks) -> Message:
        self._flush_model()
        last = self.messages[-1] if self.messages else None
        if (last is not None and last.role == "assistant" and last.turn_id == self.turn_id
                and last.blocks and all(b.type == "thinking" for b in last.blocks)):
            last.blocks.extend(blocks)
            last.ts = ts if ts is not None else last.ts
            if mid:
                last.id = mid
            self.last_compaction = None
            return last
        m = self.push("assistant", ts, mid, blocks)
        if self.last_model:
            m.meta = MessageMeta(model=self.last_model)
        return m

    def start_turn(self, turn: str):
        if self.turn_id != turn:
            self.turn_id = turn
        self._flush_model()

    def note_model(self, model: str, ts):
        if self.last_model is not None and self.last_model != model:
            self.pending_model = (model, ts)
        self.last_model = model

    def compaction_event(self, ts, summary_block: EventBlock | None):
        if self.last_compaction is not None:
            if summary_block is not None:
                first = self.last_compaction.blocks[0]
                if first.type == "event" and first.text is None:
                    self.last_compaction.blocks[0] = summary_block
            return
        block = summary_block or EventBlock(EV_COMPACTION, None)
        m = self.push("system", ts, None, [block])
        self.last_compaction = m

    def abort_turn(self, ts, reason: str):
        for cid in list(self.open):
            self.calls.pop(cid, None)
            self.push("tool", ts, None, [make_result(cid, ST_INTERRUPTED, "")])
        self.open.clear()
        self.push("system", ts, None, [EventBlock(EV_ABORTED, reason)])

    # ---- 主循环 ----------------------------------------------------------------- #
    def run(self) -> list[Message]:
        from tcer.core import codex_reader as cr
        for n, (obj, umsg) in enumerate(cr._events_with_user_messages(self.path)):
            self.n = n
            if n % 512 == 0:
                _check(self.cancel)
            if not isinstance(obj, dict):
                continue
            top = obj.get("type")
            pl = obj.get("payload")
            if not isinstance(pl, dict):
                continue
            ts = parse_timestamp_ms(obj.get("timestamp"))
            if top == "session_meta":
                if self.session_id is None:
                    self.session_id = _opt_str(pl.get("id")) or _opt_str(pl.get("session_id"))
                if self.cwd is None:
                    self.cwd = _opt_str(pl.get("cwd"))
            elif top == "turn_context":
                tid = _opt_str(pl.get("turn_id"))
                if tid:
                    self.start_turn(tid)
                model = _opt_str(pl.get("model"))
                if model:
                    self.last_model = model
            elif top == "compacted":
                msg = pl.get("message")
                blk = (make_summary_event(EV_COMPACTION, msg, self.ref("/payload/message"))
                       if isinstance(msg, str) and msg.strip() else None)
                self.compaction_event(ts, blk)
            elif top == "event_msg":
                self.event_msg(pl, ts, umsg, cr, obj)
            elif top == "response_item":
                self.response_item(pl, ts, umsg, cr, obj)
        return self.messages

    def event_msg(self, pl, ts, umsg, cr, obj):
        sub = pl.get("type")
        if sub == "user_message":
            if umsg is not None:
                self.question(umsg, ts, _opt_str(pl.get("id")))
        elif sub == "task_started":
            tid = _opt_str(pl.get("turn_id"))
            if tid:
                self.start_turn(tid)
        elif sub == "turn_aborted":
            r = pl.get("reason")
            reason = r if isinstance(r, str) and r else (_dumps(r) if r else "interrupted")
            self.abort_turn(ts, reason)
        elif sub == "thread_settings_applied":
            s = pl.get("thread_settings")
            model = _opt_str(s.get("model")) if isinstance(s, dict) else None
            if model:
                self.note_model(model, ts)
        elif sub == "context_compacted":
            self.compaction_event(ts, None)
        elif sub == "patch_apply_end":
            self.patch_end(pl)

    def question(self, umsg: dict, ts, mid):
        text = umsg["text"]
        if text.startswith(_VSCODE_CTX):
            prompt = _codex_ide_prompt(text)
            if prompt:
                text = prompt
        self.push("user", ts, mid, [TextBlock(text)])

    def response_item(self, pl, ts, umsg, cr, obj):
        sub = pl.get("type")
        if sub == "message":
            role = pl.get("role")
            mid = _opt_str(pl.get("id"))
            if role == "user":
                if umsg is not None:
                    self.question(umsg, ts, mid)
                elif cr._user_message(obj) is None:
                    text = cr._message_text(pl.get("content")).strip()
                    if text and not _is_image_wrapper(text):
                        self.push("user", ts, mid,
                                  [make_text_block(text, self.ref("/payload/content"))], injected=True)
            elif role == "assistant":
                text = cr._message_text(pl.get("content"))
                if text.strip():
                    m = self.push_assistant(ts, mid, [TextBlock(text)])
                    phase = _opt_str(pl.get("phase"))
                    if phase:
                        m.meta = m.meta or MessageMeta()
                        m.meta.stop_reason = phase
            else:   # developer / system：注入
                text = cr._message_text(pl.get("content"))
                if text.strip():
                    self.push("system", ts, mid,
                              [make_text_block(text, self.ref("/payload/content"))], injected=True)
        elif sub == "reasoning":
            self.reasoning(pl, ts, cr)
        elif sub == "function_call":
            self.function_call(pl, ts)
        elif sub == "custom_tool_call":
            self.custom_call(pl, ts)
        elif sub == "local_shell_call":
            self.local_shell(pl, ts)
        elif sub in ("function_call_output", "custom_tool_call_output"):
            self.output(pl, ts)
        elif sub == "web_search_call":
            self.web_search(pl, ts)
        elif sub == "compaction":
            self.compaction_event(ts, None)
        elif sub == "agent_message":
            self.agent_message(pl, ts)

    # ---- 子项 ------------------------------------------------------------------- #
    def reasoning(self, pl, ts, cr):
        def joined(v):
            if isinstance(v, list):
                parts = [x.get("text", "") if isinstance(x, dict) else str(x) for x in v]
                return "\n\n".join(p for p in parts if p)
            return v if isinstance(v, str) else ""
        summary = joined(pl.get("summary"))
        content = joined(pl.get("content"))
        redacted = not summary.strip() and not content.strip()
        if redacted:
            last = self.messages[-1] if self.messages else None
            if (last is not None and last.role == "assistant" and last.turn_id == self.turn_id
                    and last.blocks and last.blocks[-1].type == "thinking"
                    and last.blocks[-1].redacted):
                return
        blk = make_thinking(content, summary=summary if summary.strip() else None,
                            ref=self.ref("/payload/content"), redacted=redacted)
        self.push_assistant(ts, _opt_str(pl.get("id")), [blk])

    def _register(self, ts, mid, call: ToolCallBlock, flavor: str, extra_blocks=()):
        m = self.push_assistant(ts, mid, [call, *extra_blocks])
        if call.id:
            if call.id in self.open:
                self.open.remove(call.id)
            self.open.append(call.id)
            self.calls[call.id] = (call, flavor)
        return m

    def function_call(self, pl, ts):
        name = _opt_str(pl.get("name")) or "unknown"
        ns = _opt_str(pl.get("namespace"))
        raw_args = pl.get("arguments") if isinstance(pl.get("arguments"), str) else ""
        try:
            args = json.loads(raw_args) if raw_args else None
        except ValueError:
            args = None
        call_id = _opt_str(pl.get("call_id")) or _opt_str(pl.get("id")) or ""
        n = ts_.normalize_tool("codex", name, ns)
        kind, detail, flavor = n.kind, None, "other"

        def sa(key):
            v = args.get(key) if isinstance(args, dict) else None
            return v if isinstance(v, str) and v.strip() else None

        if kind == KIND_MCP:
            flavor = "mcp"
            detail = ts_.first_string_field(args)
            title = ts_.title_mcp(n.server or "mcp", name)
        elif kind == KIND_SHELL and name == "write_stdin":
            chars = sa("chars")
            if chars:
                title = ts_.one_line_title(chars)
            elif isinstance(args, dict) and args.get("session_id") is not None:
                title = f"session {args['session_id']}"
            else:
                title = ts_.one_line_title(name)
        elif kind == KIND_SHELL:
            flavor = "shell"
            cmdv = args.get("cmd") if isinstance(args, dict) and "cmd" in args else (
                args.get("command") if isinstance(args, dict) else None)
            cmd = codex_command(cmdv)
            detail = sa("workdir")
            kind = ts_.refine_shell_kind(cmd)
            title = ts_.one_line_title(cmd)
        elif kind == "ask":
            q = None
            if isinstance(args, dict):
                qs = args.get("questions")
                if isinstance(qs, list) and qs and isinstance(qs[0], dict):
                    for k in ("title", "question", "header"):
                        if isinstance(qs[0].get(k), str):
                            q = qs[0][k]
                            break
                if q is None:
                    q = sa("question")
            title = ts_.one_line_title(q) if q else ts_.one_line_title(name)
        elif kind == KIND_AGENT:
            detail = sa("model")
            v = next((sa(k) for k in ("task_name", "description", "agent_type", "target", "name")
                      if sa(k)), None)
            title = ts_.one_line_title(v) if v else ts_.one_line_title(name)
        elif kind == "todo":
            plan = (args.get("plan") if isinstance(args, dict) else None)
            plan = plan if isinstance(plan, list) else (args.get("todos") if isinstance(args, dict) else None)
            title = ts_.title_todo(len(plan) if isinstance(plan, list) else 0)
        elif kind in (KIND_READ, KIND_WRITE, KIND_EDIT):
            path = sa("path") or sa("file_path")
            if path and kind == KIND_READ:
                def num(k):
                    v = args.get(k)
                    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
                title, detail = ts_.title_read(path, num("offset"), num("limit"))
            elif path:
                title = ts_.one_line_title(path)
            else:
                title = ts_.one_line_title(name)
        elif kind == KIND_WEB:
            q = sa("query") or sa("url") or sa("q")
            title = ts_.title_web([q]) if q else ts_.one_line_title(name)
        else:
            detail = ts_.first_string_field(args)
            title = ts_.one_line_title(name)
        p = ts_.preview_chars(raw_args, INPUT_PREVIEW_CHARS)
        call = ToolCallBlock(
            id=call_id, raw_name=name, kind=kind, title=title, detail=detail, server=n.server,
            input_preview=p.text, input_total_len=p.total_len,
            input_full=self.ref("/payload/arguments") if p.truncated else None)
        self._register(ts, _opt_str(pl.get("id")), call, flavor)

    def custom_call(self, pl, ts):
        name = _opt_str(pl.get("name")) or "unknown"
        inp = pl.get("input") if isinstance(pl.get("input"), str) else ""
        call_id = _opt_str(pl.get("call_id")) or _opt_str(pl.get("id")) or ""
        p = ts_.preview_chars(inp, INPUT_PREVIEW_CHARS)
        kind = KIND_SHELL if name == "exec" else ts_.KIND_OTHER
        title, detail, server, diff, flavor = ts_.one_line_title(name), None, None, None, "other"
        if name == "exec":
            flavor = "exec"
            cmds = [c for c in (_decode_js_string(m.group(1)) for m in _EXEC_CMD_RE.finditer(inp))
                    if c.strip()]
            wd = _EXEC_WORKDIR_RE.search(inp)
            detail = _decode_js_string(wd.group(1)) if wd else None
            mcp = _EXEC_MCP_RE.search(inp)
            mcp_split = ts_.split_mcp_name(mcp.group(1)) if mcp else None
            if not cmds:
                if mcp_split:
                    kind, title, server = KIND_MCP, ts_.title_mcp(*mcp_split), mcp_split[0]
                else:
                    title = ts_.one_line_title(inp)
            else:
                first = cmds[0]
                kind = ts_.refine_shell_kind(first)
                title = ts_.one_line_title(first)
                if len(cmds) > 1 and not title.endswith("…"):
                    title += "…"
        elif name == "apply_patch":
            flavor = "apply_patch"
            files = parse_apply_patch(inp)
            diff = _diff_summary(files)
            kind = KIND_WRITE if files and all(f["op"] == "add" for f in files) else KIND_EDIT
            if len(files) == 1:
                title = ts_.one_line_title(files[0]["path"])
            elif files:
                title = ts_.one_line_title(f"{files[0]['path']} 等 {len(files)} 个文件")
            else:
                title = ""
            title = title or ts_.one_line_title(name)
        else:
            n = ts_.normalize_tool("codex", name)
            kind, server = n.kind, n.server
        call = ToolCallBlock(
            id=call_id, raw_name=name, kind=kind, title=title, detail=detail, server=server,
            input_preview=p.text, input_total_len=p.total_len,
            input_full=self.ref("/payload/input") if p.truncated else None, diff=diff)
        if diff is not None and p.truncated:
            diff["full"] = self.ref("/payload/input")
        self._register(ts, _opt_str(pl.get("id")), call, flavor)

    def local_shell(self, pl, ts):
        action = pl.get("action") if isinstance(pl.get("action"), dict) else {}
        cmd = codex_command(action.get("command"))
        call_id = _opt_str(pl.get("call_id")) or _opt_str(pl.get("id")) or ""
        wd = action.get("working_directory")
        p = ts_.preview_chars(_dumps(action), INPUT_PREVIEW_CHARS)
        call = ToolCallBlock(
            id=call_id, raw_name="local_shell", kind=ts_.refine_shell_kind(cmd),
            title=ts_.one_line_title(cmd), detail=wd if isinstance(wd, str) else None,
            input_preview=p.text, input_total_len=p.total_len,
            input_full=self.ref("/payload/action") if p.truncated else None)
        self._register(ts, _opt_str(pl.get("id")), call, "shell")

    def web_search(self, pl, ts):
        cid = _opt_str(pl.get("id")) or f"ws_{len(self.messages)}"
        action = pl.get("action") if isinstance(pl.get("action"), dict) else None
        title = ""
        if action:
            qs = action.get("queries")
            items = [q for q in qs if isinstance(q, str) and q.strip()] if isinstance(qs, list) else []
            if items:
                title = ts_.title_web(items)
            elif isinstance(action.get("query"), str) and action["query"].strip():
                title = ts_.title_web([action["query"]])
            elif isinstance(action.get("url"), str) and action["url"].strip():
                pat = action.get("pattern")
                title = ts_.title_web([f"{pat} in {action['url']}" if isinstance(pat, str) and pat.strip() else action["url"]])
        p = ts_.preview_chars(_dumps(action) if action else "", INPUT_PREVIEW_CHARS)
        call = ToolCallBlock(id=cid, raw_name="web_search", kind=KIND_WEB,
                             title=title or "web_search", input_preview=p.text,
                             input_total_len=p.total_len)
        st = pl.get("status")
        status = ST_ERROR if st in ("failed", "error") else (
            ST_INTERRUPTED if st in ("cancelled", "canceled", "interrupted") else ST_SUCCESS)
        self.push_assistant(ts, cid, [call, make_result(cid, status, "")])

    def output(self, pl, ts):
        call_id = _opt_str(pl.get("call_id")) or ""
        out = pl.get("output")
        images: list = []
        exit_code = duration = None
        failed = False
        if isinstance(out, str):
            legacy = _legacy_shell_output(out)
            if legacy:
                text, exit_code, duration = legacy
            else:
                text, exit_code, duration, failed = _strip_output_header(out)
        elif isinstance(out, list):
            pieces = []
            for it in out:
                if not isinstance(it, dict):
                    continue
                if it.get("type") in ("input_image", "image"):
                    url = it.get("image_url")
                    if isinstance(url, str):
                        images.append(ImageRef(media_type="image/png", size=len(url) // 4 * 3,
                                               source="inline"))
                    continue
                t = it.get("text")
                if isinstance(t, str):
                    body, ec, du, fl = _strip_output_header(t)
                    exit_code = exit_code if exit_code is not None else ec
                    duration = duration if duration is not None else du
                    failed = failed or fl
                    if body:
                        pieces.append(body)
            text = "".join(pieces)
        elif out is None:
            text = ""
        else:
            text = json.dumps(out, ensure_ascii=False, default=str)
        entry = self.calls.pop(call_id, None)
        if call_id in self.open:
            self.open.remove(call_id)
        status = ST_SUCCESS
        if failed or (exit_code is not None and exit_code != 0):
            status = ST_ERROR
        if entry is not None and entry[1] == "apply_patch" and _apply_patch_rejected(text):
            status = ST_ERROR
            call = entry[0]
            if call.diff:
                call.diff["added"] = call.diff["removed"] = 0
                for f in call.diff["files"]:
                    f["added"] = f["removed"] = 0
        block = make_result(call_id, status, text, self.ref("/payload/output"),
                            exit_code=exit_code, duration_ms=duration, images=images)
        self.push("tool", ts, None, [block])

    def patch_end(self, pl):
        """patch_apply_end.changes：{path: {type, unified_diff, …}} → 用真实 diff 修正行数。"""
        cid = _opt_str(pl.get("call_id"))
        changes = pl.get("changes")
        # 调用已随输出结束：在已推送的消息里找到它（从后往前，轮内很近）
        if not cid or not isinstance(changes, dict):
            return
        call = None
        for m in reversed(self.messages[-200:]):
            for b in m.blocks:
                if b.type == "tool_call" and b.id == cid:
                    call = b
                    break
            if call:
                break
        if call is None or call.raw_name != "apply_patch" or not call.diff:
            return
        if any(f["added"] + f["removed"] == 0 for f in call.diff["files"]) and \
                call.diff["added"] + call.diff["removed"] == 0:
            return    # 补丁被拒（已清零）不覆盖
        files = []
        for path, ch in changes.items():
            if not isinstance(ch, dict):
                continue
            ud = ch.get("unified_diff")
            a, r = ts_.count_diff_lines(ud) if isinstance(ud, str) else (0, 0)
            op = {"add": "add", "update": "update", "delete": "delete"}.get(ch.get("type"), "update")
            if ch.get("move_path"):
                op = "rename"
            files.append({"path": path, "op": op, "added": a, "removed": r})
        if files:
            full = call.diff.get("full")
            call.diff = _diff_summary(files)
            call.diff["full"] = full
            call.kind = KIND_WRITE if all(f["op"] == "add" for f in files) else KIND_EDIT

    def agent_message(self, pl, ts):
        content = pl.get("content")
        text = ""
        if isinstance(content, list):
            text = "\n".join(it.get("text", "") for it in content if isinstance(it, dict)
                             and isinstance(it.get("text"), str))
        elif isinstance(content, str):
            text = content
        text = text.strip()
        first, _, rest = text.partition("\n")
        author = _opt_str(pl.get("author")) or ""
        ev = f"{author}: {first.strip()}" if author else first.strip()
        blocks: list = [EventBlock(EV_SUB_AGENT, ev)]
        if rest.strip():
            blocks.append(TextBlock(rest.strip()))
        self.push("system", ts, _opt_str(pl.get("id")), blocks)


# --------------------------------------------------------------------------- #
# omp / Pi
# --------------------------------------------------------------------------- #
_OMP_KINDS = {
    "read": KIND_READ, "write": KIND_WRITE, "edit": KIND_EDIT, "ast_edit": KIND_EDIT,
    "grep": KIND_SEARCH, "search": KIND_SEARCH, "glob": KIND_SEARCH, "find": KIND_SEARCH,
    "ls": KIND_SEARCH, "ast_grep": KIND_SEARCH,
    "bash": KIND_SHELL, "eval": KIND_SHELL, "ssh": KIND_SHELL, "bashExecution": KIND_SHELL,
    "todo": "todo", "task": KIND_AGENT, "web_search": KIND_WEB, "fetch": KIND_WEB, "ask": "ask",
}


def _omp_call(src: str, call_id: str, name: str, args, ref_args: ContentRef | None) -> ToolCallBlock:
    n = ts_.normalize_tool("pi", name)
    kind = _OMP_KINDS.get(name, n.kind) if not ts_.split_mcp_name(name) else n.kind
    server = n.server
    title, detail = ts_.title_from_input(kind, name, server, args)
    if not title:
        title = ts_.one_line_title(name)
    if kind == KIND_SHELL:
        cmd = ts_.str_field(args, ("command", "cmd", "script"))
        if cmd:
            kind = ts_.refine_shell_kind(cmd)
    pj = "" if args is None else (args if isinstance(args, str) else _dumps(args))
    p = ts_.preview_chars(pj, INPUT_PREVIEW_CHARS)
    diff = ts_.diff_from_input(kind, args) if isinstance(args, dict) else None
    return ToolCallBlock(id=call_id, raw_name=name, kind=kind, title=title, detail=detail,
                         server=server, input_preview=p.text, input_total_len=p.total_len,
                         input_full=ref_args if p.truncated else None, diff=diff)


class _Omp:
    """omp / Pi 共用（同 schema）。只取活动分支。"""

    def __init__(self, src: str, path: Path, cancel):
        self.src = src
        self.path = path
        self.cancel = cancel
        self.items: list[tuple[str | None, list[Message]]] = []   # (entry id, messages)
        self.parent: dict[str, str | None] = {}
        self.latest_id: str | None = None
        self.calls: dict[str, ToolCallBlock] = {}
        self.header: dict = {}
        self.title: str | None = None
        self.has_ids = False

    def ref(self, n: int, pointer: str) -> ContentRef:
        return ContentRef("jsonl_idx", str(self.path), index=n, pointer=pointer, source=self.src)

    def run(self) -> list[Message]:
        from tcer.core import omp_reader
        for n, ev in enumerate(omp_reader.iter_events(self.path)):
            if n % 512 == 0:
                _check(self.cancel)
            if not isinstance(ev, dict):
                continue
            typ = ev.get("type")
            if typ == "session":
                if not self.header:
                    self.header = ev
                    t = ev.get("title")
                    if isinstance(t, str) and t.strip():
                        self.title = t.strip()
                continue
            if typ in ("title", "title_change"):
                t = ev.get("title")
                if isinstance(t, str) and t.strip():
                    self.title = t.strip()
            eid = ev.get("id") if isinstance(ev.get("id"), str) and ev.get("id") else None
            if eid is not None:
                self.has_ids = True
                pid = ev.get("parentId")
                self.parent[eid] = pid if isinstance(pid, str) and pid else None
                self.latest_id = eid
            msgs = self.entry(ev, n, omp_reader)
            if msgs:
                self.items.append((eid, msgs))
        return self.select()

    def select(self) -> list[Message]:
        version = self.header.get("version") if isinstance(self.header.get("version"), int) else 1
        if not self.has_ids or version < 2:
            return [m for _, ms in self.items for m in ms]
        active: set[str] = set()
        cur = self.latest_id
        while cur is not None and cur not in active and cur in self.parent:
            active.add(cur)
            cur = self.parent[cur]
        return [m for eid, ms in self.items if eid is not None and eid in active for m in ms]

    # ---- 单条记录 ---------------------------------------------------------------- #
    def entry(self, ev, n, omp_reader) -> list[Message]:
        typ = ev.get("type")
        ets = parse_timestamp_ms(ev.get("timestamp"))
        if typ == "message":
            mm = ev.get("message")
            if not isinstance(mm, dict):
                return []
            ts = parse_timestamp_ms(mm.get("timestamp")) or ets
            role = mm.get("role")
            if role == "user":
                return self.user(mm, ts, ev, n, omp_reader)
            if role == "assistant":
                return self.assistant(mm, ts, n)
            if role == "toolResult":
                return self.tool_result(mm, ts, n, omp_reader)
            if role == "bashExecution":
                return self.bash_exec(mm, ts, ev, n)
            if role in ("branchSummary", "compactionSummary"):
                s = mm.get("summary")
                if isinstance(s, str) and s.strip():
                    return [Message("system", ts, None, None, False,
                                    [make_summary_event(EV_COMPACTION, s, self.ref(n, "/message/summary"))])]
            return []
        if typ == "model_change":
            model = ev.get("modelId") or ev.get("model")
            prov = ev.get("provider")
            if not isinstance(model, str) or not model:
                return []
            text = f"{prov}/{model}" if isinstance(prov, str) and prov else model
            return [Message("system", ets, None, None, False, [EventBlock(EV_MODEL_CHANGE, text)])]
        if typ == "thinking_level_change":
            lv = ev.get("thinkingLevel")
            if isinstance(lv, str) and lv.strip():
                return [Message("system", ets, None, None, False, [EventBlock(EV_THINKING_LEVEL, lv)])]
            return []
        if typ in ("compaction", "branch_summary"):
            s = ev.get("summary")
            if isinstance(s, str) and s.strip():
                return [Message("system", ets, None, None, False,
                                [make_summary_event(EV_COMPACTION, s, self.ref(n, "/summary"))])]
            return []
        if typ == "custom_message" and ev.get("display") is not False:
            c = ev.get("content")
            text = c if isinstance(c, str) else omp_reader._message_text(c)
            if text.strip():
                return [Message("system", ets, None, None, False, [TextBlock(text)])]
        return []

    def user(self, mm, ts, ev, n, omp_reader) -> list[Message]:
        content = mm.get("content")
        blocks: list = []
        if isinstance(content, str):
            if content.strip():
                blocks.append(TextBlock(content))
        elif isinstance(content, list):
            for i, it in enumerate(content):
                if isinstance(it, dict):
                    if it.get("type") == "image" and isinstance(it.get("data"), str):
                        blocks.append(ImageBlock(_image_ref_b64(
                            len(it["data"]), it.get("mimeType") or it.get("mediaType"),
                            self.ref(n, f"/message/content/{i}/data"))))
                    elif isinstance(it.get("text"), str) and it["text"].strip():
                        blocks.append(TextBlock(it["text"]))
                elif isinstance(it, str) and it.strip():
                    blocks.append(TextBlock(it))
        if not blocks:
            return []
        injected = omp_reader._is_agent_attributed(mm)     # advisor / 审查 prompt：注入
        if injected:
            blocks = [make_text_block(b.text, self.ref(n, "/message/content"))
                      if b.type == "text" else b for b in blocks]
        # 非注入：与 omp_reader.read_user_messages 同口径，无文字（纯图片）不算提问
        return [Message("user", ts, ev.get("id") if isinstance(ev.get("id"), str) else None,
                        None, injected, blocks)]

    def assistant(self, mm, ts, n) -> list[Message]:
        content = mm.get("content")
        blocks: list = []
        if isinstance(content, str):
            if content.strip():
                blocks.append(TextBlock(content))
        elif isinstance(content, list):
            for i, it in enumerate(content):
                if not isinstance(it, dict):
                    continue
                t = it.get("type")
                if t == "thinking":
                    text = it.get("thinking") if isinstance(it.get("thinking"), str) else ""
                    sig = _opt_str(it.get("thinkingSignature"))
                    redacted = not text.strip()
                    if redacted and not sig:
                        continue
                    blocks.append(make_thinking(text, ref=self.ref(n, f"/message/content/{i}/thinking"),
                                                redacted=redacted))
                elif t == "text":
                    if isinstance(it.get("text"), str) and it["text"].strip():
                        blocks.append(TextBlock(it["text"]))
                elif t == "toolCall":
                    cid = it.get("id") if isinstance(it.get("id"), str) else ""
                    name = it.get("name") if isinstance(it.get("name"), str) else "unknown"
                    args = it.get("arguments")
                    if isinstance(args, str):
                        try:
                            parsed = json.loads(args)
                            if isinstance(parsed, (dict, list)):
                                args = parsed
                        except ValueError:
                            pass
                    call = _omp_call(self.src, cid, name, args,
                                     self.ref(n, f"/message/content/{i}/arguments"))
                    if cid:
                        self.calls[cid] = call
                    blocks.append(call)
        sr = mm.get("stopReason")
        if sr == "aborted":
            blocks.append(EventBlock(EV_ABORTED, None))
        elif sr == "length":
            blocks.append(EventBlock(EV_OTHER, "truncated"))
        elif sr == "error":
            em = mm.get("errorMessage")
            blocks.append(EventBlock(EV_ERROR, em if isinstance(em, str) and em else None))
        if not blocks:
            return []
        usage = mm.get("usage") if isinstance(mm.get("usage"), dict) else {}

        def tok(k):
            v = usage.get(k)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

        cost = usage.get("cost")
        cost = cost.get("total") if isinstance(cost, dict) else None
        dur = mm.get("duration", mm.get("durationMs"))
        meta = MessageMeta(
            model=_opt_str(mm.get("model")), input_tokens=tok("input"), output_tokens=tok("output"),
            cache_read_tokens=tok("cacheRead"), cache_write_tokens=tok("cacheWrite"),
            reasoning_tokens=tok("reasoning") or tok("reasoningTokens"),
            cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0 else None,
            duration_ms=int(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur >= 0 else None,
            stop_reason=_opt_str(sr))
        return [Message("assistant", ts, None, None, False, blocks, None if meta.is_empty() else meta)]

    def tool_result(self, mm, ts, n, omp_reader) -> list[Message]:
        cid = mm.get("toolCallId") if isinstance(mm.get("toolCallId"), str) else ""
        content = mm.get("content")
        details = mm.get("details") if isinstance(mm.get("details"), dict) else {}
        texts: list[tuple[int, str]] = []
        images: list[ImageRef] = []
        if isinstance(content, str):
            texts.append((-1, content))
        elif isinstance(content, list):
            for i, it in enumerate(content):
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "text" and isinstance(it.get("text"), str):
                    texts.append((i, it["text"]))
                elif it.get("type") == "image" and isinstance(it.get("data"), str):
                    images.append(_image_ref_b64(len(it["data"]), it.get("mimeType"),
                                                 self.ref(n, f"/message/content/{i}/data")))
        text = "\n".join(t for _, t in texts)
        if len(texts) == 1 and texts[0][0] >= 0:
            ptr = f"/message/content/{texts[0][0]}/text"
        else:
            ptr = "/message/content"
        status = ST_ERROR if mm.get("isError") is True else ST_SUCCESS
        ec = details.get("exitCode")
        exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
        du = details.get("durationMs")
        duration = int(du) if isinstance(du, (int, float)) and not isinstance(du, bool) and du >= 0 else None
        full_out = details.get("fullOutputPath")
        block = make_result(cid, status, text, self.ref(n, ptr), exit_code=exit_code,
                            duration_ms=duration, images=images,
                            saved_path=full_out if isinstance(full_out, str) and full_out else None)
        call = self.calls.get(cid)
        d = details.get("diff")
        if call is not None and isinstance(d, str) and call.kind in (KIND_EDIT, KIND_WRITE):
            a, r = ts_.count_diff_lines(d)
            path = (call.diff["files"][0]["path"] if call.diff and call.diff.get("files")
                    else call.title)
            op = (call.diff["files"][0]["op"] if call.diff and call.diff.get("files") else "update")
            call.diff = ts_.single_file_diff(path, op, a, r)
            call.diff["full"] = self.ref(n, "/message/details/diff")
        return [Message("tool", ts, None, None, False, [block])]

    def bash_exec(self, mm, ts, ev, n) -> list[Message]:
        eid = ev.get("id") if isinstance(ev.get("id"), str) else f"bash-{n}"
        cmd = mm.get("command") if isinstance(mm.get("command"), str) else ""
        out = mm.get("output") if isinstance(mm.get("output"), str) else ""
        p = ts_.preview_chars(cmd, INPUT_PREVIEW_CHARS)
        call = ToolCallBlock(id=eid, raw_name="bashExecution", kind=KIND_SHELL,
                             title=ts_.one_line_title(cmd), input_preview=p.text,
                             input_total_len=p.total_len, by_user=True,
                             input_full=self.ref(n, "/message/command") if p.truncated else None)
        ec = mm.get("exitCode")
        exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
        status = (ST_INTERRUPTED if mm.get("cancelled") is True
                  else ST_ERROR if exit_code not in (None, 0) else ST_SUCCESS)
        res = make_result(eid, status, out, self.ref(n, "/message/output"), exit_code=exit_code,
                          saved_path=_opt_str(mm.get("fullOutputPath")))
        return [Message("user", ts, eid, None, False, [call, res])]


# --------------------------------------------------------------------------- #
# OpenCode
# --------------------------------------------------------------------------- #
def _oc_time_span(state) -> int | None:
    t = state.get("time") if isinstance(state, dict) else None
    if isinstance(t, dict):
        s, e = t.get("start"), t.get("end")
        if isinstance(s, (int, float)) and isinstance(e, (int, float)) and e >= s:
            return int(e - s)
    return None


def _oc_total_tokens(tokens) -> int | None:
    if not isinstance(tokens, dict):
        return None
    t = tokens.get("total")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return int(t)
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    s = 0
    for v in (tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
              cache.get("read"), cache.get("write")):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s += int(v)
    return s or None


def _oc_meta(info: dict) -> MessageMeta | None:
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

    def cnt(v):
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

    mid, pid = info.get("modelID"), info.get("providerID")
    model = (f"{pid}/{mid}" if isinstance(pid, str) and pid and isinstance(mid, str) and mid
             else (mid if isinstance(mid, str) and mid else None))
    tm = info.get("time") if isinstance(info.get("time"), dict) else {}
    cr, cp = tm.get("created"), tm.get("completed")
    dur = (int(cp - cr) if isinstance(cr, (int, float)) and isinstance(cp, (int, float)) and cp >= cr else None)
    cost = info.get("cost")
    meta = MessageMeta(
        model=model, input_tokens=cnt(tokens.get("input")), output_tokens=cnt(tokens.get("output")),
        cache_read_tokens=cnt(cache.get("read")), cache_write_tokens=cnt(cache.get("write")),
        reasoning_tokens=cnt(tokens.get("reasoning")),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0 else None,
        duration_ms=dur, stop_reason=_opt_str(info.get("finish")))
    return None if meta.is_empty() else meta


class _OpenCode:
    def __init__(self, db: Path, sid: str, cancel):
        self.db, self.sid, self.cancel = db, sid, cancel

    def ref(self, part_id: str, pointer: str) -> ContentRef:
        return ContentRef("sqlite", str(self.db), pointer=pointer, table="part", row_id=part_id)

    def run(self) -> list[Message]:
        from tcer.core import opencode_reader as oc
        with oc._connect(self.db) as con:
            if not oc._has_table(con, "message") or not oc._has_table(con, "part"):
                return []
            msg_rows = con.execute(
                "select id, data, time_created from message where session_id = ? "
                "order by time_created, id", (self.sid,)).fetchall()
            infos: dict[str, dict] = {}
            roles: dict[str, str] = {}
            order: list[str] = []
            created: dict[str, int | None] = {}
            for r in msg_rows:
                mid = str(r["id"])
                data = oc._json_obj(r["data"])
                info = data if isinstance(data, dict) else {}
                role = oc._first_str(info.get("role"),
                                     r["data"] if r["data"] in ("user", "assistant") else None)
                roles[mid] = role or "assistant"
                infos[mid] = info
                order.append(mid)
                tm = info.get("time") if isinstance(info.get("time"), dict) else {}
                created[mid] = parse_timestamp_ms(tm.get("created")) or parse_timestamp_ms(r["time_created"])
            parts_by_msg: dict[str, list[Message | object]] = {}
            n = 0
            cur = con.execute("select id, message_id, data, time_created from part "
                              "where session_id = ? order by time_created, id", (self.sid,))
            for row in cur:
                n += 1
                if n % 256 == 0:
                    _check(self.cancel)
                mid = str(row["message_id"])
                data = oc._json_obj(row["data"])
                if not isinstance(data, dict):
                    continue
                role = roles.get(mid, "assistant")
                ts = parse_timestamp_ms(row["time_created"])
                parts_by_msg.setdefault(mid, []).append(
                    self.part(role, str(row["id"]), data, ts, oc))
        out: list[Message] = []
        for mid in order:
            role = roles[mid]
            ts = created.get(mid)
            items = parts_by_msg.get(mid, [])
            if role == "user":
                # 与 opencode_reader.read_user_messages 同口径：每个合格文本 part 一条用户消息
                pending_imgs: list = []
                last_user: Message | None = None
                for it in items:
                    if isinstance(it, str):
                        last_user = Message("user", ts, mid, None, False, [TextBlock(it)])
                        out.append(last_user)
                    elif it is not None:
                        pending_imgs.append(it)
                if pending_imgs:
                    if last_user is not None:
                        last_user.blocks.extend(pending_imgs)
                    else:
                        out.append(Message("user", ts, mid, None, False, pending_imgs))
                continue
            blocks: list = []
            for it in items:
                if isinstance(it, list):
                    blocks.extend(it)
                elif it is not None and not isinstance(it, str):
                    blocks.append(it)
            ev = self.message_error(infos[mid])
            if ev is not None:
                blocks.append(ev)
            if blocks:
                out.append(Message(role, ts, mid, None, False, blocks, _oc_meta(infos[mid]) if role == "assistant" else None))
        return out

    @staticmethod
    def message_error(info):
        err = info.get("error")
        if not isinstance(err, dict):
            return None
        name = err.get("name") if isinstance(err.get("name"), str) else ""
        if name == "MessageAbortedError":
            return EventBlock(EV_ABORTED, None)
        d = err.get("data") if isinstance(err.get("data"), dict) else {}
        text = d.get("message") if isinstance(d.get("message"), str) and d.get("message") else name
        return EventBlock(EV_ERROR, text or None)

    def part(self, role: str, pid: str, part: dict, ts, oc):
        """part → 用户：合格文本返回 str、图片 / 文件返回块或 None；助手：块列表。"""
        ptype = part.get("type")
        if role == "user":
            if ptype == "text":
                if part.get("synthetic") is True or part.get("ignored") is True:
                    return None
                t = part.get("text")
                if isinstance(t, str) and t.strip():
                    return t.strip()
                return None
            if ptype == "file":
                return self.file_part(pid, part)
            return None
        blocks: list = []
        if ptype == "text":
            if part.get("synthetic") is True or part.get("ignored") is True:
                return None
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                blocks.append(TextBlock(t))
        elif ptype == "reasoning":
            text = part.get("text") if isinstance(part.get("text"), str) else ""
            tm = part.get("time") if isinstance(part.get("time"), dict) else {}
            dur = None
            if isinstance(tm.get("start"), (int, float)) and isinstance(tm.get("end"), (int, float)) \
                    and tm["end"] >= tm["start"]:
                dur = int(tm["end"] - tm["start"])
            blocks.append(make_thinking(text, duration_ms=dur, ref=self.ref(pid, "/text")))
        elif ptype == "step-start":
            blocks.append(StepBlock("start"))
        elif ptype == "step-finish":
            c = part.get("cost")
            blocks.append(StepBlock("finish", tokens=_oc_total_tokens(part.get("tokens")),
                                    cost_usd=float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else None,
                                    reason=_opt_str(part.get("reason"))))
        elif ptype == "tool":
            blocks.extend(self.tool(pid, part))
        elif ptype == "agent":
            nm = part.get("name")
            if isinstance(nm, str):
                blocks.append(EventBlock(EV_OTHER, f"@{nm}"))
        elif ptype == "patch":
            blocks.extend(self.patch(pid, part))
        elif ptype == "file":
            b = self.file_part(pid, part)
            if b is not None:
                blocks.append(b)
        return blocks or None

    def file_part(self, pid, part):
        mime = part.get("mime") if isinstance(part.get("mime"), str) else ""
        url = part.get("url") if isinstance(part.get("url"), str) else ""
        fn = _opt_str(part.get("filename"))
        if mime.startswith("image/"):
            if url.startswith("file://"):
                return ImageBlock(ImageRef(media_type=mime, alt=fn, source="local_file", path=url[7:]))
            if url.startswith("data:"):
                data = url.split(",", 1)[1] if "," in url else ""
                return ImageBlock(_image_ref_b64(len(data), mime, self.ref(pid, "/url"), fn))
        text = fn or (url or None)
        return EventBlock(EV_OTHER, text) if text else None

    def tool(self, pid, part) -> list:
        name = part.get("tool") if isinstance(part.get("tool"), str) else (
            part.get("name") if isinstance(part.get("name"), str) else "unknown")
        cid = part.get("callID") if isinstance(part.get("callID"), str) else (
            part.get("id") if isinstance(part.get("id"), str) else "")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inp = state.get("input") if "input" in state else part.get("input")
        meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else None
        st_title = _opt_str(state.get("title"))
        call = make_tool_call("opencode", cid, name, inp, input_ref=self.ref(pid, "/state/input"))
        if call.kind in (KIND_EDIT, KIND_WRITE) and st_title:
            call.title = ts_.one_line_title(st_title)
        if not call.title and st_title:
            call.title = ts_.one_line_title(st_title)
        if meta is not None and call.kind in (KIND_EDIT, KIND_WRITE):
            path = ts_.str_field(inp, ("filePath", "file_path", "path")) or call.title
            op = ("update" if meta.get("exists") is True else "add") if call.kind == KIND_WRITE else "update"
            fd = meta.get("filediff") if isinstance(meta.get("filediff"), dict) else {}
            a, d = fd.get("additions"), fd.get("deletions")
            counts = None
            if isinstance(a, int) and isinstance(d, int):
                counts = (a, d)
            elif isinstance(meta.get("diff"), str):
                counts = ts_.count_diff_lines(meta["diff"])
            if counts is not None:
                call.diff = ts_.single_file_diff(path, op, counts[0], counts[1])
                if isinstance(meta.get("diff"), str):
                    call.diff["full"] = self.ref(pid, "/state/metadata/diff")
            elif call.diff:
                for f in call.diff["files"]:
                    f["op"] = op
        status_raw = state.get("status")
        error = state.get("error") if isinstance(state.get("error"), str) else ""
        if status_raw == "completed":
            status = ST_SUCCESS
        elif status_raw == "error":
            status = ST_INTERRUPTED if "abort" in error.lower() else ST_ERROR
        elif status_raw in ("running", "pending"):
            status = ST_PENDING
        else:
            status = ST_UNKNOWN
        output = state.get("output") if isinstance(state.get("output"), str) else None
        if output is not None and (status != ST_ERROR or not error):
            text, ptr = output, "/state/output"
        elif error:
            text, ptr = error, "/state/error"
        else:
            text, ptr = "", "/state/output"
        ex = meta.get("exit") if meta else None
        res = make_result(cid, status, text, self.ref(pid, ptr),
                          exit_code=ex if isinstance(ex, int) and not isinstance(ex, bool) else None,
                          duration_ms=_oc_time_span(state))
        return [call, res]

    def patch(self, pid, part) -> list:
        files = [f for f in part.get("files", []) if isinstance(f, str)] if isinstance(part.get("files"), list) else []
        if not files:
            return []
        cid = _opt_str(part.get("id")) or _opt_str(part.get("hash")) or "patch"
        call = make_tool_call("opencode", cid, "patch", part.get("files"), with_diff=False)
        call.title = ts_.one_line_title(files[0])
        call.detail = f"{len(files)} 个文件" if len(files) > 1 else None
        call.diff = _diff_summary([{"path": f, "op": "update", "added": 0, "removed": 0} for f in files])
        return [call, make_result(cid, ST_SUCCESS, "\n".join(files))]


# --------------------------------------------------------------------------- #
# Antigravity
# --------------------------------------------------------------------------- #
def _agy_build(path: Path, cancel) -> tuple[list[Message], list[str]]:
    from tcer.core import antigravity_reader as agy
    msgs: list[Message] = []
    warnings: list[str] = []
    calls: dict[str, ToolCallBlock] = {}
    paired: set[str] = set()
    if not path.is_file():
        return msgs, warnings
    with agy._connect_ro(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx ASC")
        for k, (idx, stype, meta, payload) in enumerate(cur.fetchall()):
            if k % 256 == 0:
                _check(cancel)
            sec = agy._decode_timestamp(meta)
            ts = int(sec * 1000) if sec is not None else None
            if stype == 14:
                txt = agy._extract_user_text(payload)
                if txt:
                    msgs.append(Message("user", ts, str(idx), None, False, [TextBlock(txt)]))
            elif stype == 15:
                name, args_json, cid = agy._extract_tool_call(payload)
                blocks: list = []
                thought, said = _agy_text_fields(agy, payload)
                if thought:
                    blocks.append(make_thinking(thought, summary=None, redacted=False))
                if said:
                    blocks.append(TextBlock(said))
                if name:
                    canon = agy._canon_tool(name, args_json)
                    try:
                        args = json.loads(args_json) if args_json else None
                    except ValueError:
                        args = None
                    call = _agy_call(canon, name, cid or f"agy-{idx}", args, args_json)
                    calls[call.id] = call
                    blocks.append(call)
                if blocks:
                    msgs.append(Message("assistant", ts, str(idx), None, False, blocks))
            elif stype == 132:
                out_text, exit_code = agy._extract_tool_result(payload)
                # 结果 payload 不带 call_id：按出现顺序配最近一个未配对的调用
                target = next((c for c in reversed(list(calls.values()))
                               if c.id not in paired), None)
                cid = target.id if target is not None else ""
                if target is not None:
                    paired.add(target.id)
                status = ST_ERROR if exit_code else ST_SUCCESS
                msgs.append(Message("tool", ts, str(idx), None, False,
                                    [make_result(cid, status, out_text, exit_code=exit_code)]))
            elif stype == 101:
                txt = _agy_f114(agy, payload)
                if txt:
                    msgs.append(Message("system", ts, str(idx), None, True,
                                        [make_text_block(txt)]))
            elif stype == 17:
                txt = _agy_f24(agy, payload)
                if txt:
                    msgs.append(Message("system", ts, str(idx), None, False,
                                        [EventBlock(EV_ERROR, txt)]))
            elif stype == 23:
                summ = _agy_summary(agy, payload)
                msgs.append(Message("system", ts, str(idx), None, False,
                                    [make_summary_event(EV_COMPACTION, summ, None) if summ
                                     else EventBlock(EV_COMPACTION, None)]))
    warnings.append("antigravity：结果步骤无 call_id，按出现顺序配对最近的未配对调用；"
                    "思考文本取自步骤 payload 字段 20.3、助手回复取自 20.1（均为推断）")
    return msgs, warnings


def _agy_str(b) -> str:
    return b.decode("utf-8", errors="ignore") if isinstance(b, bytes) else ""


def _agy_text_fields(agy, payload) -> tuple[str, str]:
    """step 15 payload 字段 20：``3`` = 思考文本（推断）、``1`` = 助手可见回复（无工具名的步骤即最终回复）。"""
    if not payload:
        return "", ""
    f20 = agy._find_field(agy._parse_proto_fields(payload), 20)
    if not isinstance(f20, bytes):
        return "", ""
    sub = agy._parse_proto_fields(f20)
    return _agy_str(agy._find_field(sub, 3)).strip(), _agy_str(agy._find_field(sub, 1)).strip()


def _agy_f114(agy, payload) -> str:
    if not payload:
        return ""
    return _agy_str(agy._find_field(agy._parse_proto_fields(payload), 114)).strip()


def _agy_f24(agy, payload) -> str:
    if not payload:
        return ""
    s = _agy_str(agy._find_field(agy._parse_proto_fields(payload), 24))
    return "".join(ch for ch in s if ch.isprintable() or ch in "\n\t").strip()


def _agy_summary(agy, payload) -> str:
    if not payload:
        return ""
    f30 = agy._find_field(agy._parse_proto_fields(payload), 30)
    if not isinstance(f30, bytes):
        return ""
    return _agy_str(agy._find_field(agy._parse_proto_fields(f30), 5)).strip()


_AGY_KIND = {"Read": KIND_READ, "Write": KIND_WRITE, "Edit": KIND_EDIT, "Bash": KIND_SHELL,
             "Task": KIND_AGENT, "WebSearch": KIND_WEB, "WebFetch": KIND_WEB, "Ask": "ask"}
_AGY_ARG_KEYS = {
    KIND_READ: ("AbsolutePath", "absolute_path", "path", "file_path"),
    KIND_WRITE: ("TargetFile", "target_file", "AbsolutePath", "path", "file_path"),
    KIND_EDIT: ("TargetFile", "target_file", "AbsolutePath", "path", "file_path"),
    KIND_SHELL: ("CommandLine", "command_line", "command", "Command"),
    KIND_WEB: ("query", "Query", "url", "Url"),
    KIND_AGENT: ("TaskName", "task_name", "description", "Description"),
    "ask": ("question", "Question"),
}


def _agy_call(canon: str, raw_name: str, cid: str, args, args_json: str) -> ToolCallBlock:
    n = ts_.normalize_tool("claude", canon)
    kind = _AGY_KIND.get(canon, n.kind)
    server = n.server
    title = ""
    detail = None
    if kind == KIND_MCP:
        sp = ts_.split_mcp_name(canon)
        title = ts_.title_mcp(*sp) if sp else ts_.one_line_title(raw_name)
        detail = ts_.first_string_field(args)
    else:
        v = ts_.str_field(args, _AGY_ARG_KEYS.get(kind, ())) if isinstance(args, dict) else None
        if v:
            title = ts_.one_line_title(v)
        if isinstance(args, dict):
            d = ts_.str_field(args, ("toolAction", "toolSummary"))
            detail = ts_.one_line_title(d) if d else None
    if kind == KIND_SHELL and title:
        kind = ts_.refine_shell_kind(title)
    if not title:
        title = ts_.one_line_title(raw_name)
    p = ts_.preview_chars(args_json or "", INPUT_PREVIEW_CHARS)
    return ToolCallBlock(id=cid, raw_name=raw_name, kind=kind, title=title, detail=detail,
                         server=server, input_preview=p.text, input_total_len=p.total_len)


# --------------------------------------------------------------------------- #
# WorkBuddy / CodeBuddy
# --------------------------------------------------------------------------- #
def _wb_build(path: Path, cancel) -> list[Message]:
    from tcer.core import workbuddy_reader as wb
    msgs: list[Message] = []
    calls: dict[str, ToolCallBlock] = {}
    n = 0
    for n, obj in enumerate(wb.iter_records(path)):
        if n % 512 == 0:
            _check(cancel)
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        ts = parse_timestamp_ms(obj.get("timestamp"))
        if t == "message":
            role = obj.get("role")
            content = obj.get("content") or []
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            else:
                txt = ""
            if role == "user":
                clean = wb.clean_user_text(txt)
                if clean:
                    msgs.append(Message("user", ts, _opt_str(obj.get("id")), None, False, [TextBlock(clean)]))
            elif role == "assistant":
                if txt.strip():
                    msgs.append(Message("assistant", ts, _opt_str(obj.get("id")), None, False,
                                        [TextBlock(txt.strip())]))
        elif t == "reasoning":
            raw_c = obj.get("rawContent") or []
            rtxt = ""
            if isinstance(raw_c, list):
                rtxt = "\n".join(c.get("text", "") for c in raw_c if isinstance(c, dict) and c.get("text"))
            if not rtxt:
                pd = obj.get("providerData")
                if isinstance(pd, dict):
                    rtxt = str(pd.get("reasoning", "") or "")
            if rtxt.strip():
                blk = make_thinking(rtxt.strip(), ref=ContentRef(
                    "jsonl_idx", str(path), index=n, pointer="/rawContent", source="workbuddy"))
                msgs.append(Message("assistant", ts, None, None, False, [blk]))
        elif t == "function_call":
            name = obj.get("name") or "tool"
            args = obj.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            cid = _opt_str(obj.get("callId")) or _opt_str(obj.get("id")) or ""
            call = make_tool_call("generic", cid, str(name), args)
            if cid:
                calls[cid] = call
            msgs.append(Message("assistant", ts, None, None, False, [call]))
        elif t == "function_call_result":
            cid = _opt_str(obj.get("callId")) or _opt_str(obj.get("id")) or ""
            out = obj.get("output")
            if isinstance(out, dict):
                out_txt = out.get("text") or ""
            elif isinstance(out, str):
                out_txt = out
            elif isinstance(out, list):
                out_txt = "\n".join(c.get("text", "") for c in out if isinstance(c, dict))
            else:
                out_txt = ""
            lowered = out_txt.strip().lower()
            is_err = (obj.get("status") != "completed" or lowered.startswith("error:")
                      or lowered.startswith("error during") or lowered.startswith("failed:")
                      or "traceback (most recent call last):" in lowered)
            msgs.append(Message("tool", ts, None, None, False,
                                [make_result(cid, ST_ERROR if is_err else ST_SUCCESS, out_txt)]))
    return msgs


# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #
def build_transcript(src: str, path: Path, session_id: str | None, cancel=None) -> Transcript:
    from tcer.core import reader
    warnings: list[str] = []
    if src == "claude":
        msgs = _Claude(path, cancel).run()
        try:
            meta = reader.read_session_meta(path)
            title, cwd = meta.title, meta.cwd
        except (AttributeError, TypeError):          # 个别行不是对象（损坏）时元信息读不出来，不拖垮阅读器
            title = cwd = None
        sid = session_id or path.stem
        return finalize(src, path, sid, msgs, title=title, cwd=cwd)
    if src == "codex":
        from tcer.core import codex_reader
        b = _Codex(path, cancel)
        msgs = b.run()
        meta = codex_reader.read_session_meta(path)
        sid = session_id or meta.session_id or b.session_id
        return finalize(src, path, sid, msgs, title=meta.title, cwd=meta.cwd or b.cwd)
    if src in ("omp", "pi"):
        from tcer.core import omp_reader
        b = _Omp(src, path, cancel)
        msgs = b.run()
        meta = omp_reader.read_session_meta(path)
        sid = session_id or meta.session_id or path.stem
        return finalize(src, path, sid, msgs, title=b.title or meta.title,
                        cwd=meta.cwd or _opt_str(b.header.get("cwd")))
    if src == "opencode":
        from tcer.core import opencode_reader
        if not session_id:
            raise ValueError("OpenCode 需要 session_id")
        msgs = _OpenCode(path, session_id, cancel).run()
        meta = opencode_reader.read_session_meta(path, session_id)
        return finalize(src, path, session_id, msgs, title=meta.title, cwd=meta.cwd)
    if src == "antigravity":
        from tcer.core import antigravity_reader
        msgs, w = _agy_build(path, cancel)
        meta = antigravity_reader.read_session_meta(path)
        return finalize(src, path, session_id or meta.session_id or path.stem, msgs,
                        title=meta.title, cwd=meta.cwd, warnings=w)
    if src == "workbuddy":
        from tcer.core import workbuddy_reader
        msgs = _wb_build(path, cancel)
        meta = workbuddy_reader.read_session_meta(path)
        return finalize(src, path, session_id or meta.session_id or path.stem, msgs,
                        title=meta.title, cwd=meta.cwd)
    raise ValueError(f"未知数据源：{src}")


# --------------------------------------------------------------------------- #
# 用量头信息（复用各 reader 的聚合与 file_cache，数值与分析页一致）
# --------------------------------------------------------------------------- #
def _claude_usage(path: Path):
    from tcer.core import reader
    sid, main, sdir = reader.session_artifacts(path)
    files = [main] if main.is_file() else [path]
    if sdir.is_dir():
        for f in sorted(sdir.rglob("*.jsonl")):
            if f.name != "journal.jsonl" and reader.is_subagent(f):
                files.append(f)
    return reduce(lambda a, b: a.merge(b),
                  (reader.scan_session(f, with_loc=True, include_user_texts=False)[0] for f in files))


def _usage_of(src: str, path: Path, sid: str | None):
    from tcer.core import analyze
    if src == "claude":
        return _claude_usage(path)
    if src == "codex":
        return analyze._codex_usage_of(None, path)
    if src == "opencode":
        return analyze._opencode_usage_of(SimpleNamespace(path=path), sid or "")
    if src == "omp":
        return analyze._omp_usage_of(None, path)
    if src == "pi":
        return analyze._pi_usage_of(None, path)
    if src == "antigravity":
        return analyze._antigravity_usage_of(None, path)
    if src == "workbuddy":
        return analyze._workbuddy_usage_of(None, path)
    return None


def usage_header(src: str, path: Path, sid: str | None) -> dict:
    """token / 成本头信息字段（失败返回空 dict，宁缺勿编）。"""
    try:
        u = _usage_of(src, path, sid)
        if u is None:
            return {}
        from tcer.core import metrics
        cost = metrics.cost_usd(u)
        rep = getattr(u, "reported_cost_usd", None)
        return dict(
            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens,
            cache_write_tokens=u.cache_creation_input_tokens,
            total_tokens=u.total, cost_usd=cost,
            reported_cost_usd=rep if rep else None)
    except Exception:   # noqa: BLE001  用量只是头信息，失败不应拖垮阅读器
        return {}
