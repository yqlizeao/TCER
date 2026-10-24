"""会话记录阅读器的数据层：统一消息模型 → Turn（提问 → 执行步骤 → 最终回复）→ 行模型。

纯函数、无 Tk 依赖。各数据源的解析 adapter 在 :mod:`tcer.core.transcript_sources`，
本模块负责：

- 数据类：``Message`` / 各 ``Block`` / ``Step`` / ``Turn`` / ``Transcript`` / 行类型；
- ``build_turns``（cc-switch ``buildTurns`` 移植）、``summarize_turn``（见 tool_summary）；
- ``flatten_rows``（视图 all / conversation / changes）、``find_search_hits``、
  ``build_turn_index``、``transcript_to_markdown``；
- ``load_transcript(source, path, session_id)`` 入口与 8 会话 LRU；
- ``read_content``：按 :class:`ContentRef` 懒取被截断的全文（大会话内存友好）。

「用户提问」的唯一判定来源是 :mod:`tcer.core.user_input`（各 adapter 调用，不另写过滤）。
本模块只做展示，**不进指标 / CSV**。

Portions of this file are ported from cc-switch
(https://github.com/farion1231/cc-switch), Copyright (c) 2025 Jason Young,
licensed under the MIT License. See THIRD_PARTY_NOTICES.md.
Sources:
  - src/components/sessions/reader/turns.ts (buildTurns / groupMessages / locateFinal /
    buildTurnIndex / findSearchHits / flattenRows / findTurnRowIndex / findMatchRowIndex)
  - src/components/sessions/reader/exportMarkdown.ts
  - src/components/sessions/reader/SessionDiff.tsx (parseDiff / buildInputDiff)
  - src/components/sessions/reader/SessionOutline.tsx (toSnippet)
  - src-tauri/src/session_manager/providers/blocks.rs (starts_turn / assign_turn_ids)
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Iterable

from tcer.core import tool_summary as ts_
from tcer.core.tool_summary import (  # noqa: F401  (re-export 供 GUI 一处导入)
    AGENT_STYLES, EV_ABORTED, EV_COMPACTION, EV_ERROR, EV_HOOK, EV_INFO, EV_MODEL_CHANGE,
    EV_OTHER, EV_PR_LINK, EV_SLASH, EV_SUB_AGENT, EV_THINKING_LEVEL,
    KIND_AGENT, KIND_EDIT, KIND_SHELL, KIND_WRITE,
    LEGACY_CALL_PREFIX, ST_ERROR, ST_INTERRUPTED, ST_PENDING, ST_SUCCESS, ST_UNKNOWN,
    AgentStyle, TurnSummary, format_duration, format_tokens, get_style, is_failure_status,
    is_failure_step, merge_steps, summarize_turn,
)

# --------------------------------------------------------------------------- #
# 数据类：按需取全文的引用
# --------------------------------------------------------------------------- #


@dataclass
class ContentRef:
    """被截断内容的全文引用（只存「去哪取」，不存正文）。

    kind:
      - ``jsonl``：Claude 等按字节偏移取行（``offset``），再沿 ``pointer`` 取值；
      - ``jsonl_idx``：Codex / omp / Pi 按「第 n 个解析成功的事件」取（``index``，
        ``source`` 指明用哪个 reader 的 ``iter_events``；zst 压缩的 Codex 也适用）；
      - ``sqlite``：OpenCode ``part`` 行（``table`` / ``row_id`` / ``column``）；
      - ``file``：Claude ``tool-results/*.txt`` 之类的附属文件。
    """
    kind: str
    path: str
    offset: int = 0
    index: int = -1
    pointer: str = ""
    source: str = ""
    table: str = ""
    row_id: str = ""
    column: str = "data"


@dataclass
class ImageRef:
    media_type: str = "application/octet-stream"
    size: int = 0                 # 估算字节数
    alt: str | None = None
    source: str = "inline"        # inline（base64 在会话文件里，不保留）/ local_file
    path: str | None = None       # local_file 的路径
    ref: ContentRef | None = None


@dataclass
class MessageMeta:
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in self.__dataclass_fields__)


# --------------------------------------------------------------------------- #
# 块（Block）
# --------------------------------------------------------------------------- #
@dataclass
class TextBlock:
    type: ClassVar[str] = "text"
    text: str
    full: ContentRef | None = None        # 存在时 text 只是预览（>8KB 的注入文本）


@dataclass
class ThinkingBlock:
    type: ClassVar[str] = "thinking"
    text: str = ""                        # 预览 ≤400 字符
    summary: str | None = None
    redacted: bool = False
    duration_ms: int | None = None
    full: ContentRef | None = None


@dataclass
class ToolCallBlock:
    type: ClassVar[str] = "tool_call"
    id: str
    raw_name: str
    kind: str                             # tool_summary.TOOL_KINDS
    title: str
    detail: str | None = None
    server: str | None = None
    input_preview: str = ""               # ≤400 字符
    input_total_len: int = 0
    input_full: ContentRef | None = None
    diff: dict | None = None              # {files:[{path,op,added,removed}], added, removed, full}
    by_user: bool = False                 # 用户 !cmd


@dataclass
class ToolResultBlock:
    type: ClassVar[str] = "tool_result"
    call_id: str
    status: str = ST_SUCCESS
    preview: str = ""                     # 前 12 行且 ≤1200 字符
    total_len: int = 0
    line_count: int = 0
    truncated: bool = False
    full: ContentRef | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    images: list = field(default_factory=list)
    saved_path: str | None = None


@dataclass
class ImageBlock:
    type: ClassVar[str] = "image"
    image: ImageRef = field(default_factory=ImageRef)


@dataclass
class EventBlock:
    type: ClassVar[str] = "event"
    kind: str
    text: str | None = None
    url: str | None = None
    full: ContentRef | None = None


@dataclass
class StepBlock:
    """仅 OpenCode：step-start / step-finish。"""
    type: ClassVar[str] = "step"
    phase: str = "start"                  # start / finish
    tokens: int | None = None
    cost_usd: float | None = None
    reason: str | None = None


Block = (TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock | ImageBlock
         | EventBlock | StepBlock)


@dataclass
class Message:
    role: str                             # user / assistant / tool / system
    ts: int | None = None                 # epoch ms
    id: str | None = None                 # 源记录 id
    turn_id: str | None = None            # Codex 原生；其余 assign_turn_ids 生成 t{n}
    injected: bool = False                # 注入型消息（默认隐藏）
    blocks: list = field(default_factory=list)
    meta: MessageMeta | None = None

    def is_empty(self) -> bool:
        return not self.blocks


# --------------------------------------------------------------------------- #
# 步骤（Step）与 Turn
# --------------------------------------------------------------------------- #
@dataclass
class StepCost:
    tokens: int | None = None
    cost_usd: float | None = None
    reason: str | None = None


@dataclass
class ToolStep:
    kind: ClassVar[str] = "tool"
    id: str
    message_index: int
    call: ToolCallBlock | None = None     # None = 无配对结果（孤儿输出）
    result: ToolResultBlock | None = None
    result_message_index: int | None = None
    status: str = ST_PENDING
    cost: StepCost | None = None


@dataclass
class ThinkingStep:
    kind: ClassVar[str] = "thinking"
    id: str
    message_index: int
    block: ThinkingBlock
    cost: StepCost | None = None


@dataclass
class NoteStep:
    """过程中的助手说明（最终回复之前的 Text）。"""
    kind: ClassVar[str] = "note"
    id: str
    message_index: int
    text: str
    cost: StepCost | None = None


@dataclass
class ImageStep:
    kind: ClassVar[str] = "image"
    id: str
    message_index: int
    image: ImageRef
    cost: StepCost | None = None


@dataclass
class EventStep:
    kind: ClassVar[str] = "event"
    id: str
    message_index: int
    block: EventBlock
    body: str | None = None               # 同一条 system 消息里跟在事件后的正文
    cost: StepCost | None = None


@dataclass
class MergedStep:
    kind: ClassVar[str] = "merged"
    id: str
    merge_kind: str                       # explored / mcp
    children: list
    server: str | None = None
    status: str = ST_SUCCESS
    cost: StepCost | None = None


Step = ToolStep | ThinkingStep | NoteStep | ImageStep | EventStep | MergedStep


@dataclass
class TurnEvent:
    id: str
    message_index: int
    block: EventBlock
    body: str | None = None


@dataclass
class TurnQuestion:
    message_index: int
    text: str
    images: list
    ts: int | None = None


@dataclass
class TurnFinal:
    message_index: int
    text: str
    images: list
    ts: int | None = None
    model: str | None = None
    cost: StepCost | None = None


@dataclass
class Turn:
    key: str                              # turn_id（同 id 再次出现加 #n）
    index: int
    first_message_index: int
    last_message_index: int
    question: TurnQuestion | None = None
    leading_events: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    final: TurnFinal | None = None
    trailing_events: list = field(default_factory=list)
    injected: list = field(default_factory=list)     # 注入消息下标
    step_costs: list = field(default_factory=list)
    aborted: bool = False
    ts: int | None = None
    end_ts: int | None = None
    summary: TurnSummary | None = None


SessionTurn = Turn  # cc-switch 命名别名


# --------------------------------------------------------------------------- #
# 块 / 消息小工具
# --------------------------------------------------------------------------- #
def has_text(block) -> bool:
    return block.type == "text" and bool(block.text.strip())


def is_question_message(m: Message) -> bool:
    """真人提问：非注入的 user 消息，含非空文本或图片。"""
    if m.role != "user" or m.injected:
        return False
    for b in m.blocks:
        t = b.type
        if t == "image" or (t == "text" and b.text.strip()):
            return True
    return False


def starts_turn(m: Message) -> bool:
    """这条消息是否开启新一轮（blocks.rs starts_turn）：非注入 user 消息，且带真人输入——
    非空文本、图片或斜杠命令。只有中断事件 / 用户 !cmd / 工具结果的消息不开新轮。"""
    if m.role != "user" or m.injected:
        return False
    for b in m.blocks:
        t = b.type
        if t == "text" and b.text.strip():
            return True
        if t == "image":
            return True
        if t == "event" and b.kind == EV_SLASH:
            return True
    return False


def assign_turn_ids(messages: list[Message]) -> None:
    """没有原生轮次概念的源共用：按 starts_turn 递增生成 ``t{n}``，首条提问之前归 ``t0``。
    已有 turn_id 的消息保持不变。"""
    turn = 0
    for m in messages:
        if starts_turn(m):
            turn += 1
        if m.turn_id is None:
            m.turn_id = f"t{turn}"


def message_text(m: Message) -> str:
    return "\n\n".join(b.text for b in m.blocks if b.type == "text")


def _join_texts(blocks: Iterable) -> str:
    return "\n\n".join(b.text for b in blocks if b.type == "text" and b.text.strip())


# --------------------------------------------------------------------------- #
# build_turns
# --------------------------------------------------------------------------- #
def _group_messages(messages: list[Message]) -> list[tuple[str, int, int]]:
    groups: list[list] = []
    seen: dict[str, int] = {}
    auto_turn = 0
    last_id = None
    for index, m in enumerate(messages):
        tid = m.turn_id
        if not tid:
            if is_question_message(m):
                auto_turn += 1
            tid = f"auto-{auto_turn}"
        if groups and last_id == tid:
            groups[-1][2] = index
            continue
        last_id = tid
        times = seen.get(tid, 0)
        seen[tid] = times + 1
        groups.append([tid if times == 0 else f"{tid}#{times}", index, index])
    return [(g[0], g[1], g[2]) for g in groups]


def _locate_final(messages, start: int, end: int) -> tuple[int, int] | None:
    """最终回复 = 本轮最后一个 tool_call 之后、最后一条含非空 Text 的 assistant 消息里的 Text 块。
    返回 (消息下标, 起始块下标)；最后一个 tool_call 在这些 Text 之后 → None。"""
    last_call = (-1, -1)
    for i in range(start, end + 1):
        m = messages[i]
        if m.injected:
            continue
        for j, b in enumerate(m.blocks):
            if b.type == "tool_call":
                last_call = (i, j)
    for i in range(end, start - 1, -1):
        m = messages[i]
        if m.role != "assistant" or m.injected:
            continue
        if i < last_call[0]:
            return None
        frm = last_call[1] + 1 if i == last_call[0] else 0
        for b in m.blocks[frm:]:
            if has_text(b):
                return (i, frm)
    return None


def _build_turn(messages: list[Message], key: str, start: int, end: int,
                turn_index: int, style: AgentStyle) -> Turn:
    question_index = -1
    for i in range(start, end + 1):
        if is_question_message(messages[i]):
            question_index = i
            break
    final_at = _locate_final(messages, start, end)

    turn = Turn(key=key, index=turn_index, first_message_index=start, last_message_index=end)
    steps: list = []
    call_steps: dict[str, ToolStep] = {}
    later_calls: set[str] = set()
    for i in range(start, end + 1):
        m = messages[i]
        if m.injected:
            continue
        for b in m.blocks:
            if b.type == "tool_call" and b.id:
                later_calls.add(b.id)
    early_results: dict[str, tuple[ToolResultBlock, int]] = {}
    final_cost: StepCost | None = None
    segment_start = 0
    final_images: list = []
    final_texts: list = []

    for i in range(start, end + 1):
        m = messages[i]
        if m.ts is not None:
            if turn.ts is None:
                turn.ts = m.ts
            turn.end_ts = m.ts
        if m.injected:
            turn.injected.append(i)
            continue
        last_event_in_message: EventStep | None = None
        for j, b in enumerate(m.blocks):
            sid = f"{key}:{i}:{j}"
            in_final = final_at is not None and i == final_at[0] and j >= final_at[1]
            t = b.type
            if t == "text":
                if in_final:
                    final_texts.append(b)
                    continue
                if i == question_index or not b.text.strip():
                    continue
                if m.role == "system" and last_event_in_message is not None:
                    last_event_in_message.body = b.text
                    continue
                steps.append(NoteStep(id=sid, message_index=i, text=b.text))
            elif t == "image":
                if i == question_index:
                    continue
                if in_final:
                    final_images.append(b.image)
                    continue
                steps.append(ImageStep(id=sid, message_index=i, image=b.image))
            elif t == "thinking":
                if not b.text and not b.summary and not b.redacted:
                    continue
                steps.append(ThinkingStep(id=sid, message_index=i, block=b))
            elif t == "tool_call":
                step = ToolStep(id=sid, message_index=i, call=b,
                                status=ST_UNKNOWN if b.id.startswith(LEGACY_CALL_PREFIX)
                                else ST_PENDING)
                if b.id and b.id not in call_steps:
                    call_steps[b.id] = step
                    early = early_results.pop(b.id, None)
                    if early is not None:
                        step.result = early[0]
                        step.result_message_index = early[1]
                        step.status = early[0].status
                steps.append(step)
            elif t == "tool_result":
                owner = call_steps.get(b.call_id) if b.call_id else None
                if owner is not None and owner.result is None:
                    owner.result = b
                    owner.result_message_index = i
                    owner.status = b.status
                    continue
                if owner is None and b.call_id in later_calls and b.call_id not in early_results:
                    early_results[b.call_id] = (b, i)
                    continue
                steps.append(ToolStep(id=sid, message_index=i, result=b,
                                      result_message_index=i, status=b.status))
            elif t == "event":
                if b.kind == EV_ABORTED:
                    turn.aborted = True
                if question_index >= 0:
                    before_question = i <= question_index
                else:
                    before_question = not any(s.kind != "event" for s in steps)
                if before_question:
                    turn.leading_events.append(TurnEvent(id=sid, message_index=i, block=b))
                    continue
                last_event_in_message = EventStep(id=sid, message_index=i, block=b)
                steps.append(last_event_in_message)
            elif t == "step":
                if b.phase == "start":
                    segment_start = len(steps)
                    continue
                cost = StepCost(tokens=b.tokens, cost_usd=b.cost_usd, reason=b.reason)
                turn.step_costs.append(cost)
                if len(steps) > segment_start:
                    steps[-1].cost = cost
                else:
                    final_cost = cost
                segment_start = len(steps)

    # 事件之后再没有别的步骤 → 不算过程中的事件，移到最终回复之后
    last_work = -1
    for idx, s in enumerate(steps):
        if s.kind != "event":
            last_work = idx
    trailing = steps[last_work + 1:]
    del steps[last_work + 1:]
    turn.trailing_events = [TurnEvent(id=s.id, message_index=s.message_index,
                                      block=s.block, body=s.body) for s in trailing]

    if question_index >= 0:
        qm = messages[question_index]
        turn.question = TurnQuestion(
            message_index=question_index,
            text=_join_texts(qm.blocks),
            images=[b.image for b in qm.blocks if b.type == "image"],
            ts=qm.ts)
    if final_at is not None:
        fm = messages[final_at[0]]
        turn.final = TurnFinal(
            message_index=final_at[0],
            text=_join_texts(final_texts),
            images=final_images,
            ts=fm.ts,
            model=fm.meta.model if fm.meta else None,
            cost=final_cost)
    turn.steps = merge_steps(steps, style)
    turn.summary = summarize_turn(turn)
    return turn


def build_turns(messages: list[Message], style: AgentStyle | str | None = None) -> list[Turn]:
    """``Message[]`` → ``Turn[]``（提问 → 执行过程步骤 → 最终回复）。

    分组按 ``turn_id``（Codex 原生 turn_id，其余按真人提问递增 ``t{n}``）；缺 turn_id 时按提问
    自行递增。配对在轮内按 call_id 做：无结果 → pending；无调用的结果 → 「工具输出」步骤。
    ``style`` 决定合并规则（AgentStyle 或数据源名；缺省 generic 不合并）。
    """
    if not isinstance(style, AgentStyle):
        style = get_style(style)
    return [_build_turn(messages, key, s, e, idx, style)
            for idx, (key, s, e) in enumerate(_group_messages(messages))]


# --------------------------------------------------------------------------- #
# 目录（build_turn_index）
# --------------------------------------------------------------------------- #
QUESTION_PREVIEW_CHARS = 80
SNIPPET_CHARS = 160

_WS = re.compile(r"\s+")


def _preview_text(text: str) -> str:
    flat = _WS.sub(" ", text).strip()
    return flat[:QUESTION_PREVIEW_CHARS] + "…" if len(flat) > QUESTION_PREVIEW_CHARS else flat


@dataclass
class TurnIndexEntry:
    turn_id: str                  # Turn.key
    turn_index: int
    first_message_index: int
    last_message_index: int
    question_preview: str         # 80 字符 + …
    ts: int | None
    step_count: int               # = summary.step_count（与摘要行统一口径）
    error_count: int
    has_error: bool               # 任一失败步骤或中断（目录红点）
    has_final_reply: bool
    aborted: bool


def build_turn_index(turns: list[Turn]) -> list[TurnIndexEntry]:
    """目录：只收有真人提问或斜杠命令的轮；纯注入 / 纯事件的轮不进。"""
    out = []
    for turn in turns:
        slash = next((e for e in turn.leading_events if e.block.kind == EV_SLASH), None)
        if turn.question is None and slash is None:
            continue
        s = turn.summary or summarize_turn(turn)
        qtext = (turn.question.text if turn.question and turn.question.text
                 else (slash.block.text if slash else "") or "")
        out.append(TurnIndexEntry(
            turn_id=turn.key, turn_index=turn.index,
            first_message_index=turn.first_message_index,
            last_message_index=turn.last_message_index,
            question_preview=_preview_text(qtext),
            ts=turn.question.ts if turn.question and turn.question.ts is not None else turn.ts,
            step_count=s.step_count, error_count=s.error_count,
            has_error=bool(s.error_count) or turn.aborted,
            has_final_reply=turn.final is not None, aborted=turn.aborted))
    return out


_FENCE = re.compile(r"```[\s\S]*?```")
_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MD = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARK = re.compile(r"[#>*`~|]+")


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """目录摘要：去掉 Markdown 记号，只留可读文字（toSnippet）。"""
    s = _FENCE.sub(" ", text)
    s = _IMG_MD.sub(" ", s)
    s = _LINK_MD.sub(r"\1", s)
    s = _MD_MARK.sub(" ", s)
    return _WS.sub(" ", s).strip()[:limit]


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
def count_matches(text: str, query: str) -> int:
    """不区分大小写的非重叠计数。"""
    needle = query.strip().lower()
    if not needle or not text:
        return 0
    return text.lower().count(needle)


@dataclass
class SearchMatch:
    turn: int                     # turns 下标
    target: str                   # question / step / final / event
    count: int
    target_id: str | None = None  # step：命中的（子）步骤 id；event：事件 id


@dataclass
class SearchHits:
    query: str
    total: int = 0
    matches: list = field(default_factory=list)       # 文档顺序
    turn_keys: set = field(default_factory=set)       # 命中在执行过程里、需展开时间线的轮
    step_ids: set = field(default_factory=set)        # 需展开的步骤（含合并步骤本身）
    question_keys: set = field(default_factory=set)
    final_keys: set = field(default_factory=set)

    def match_at(self, n: int) -> SearchMatch | None:
        """第 n 处命中（0 起、文档顺序）落在哪个 SearchMatch。"""
        for m in self.matches:
            if n < m.count:
                return m
            n -= m.count
        return None


def step_search_text(step) -> str:
    """步骤可被查找的文本：标题、detail、参数预览、输出预览、思考、说明、事件。"""
    k = step.kind
    if k == "tool":
        parts = []
        if step.call:
            parts += [step.call.title, step.call.detail, step.call.input_preview]
        if step.result:
            parts.append(step.result.preview)
        return "\n".join(p for p in parts if p)
    if k == "thinking":
        return "\n".join(p for p in (step.block.summary, step.block.text) if p)
    if k == "note":
        return step.text
    if k == "event":
        return "\n".join(p for p in (step.block.text, step.body) if p)
    return ""


def _event_text(e: TurnEvent) -> str:
    return "\n".join(p for p in (e.block.text, e.body) if p)


def find_search_hits(turns: list[Turn], query: str) -> SearchHits | None:
    """在 turn 结构上查找；空查询返回 None。注入内容不参与（默认不可见）。"""
    if not query.strip():
        return None
    hits = SearchHits(query=query)

    def add(m: SearchMatch):
        hits.total += m.count
        hits.matches.append(m)

    for turn in turns:
        for e in turn.leading_events:
            c = count_matches(_event_text(e), query)
            if c:
                add(SearchMatch(turn.index, "event", c, e.id))
        qc = count_matches(turn.question.text if turn.question else "", query)
        if qc:
            hits.question_keys.add(turn.key)
            add(SearchMatch(turn.index, "question", qc))

        def visit(step, parent=None):
            if step.kind == "merged":
                for ch in step.children:
                    visit(ch, step)
                return
            c = count_matches(step_search_text(step), query)
            if not c:
                return
            hits.turn_keys.add(turn.key)
            hits.step_ids.add(step.id)
            if parent is not None:
                hits.step_ids.add(parent.id)
            add(SearchMatch(turn.index, "step", c, step.id))

        for s in turn.steps:
            visit(s)
        fc = count_matches(turn.final.text if turn.final else "", query)
        if fc:
            hits.final_keys.add(turn.key)
            add(SearchMatch(turn.index, "final", fc))
        for e in turn.trailing_events:
            c = count_matches(_event_text(e), query)
            if c:
                add(SearchMatch(turn.index, "event", c, e.id))
    return hits


# --------------------------------------------------------------------------- #
# 行模型
# --------------------------------------------------------------------------- #
MAX_PINNED_FAILURES = 5
FAILURE_PREVIEW_LINES = 6
STEP_PREVIEW_LINES = 12
VIEW_ALL, VIEW_CONVERSATION, VIEW_CHANGES = "all", "conversation", "changes"
_BIG = 10**9


@dataclass
class QuestionRow:
    kind: ClassVar[str] = "question"
    key: str
    turn: int
    message_index: int
    force_expanded: bool = False          # 查找命中：长提问自动展开


@dataclass
class TimelineRow:
    """折叠摘要行「执行过程 · N 步 · …」。"""
    kind: ClassVar[str] = "timeline"
    key: str
    turn: int
    summary: TurnSummary
    expanded: bool
    hidden_failures: int = 0              # 折叠态下超过 5 条没列出的失败步骤数
    no_final: bool = False
    aborted: bool = False

    @property
    def text(self) -> str:
        return ts_.format_turn_summary(self.summary, hidden_failures=self.hidden_failures,
                                       no_final=self.no_final, aborted=self.aborted)


@dataclass
class StepRow:
    kind: ClassVar[str] = "step"
    key: str
    turn: int
    step: Any
    depth: int = 0                        # 0 顶层；1 合并步骤的子步骤
    expanded: bool = False                # 详情是否展开
    preview_lines: int = STEP_PREVIEW_LINES
    pinned: bool = False                  # 折叠态下因失败常显


@dataclass
class FinalRow:
    kind: ClassVar[str] = "final"
    key: str
    turn: int
    message_index: int
    force_expanded: bool = False


@dataclass
class EventRow:
    kind: ClassVar[str] = "event"
    key: str
    turn: int
    event_id: str
    message_index: int
    block: EventBlock
    body: str | None = None


@dataclass
class InjectedRow:
    kind: ClassVar[str] = "injected"
    key: str
    turn: int
    message_index: int


@dataclass
class DividerRow:
    kind: ClassVar[str] = "turn_divider"
    key: str
    turn: int


def is_change_step(step) -> bool:
    """改动类步骤：edit / write 或带 diff 的调用。"""
    return step.kind == "tool" and step.call is not None and (
        step.call.kind in (KIND_EDIT, KIND_WRITE) or bool(step.call.diff))


def is_timeline_expanded(turn: Turn, expand_all: bool = False, turn_overrides=None,
                         search: SearchHits | None = None) -> bool:
    """手动切换优先；否则「展开全部」、中断、无最终回复、查找命中在步骤内时自动展开。"""
    if turn_overrides:
        ov = turn_overrides.get(turn.key)
        if ov is not None:
            return bool(ov)
    return bool(expand_all or turn.aborted or turn.final is None
                or (search is not None and turn.key in search.turn_keys))


def _preview_tail_has_query(step, visible: int, query: str) -> bool:
    if step.kind != "tool" or not step.result or not step.result.preview or not query:
        return False
    tail = "\n".join(re.split(r"\r?\n", step.result.preview)[visible:])
    return query.lower() in tail.lower()


def _step_rows(turn: Turn, step, depth: int, pinned: bool, step_overrides, search) -> list:
    override = step_overrides.get(step.id) if step_overrides else None
    failure = is_failure_step(step)
    search_hit = bool(search and step.id in search.step_ids)
    # 折叠态钉出的失败步骤只露标题行（失败信号不丢，输出不刷屏）；展开执行过程后
    # 失败步骤才默认展开 6 行预览。
    expanded = override if override is not None else ((failure and not pinned) or search_hit)
    base = FAILURE_PREVIEW_LINES if (failure and override is None) else STEP_PREVIEW_LINES
    preview_lines = (_BIG if (search_hit and search
                              and _preview_tail_has_query(step, base, search.query)) else base)
    rows = [StepRow(key=f"{turn.key}:step:{step.id}", turn=turn.index, step=step, depth=depth,
                    expanded=bool(expanded), preview_lines=preview_lines, pinned=pinned)]
    if step.kind == "merged" and expanded:
        for ch in step.children:
            rows.extend(_step_rows(turn, ch, 1, False, step_overrides, search))
    return rows


def _event_row(turn: Turn, e: TurnEvent) -> EventRow:
    return EventRow(key=f"{turn.key}:event:{e.id}", turn=turn.index, event_id=e.id,
                    message_index=e.message_index, block=e.block, body=e.body)


def _timeline_rows(turn: Turn, expand_all, turn_overrides, step_overrides, search) -> list:
    steps = turn.steps
    if not steps:
        return []
    if len(steps) == 1 and not is_failure_step(steps[0]):   # 1 步且没失败：无摘要行
        return _step_rows(turn, steps[0], 0, False, step_overrides, search)
    expanded = is_timeline_expanded(turn, expand_all, turn_overrides, search)
    failures = [s for s in steps if is_failure_step(s)]
    pinned = [] if expanded else failures[:MAX_PINNED_FAILURES]
    rows: list = [TimelineRow(
        key=f"{turn.key}:timeline", turn=turn.index,
        summary=turn.summary or summarize_turn(turn), expanded=expanded,
        hidden_failures=0 if expanded else len(failures) - len(pinned),
        no_final=turn.final is None, aborted=turn.aborted)]
    for s in (steps if expanded else pinned):
        rows.extend(_step_rows(turn, s, 0, not expanded, step_overrides, search))
    return rows


def _turn_rows(turn: Turn, view, expand_all, turn_overrides, step_overrides, search,
               show_injected) -> list:
    def q_row():
        return QuestionRow(key=f"{turn.key}:question", turn=turn.index,
                           message_index=turn.question.message_index,
                           force_expanded=bool(search and turn.key in search.question_keys))

    def f_row():
        return FinalRow(key=f"{turn.key}:final", turn=turn.index,
                        message_index=turn.final.message_index,
                        force_expanded=bool(search and turn.key in search.final_keys))

    rows: list = []
    if view == VIEW_CONVERSATION:
        if turn.question:
            rows.append(q_row())
        if turn.final:
            rows.append(f_row())
        return rows
    if view == VIEW_CHANGES:
        changes = [s for s in turn.steps if is_change_step(s)]
        if not changes:
            return rows
        if turn.question:
            rows.append(q_row())
        for s in changes:
            rows.extend(_step_rows(turn, s, 0, False, step_overrides, search))
        return rows
    rows.extend(_event_row(turn, e) for e in turn.leading_events)
    if show_injected:
        for mi in turn.injected:
            rows.append(InjectedRow(key=f"{turn.key}:injected:{mi}", turn=turn.index,
                                    message_index=mi))
    if turn.question:
        rows.append(q_row())
    rows.extend(_timeline_rows(turn, expand_all, turn_overrides, step_overrides, search))
    if turn.final:
        rows.append(f_row())
    rows.extend(_event_row(turn, e) for e in turn.trailing_events)
    return rows


def flatten_rows(turns: list[Turn], view: str = VIEW_ALL, expanded: bool = False, *,
                 turn_overrides: dict | None = None, step_overrides: dict | None = None,
                 search: SearchHits | None = None, show_injected: bool = False) -> list:
    """Turn → 虚拟列表行；空轮不出行，相邻两轮之间插 ``DividerRow``。

    ``view``：all / conversation（只提问 + 最终回复）/ changes（提问 + 改动步骤，无改动的轮整轮隐藏）。
    ``expanded``：「展开全部过程」偏好。``turn_overrides`` / ``step_overrides``：用户手动
    切换（key/id → bool）。"""
    rows: list = []
    for turn in turns:
        own = _turn_rows(turn, view, expanded, turn_overrides, step_overrides, search,
                         show_injected)
        if not own:
            continue
        if rows:
            rows.append(DividerRow(key=f"{turn.key}:divider", turn=turn.index))
        rows.extend(own)
    return rows


def find_turn_row_index(rows: list, turns: list[Turn], turn_key: str) -> int:
    """目录 / j k 跳转：该轮第一行（不含分轮线）下标，没有 -1。"""
    turn = next((t for t in turns if t.key == turn_key), None)
    if turn is None:
        return -1
    for i, r in enumerate(rows):
        if r.turn == turn.index and r.kind != "turn_divider":
            return i
    return -1


def find_match_row_index(rows: list, match: SearchMatch) -> int:
    """查找命中 → 行下标；目标行被过滤时退回该轮第一行，整轮都不在 -1。"""
    for i, r in enumerate(rows):
        if r.turn != match.turn:
            continue
        k = r.kind
        if match.target == "question" and k == "question":
            return i
        if match.target == "final" and k == "final":
            return i
        if match.target == "step" and k == "step" and r.step.id == match.target_id:
            return i
        if match.target == "event" and (
                (k == "event" and r.event_id == match.target_id)
                or (k == "step" and r.step.id == match.target_id)):
            return i
    for i, r in enumerate(rows):
        if r.turn == match.turn and r.kind != "turn_divider":
            return i
    return -1


# --------------------------------------------------------------------------- #
# diff 解析 / 参数还原
# --------------------------------------------------------------------------- #
@dataclass
class DiffLine:
    type: str                      # file / gap / add / del / context
    text: str = ""
    path: str = ""
    old_no: int | None = None
    new_no: int | None = None


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")


def parse_diff(text: str) -> list[DiffLine]:
    """解析 unified diff（git diff、Codex FileChange）与 Codex apply_patch 文本。"""
    lines: list[DiffLine] = []
    old_no = new_no = None
    hunks = 0
    all_ = text.replace("\r\n", "\n").split("\n")

    def push_file(path: str):
        nonlocal hunks
        if lines and lines[-1].type == "file" and lines[-1].path == path:
            return
        lines.append(DiffLine("file", path=path))
        hunks = 0

    for index, raw in enumerate(all_):
        nxt = all_[index + 1] if index + 1 < len(all_) else ""
        prv = all_[index - 1] if index > 0 else ""
        file_old = raw.startswith("--- ") and nxt.startswith("+++ ")
        file_new = raw.startswith("+++ ") and prv.startswith("--- ")
        if raw.startswith("diff --git "):
            hunks = 0
            continue
        if ((raw.startswith("index ") and hunks == 0) or file_old
                or raw.startswith("*** Begin Patch") or raw.startswith("*** End Patch")
                or raw.startswith("\\ No newline")):
            continue
        if file_new:
            p = raw[4:]
            if p.startswith("b/"):
                p = p[2:]
            p = p.strip()
            if p and p != "/dev/null":
                push_file(p)
            continue
        pf = _PATCH_FILE.match(raw)
        if pf:
            push_file(pf.group(1).strip())
            old_no = new_no = None
            continue
        if raw.startswith("*** Move to: "):
            push_file(raw[len("*** Move to: "):].strip())
            continue
        if raw.startswith("@@"):
            if hunks > 0:
                lines.append(DiffLine("gap"))
            hunks += 1
            m = _HUNK.match(raw)
            old_no = int(m.group(1)) if m else None
            new_no = int(m.group(2)) if m else None
            continue
        if raw.startswith("+"):
            lines.append(DiffLine("add", raw[1:], new_no=new_no))
            if new_no is not None:
                new_no += 1
            continue
        if raw.startswith("-"):
            lines.append(DiffLine("del", raw[1:], old_no=old_no))
            if old_no is not None:
                old_no += 1
            continue
        if raw == "" and not lines:
            continue
        lines.append(DiffLine("context", raw[1:] if raw.startswith(" ") else raw,
                              old_no=old_no, new_no=new_no))
        if old_no is not None:
            old_no += 1
        if new_no is not None:
            new_no += 1
    while lines and lines[-1].type == "context" and lines[-1].text == "":
        lines.pop()
    return lines


def _pick(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return None


def _split_lines(text: str) -> list[str]:
    if text == "":
        return []
    t = text.replace("\r\n", "\n")
    if t.endswith("\n"):
        t = t[:-1]
    return t.split("\n")


def build_input_diff(call: ToolCallBlock, load_full: bool = True) -> str | None:
    """没有 unified diff 全文时，从调用参数还原改动（伪 hunk）。参数预览被截断时
    ``load_full=True`` 会经 ``input_full`` 懒取完整参数（cc-switch 此处直接放弃）。"""
    if call.kind not in (KIND_EDIT, KIND_WRITE):
        return None
    inp = None
    if len(call.input_preview) >= call.input_total_len:
        try:
            inp = json.loads(call.input_preview)
        except ValueError:
            inp = None
    elif load_full and call.input_full is not None:
        raw = read_content(call.input_full)
        if raw:
            try:
                inp = json.loads(raw)
            except ValueError:
                inp = None
    if not isinstance(inp, dict):
        return None
    pairs: list[tuple[str, str]] = []
    edits = inp["edits"] if isinstance(inp.get("edits"), list) else [inp]
    for e in edits:
        if not isinstance(e, dict):
            continue
        o = _pick(e, ("old_string", "oldString", "oldText", "old_str"))
        n = _pick(e, ("new_string", "newString", "newText", "new_str"))
        if o is not None or n is not None:
            pairs.append((o or "", n or ""))
    if not pairs:
        content = _pick(inp, ("content", "file_text", "text"))
        if content is None:
            return None
        pairs.append(("", content))
    chunks = []
    for o, n in pairs:
        body = [f"-{ln}" for ln in _split_lines(o)] + [f"+{ln}" for ln in _split_lines(n)]
        chunks.append("@@\n" + "\n".join(body))
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# 按需取全文
# --------------------------------------------------------------------------- #
FULL_TEXT_CAP = 32 * 1024 * 1024


def _pointer_get(obj: Any, pointer: str) -> Any:
    if not pointer:
        return obj
    cur = obj
    for tok in pointer.lstrip("/").split("/"):
        tok = tok.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                cur = cur[int(tok)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if tok not in cur:
                return None
            cur = cur[tok]
        else:
            return None
    return cur


def _value_to_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for it in v:
            if isinstance(it, dict) and isinstance(it.get("text"), str):
                parts.append(it["text"])
            elif isinstance(it, str):
                parts.append(it)
        if parts:
            return "\n".join(parts)
    return json.dumps(v, ensure_ascii=False, indent=2)


def _iter_events_for(source: str, path: str):
    if source == "codex":
        from tcer.core import codex_reader
        return codex_reader.iter_events(Path(path))
    from tcer.core import omp_reader
    return omp_reader.iter_events(Path(path))


def read_content(ref: ContentRef | None, limit: int = FULL_TEXT_CAP) -> str | None:
    """按引用取被截断内容的全文（调用方在 worker 线程里用；失败返回 None）。"""
    if ref is None:
        return None
    try:
        if ref.kind == "file":
            with open(ref.path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(limit)
        if ref.kind == "jsonl":
            with open(ref.path, "rb") as fh:
                fh.seek(ref.offset)
                obj = json.loads(fh.readline().decode("utf-8", errors="replace"))
            txt = _value_to_text(_pointer_get(obj, ref.pointer))
        elif ref.kind == "jsonl_idx":
            obj = None
            for n, ev in enumerate(_iter_events_for(ref.source, ref.path)):
                if n == ref.index:
                    obj = ev
                    break
            txt = _value_to_text(_pointer_get(obj, ref.pointer)) if obj is not None else None
        elif ref.kind == "sqlite":
            if ref.table not in ("part", "session_message", "message") or ref.column != "data":
                return None
            from tcer.core import opencode_reader
            con = opencode_reader._connect(Path(ref.path))
            try:
                row = con.execute(f"select data from {ref.table} where id = ?",
                                  (ref.row_id,)).fetchone()
            finally:
                con.close()
            if row is None:
                return None
            obj = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else row[0]
            txt = _value_to_text(_pointer_get(obj, ref.pointer))
        else:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if txt is not None and len(txt) > limit:
        txt = txt[:limit]
    return txt


# --------------------------------------------------------------------------- #
# Transcript 与 load_transcript
# --------------------------------------------------------------------------- #
@dataclass
class Transcript:
    source: str
    path: str
    session_id: str | None = None
    title: str | None = None
    cwd: str | None = None
    started_ts: int | None = None
    ended_ts: int | None = None
    models: list = field(default_factory=list)       # 出现过的模型（顺序、去重、不含 <synthetic>）
    model: str | None = None                          # 最近一条带 model 的助手消息
    question_count: int = 0                           # = user_input.read_user_messages 的条数（同口径）
    tool_call_count: int = 0                          # 去重后的 ToolCallBlock 总数（不合并）
    input_tokens: int | None = None                   # 来自 reader 聚合（含子代理），不是逐消息相加
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None                     # 价表估算（标价）
    reported_cost_usd: float | None = None            # 数据源自报成本（如有）
    resume_command: str | None = None
    style: str = "generic"
    messages: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def agent_style(self) -> AgentStyle:
        return get_style(self.style)

    def index(self) -> list[TurnIndexEntry]:
        return build_turn_index(self.turns)


# ---- 恢复命令 -------------------------------------------------------------- #
def _shell_quote_posix(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def resume_command(source: str, session_id: str | None, path: str | Path | None = None) -> str | None:
    """各源恢复命令；拿不准返回 None（不编）。"""
    src = (source or "").lower()
    sid = session_id or ""
    if src == "claude":
        return f"claude --resume {sid}" if sid else None
    if src == "codex":
        return f"codex resume {sid}" if sid else None
    if src == "opencode":
        return f"opencode -s {sid}" if sid else None
    if src == "pi":
        return f"pi --session {_shell_quote_posix(str(path))}" if path else None
    if src == "omp":
        # oh-my-pi docs/cli-reference.md：`--resume [id]` 按会话 id 前缀或路径恢复
        return f"omp --resume {sid}" if sid else None
    if src == "antigravity":
        return f"agy --conversation={sid}" if sid else None
    return None   # workbuddy / 未知：无


def cd_resume_command(cwd: str | None, command: str | None, shell: str = "posix") -> str | None:
    """「进入目录并恢复」：posix ``cd '<dir>' && cmd``；cmd ``cd /d "<dir>" && cmd``；
    powershell ``Set-Location -LiteralPath '<dir>'; cmd``。"""
    if not cwd or not command:
        return None
    if shell == "cmd":
        return f'cd /d "{cwd}" && {command}'
    if shell == "powershell":
        return f"Set-Location -LiteralPath '{cwd.replace(chr(39), chr(39) * 2)}'; {command}"
    return f"cd {_shell_quote_posix(cwd)} && {command}"


# ---- LRU 缓存（≤8 个会话；仅缓存消息 / turns，用量每次从 file_cache 取） ---------- #
_CACHE_MAX = 8
_CACHE: "OrderedDict[tuple, Transcript]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _sig_of(p: Path) -> tuple:
    try:
        st = p.stat()
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (0, 0)


def _cache_key(source: str, path: Path, session_id: str | None) -> tuple:
    sigs = [_sig_of(path)]
    if path.suffix == ".db":      # SQLite：WAL 里可能有尚未 checkpoint 的写入
        sigs.append(_sig_of(path.with_name(path.name + "-wal")))
    return (source, os.path.normcase(os.path.abspath(path)), session_id or "", tuple(sigs))


def load_transcript(source: str, path: str | Path, session_id: str | None = None, *,
                    with_usage: bool = True, use_cache: bool = True,
                    cancel_event: threading.Event | None = None) -> Transcript:
    """读取一个会话的完整记录。

    ``source`` ∈ claude / codex / opencode / omp / pi / antigravity / workbuddy（codebuddy 同
    workbuddy）。``path``：JSONL 文件；OpenCode / Antigravity 为 SQLite 路径（OpenCode 另需
    ``session_id``）。**只读主会话**：子代理 / 子会话不并入，只以 agent 类步骤出现。

    ``with_usage``：用 reader 既有聚合（file_cache 命中时近零成本）填 token / 成本头信息；
    False 则这些字段为 None。返回对象可能是缓存共享引用，调用方勿原地修改。
    ``cancel_event``：置位时 adapter 在行边界抛 ``TranscriptCancelled``。
    """
    from tcer.core import transcript_sources as srcs

    src = (source or "claude").lower()
    if src == "codebuddy":
        src = "workbuddy"
    p = Path(path)
    key = _cache_key(src, p, session_id)
    base = None
    if use_cache:
        with _CACHE_LOCK:
            base = _CACHE.get(key)
            if base is not None:
                _CACHE.move_to_end(key)
    if base is None:
        base = srcs.build_transcript(src, p, session_id, cancel_event)
        if use_cache:
            with _CACHE_LOCK:
                _CACHE[key] = base
                _CACHE.move_to_end(key)
                while len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
    if not with_usage:
        return base
    usage = srcs.usage_header(src, p, base.session_id or session_id)
    return replace(base, **usage) if usage else base


class TranscriptCancelled(Exception):
    """cancel_event 置位。"""


# --------------------------------------------------------------------------- #
# Markdown 导出
# --------------------------------------------------------------------------- #
def format_message_time(ts: int | None, now: datetime | None = None) -> str:
    """月日 + 时分；非今年带年。"""
    if ts is None:
        return ""
    try:
        d = datetime.fromtimestamp(ts / 1000)
    except (OverflowError, OSError, ValueError):
        return ""
    now = now or datetime.now()
    return d.strftime("%m-%d %H:%M") if d.year == now.year else d.strftime("%Y-%m-%d %H:%M")


def _indent_body(text: str, prefix: str = "  ") -> str:
    lines = text.strip().split("\n")
    return "\n".join(ln if i == 0 else prefix + ln for i, ln in enumerate(lines))


def _step_md_lines(step, style: AgentStyle, project_dir, include_thinking: bool,
                   depth: int = 0) -> list[str]:
    pad = "  " * depth
    k = step.kind
    if k == "tool":
        title = ts_.format_step_title(step, style, project_dir)
        meta = ts_.format_step_meta(step, style)
        mt = ts_.format_step_meta_text(meta)
        return [f"{pad}- {meta.glyph} {title.text}{' — ' + mt if mt else ''}"]
    if k == "merged":
        title = ts_.format_merged_title(step)
        meta = ts_.format_step_meta(step, style)
        mt = ts_.format_step_meta_text(meta)
        out = [f"{pad}- {meta.glyph} {title.text}{' — ' + mt if mt else ''}"]
        for ch in step.children:
            out.extend(_step_md_lines(ch, style, project_dir, include_thinking, depth + 1))
        return out
    if k == "thinking":
        if not include_thinking:
            return []
        body = step.block.text or step.block.summary or ""
        glyph = f"{style.thinking_glyph} " if style.thinking_glyph else ""
        tail = f": {_indent_body(body, pad + '  ')}" if body else ""
        return [f"{pad}- {glyph}{style.thinking_label}{tail}"]
    if k == "note":
        return [f"{pad}- {_indent_body(step.text, pad + '  ')}"]
    if k == "image":
        return [f"{pad}- [{step.image.alt or ts_.t('image.alt', index=1)}]"]
    if k == "event":
        d = ts_.describe_event(step.block, step.body)
        return [f"{pad}- {d.label}{': ' + _indent_body(d.detail) if d.detail else ''}"]
    return []


def _event_md_line(e: TurnEvent) -> str:
    d = ts_.describe_event(e.block, e.body)
    url = e.block.url if e.block.kind == EV_PR_LINK and e.block.url else ""
    tail = f"\n>\n> {d.detail.replace(chr(10), chr(10) + '> ')}" if d.detail else ""
    return f"> {d.label}{' (' + url + ')' if url else ''}{tail}"


def turn_to_markdown(turn: Turn, style: AgentStyle | str | None = None, *,
                     app_name: str = "助手", you_label: str = "你",
                     project_dir: str | None = None, include_thinking: bool = False) -> str:
    """单轮 Markdown：事件 → 提问 → 步骤列表 → 最终回复 → 尾部事件。"""
    if not isinstance(style, AgentStyle):
        style = get_style(style)
    sections: list[str] = [_event_md_line(e) for e in turn.leading_events]

    def heading(label: str, ts: int | None) -> str:
        tm = format_message_time(ts)
        return f"**{label}**{' ' + tm if tm else ''}"

    if turn.question:
        imgs = " ".join(f"[{im.alt or ts_.t('image.alt', index=i + 1)}]"
                        for i, im in enumerate(turn.question.images))
        sections.append("\n\n".join(p for p in (heading(you_label, turn.question.ts),
                                                turn.question.text, imgs) if p))
    steps = [ln for s in turn.steps
             for ln in _step_md_lines(s, style, project_dir, include_thinking)]
    if steps:
        sections.append("\n".join(steps))
    if turn.final:
        sections.append(f"{heading(app_name, turn.final.ts)}\n\n{turn.final.text}")
    sections.extend(_event_md_line(e) for e in turn.trailing_events)
    return "\n\n".join(sections)


_APP_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "omp": "omp",
              "pi": "Pi", "antigravity": "Antigravity", "workbuddy": "WorkBuddy"}


def transcript_to_markdown(t: Transcript, *, include_thinking: bool = False,
                           you_label: str = "你", app_name: str | None = None,
                           project_dir: str | None = None) -> str:
    """整段会话 → Markdown：``# {标题}`` + 各轮以 ``---`` 分隔。"""
    style = t.agent_style
    name = app_name or _APP_NAMES.get(t.source, "助手")
    pdir = project_dir if project_dir is not None else t.cwd
    body = "\n\n---\n\n".join(
        s for s in (turn_to_markdown(turn, style, app_name=name, you_label=you_label,
                                     project_dir=pdir, include_thinking=include_thinking)
                    for turn in t.turns) if s)
    title = t.title or Path(t.path).stem
    return f"# {title}\n\n{body}\n"


_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def export_filename(title: str | None, fallback: str = "session", ext: str = ".md") -> str:
    """导出文件名：标题去 ``\\/:*?"<>|`` 与控制字符、压空白、限 60 字符。"""
    s = _WS.sub(" ", _BAD_FILENAME.sub("", title or "")).strip()[:60].strip()
    return (s or fallback) + ext


# --------------------------------------------------------------------------- #
# 页头信息小工具
# --------------------------------------------------------------------------- #
def format_time_range(started: int | None, ended: int | None) -> str:
    """``{起} → {止} (时长)``；同日止只显示时钟。"""
    if started is None:
        return ""
    s = format_message_time(started)
    if ended is None or ended == started:
        return s
    try:
        a, b = datetime.fromtimestamp(started / 1000), datetime.fromtimestamp(ended / 1000)
    except (OverflowError, OSError, ValueError):
        return s
    e = b.strftime("%H:%M") if a.date() == b.date() else format_message_time(ended)
    return f"{s} → {e} ({format_duration(max(0, ended - started))})"


# --------------------------------------------------------------------------- #
# 单轮遥测与改动文件汇总（纯函数，仅作阅读器展示，不进指标 / CSV）
# --------------------------------------------------------------------------- #
@dataclass
class TurnTelemetry:
    turn_index: int
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    cost_share: float = 0.0
    token_share: float = 0.0
    added: int = 0
    removed: int = 0
    files_changed: int = 0
    atomic_steps: int = 0
    has_code_change: bool = False
    is_hotspot: bool = False
    has_context_shift: bool = False
    is_deep_turn: bool = False
    is_severe_abort: bool = False


@dataclass
class FileChangeEntry:
    path: str
    display_path: str
    added: int = 0
    removed: int = 0
    edit_count: int = 0
    ops: set[str] = field(default_factory=set)
    first_turn: int = 0
    first_step_id: str = ""
    turn_indices: list[int] = field(default_factory=list)


def _walk_atomic_steps(steps: Iterable):
    for st in steps:
        if st.kind == "merged":
            yield from _walk_atomic_steps(st.children)
        else:
            yield st


def compute_turn_telemetries(tr: Transcript) -> list[TurnTelemetry]:
    """逐轮汇总 Token / 成本 / 代码增删与多色热点语义标签（拿不准的字段留空，绝不编造）。"""
    from tcer.core import pricing

    msgs = tr.messages
    n_msgs = len(msgs)
    out: list[TurnTelemetry] = []
    prev_model: str | None = None

    for turn in tr.turns:
        inp = out_t = cr = cw = 0
        cost_sum = 0.0
        has_cost = False
        turn_model = turn.final.model if (turn.final and turn.final.model) else None

        lo = max(0, turn.first_message_index)
        hi = min(n_msgs - 1, turn.last_message_index)
        for mi in range(lo, hi + 1):
            m = msgs[mi]
            if m.injected or m.role != "assistant" or m.meta is None:
                continue
            mm = m.meta
            if mm.model:
                turn_model = mm.model
            mi_i = mm.input_tokens or 0
            mi_o = mm.output_tokens or 0
            mi_cr = mm.cache_read_tokens or 0
            mi_cw = mm.cache_write_tokens or 0
            inp += mi_i
            out_t += mi_o
            cr += mi_cr
            cw += mi_cw
            if mm.cost_usd is not None and mm.cost_usd > 0:
                cost_sum += float(mm.cost_usd)
                has_cost = True
            elif mi_i + mi_o + mi_cr + mi_cw > 0:
                rates = pricing.resolve(mm.model or turn_model or tr.model or "")
                cost_sum += (
                    mi_i * rates["input"]
                    + mi_cw * rates["cache_write"]
                    + mi_cr * rates["cache_read"]
                    + mi_o * rates["output"]
                ) / 1_000_000
                has_cost = True

        total_tok = inp + out_t + cr + cw
        if total_tok == 0 and turn.step_costs:
            sc_tok = sum(sc.tokens or 0 for sc in turn.step_costs)
            sc_cost = sum(float(sc.cost_usd) for sc in turn.step_costs
                          if sc.cost_usd is not None and sc.cost_usd > 0)
            total_tok = sc_tok
            if not has_cost and sc_cost > 0:
                cost_sum = sc_cost
                has_cost = True

        added = removed = 0
        files_set: set[str] = set()
        atomic_n = 0
        ctx_shift = False
        severe_abort = bool(turn.aborted)

        for ev in (*turn.leading_events, *turn.trailing_events):
            ek = ev.block.kind
            if ek in (EV_COMPACTION, EV_MODEL_CHANGE, EV_SUB_AGENT):
                ctx_shift = True
            elif ek in (EV_ABORTED, EV_ERROR):
                severe_abort = True

        for st in _walk_atomic_steps(turn.steps):
            atomic_n += 1
            if st.kind == "event":
                ek = st.block.kind
                if ek in (EV_COMPACTION, EV_MODEL_CHANGE, EV_SUB_AGENT):
                    ctx_shift = True
                elif ek in (EV_ABORTED, EV_ERROR):
                    severe_abort = True
            elif st.kind == "tool" and st.call is not None:
                if st.call.kind == KIND_AGENT:
                    ctx_shift = True
                if not is_failure_step(st) and st.call.diff:
                    d = st.call.diff
                    added += int(d.get("added") or 0)
                    removed += int(d.get("removed") or 0)
                    for f in d.get("files") or ():
                        if isinstance(f, dict) and f.get("path"):
                            files_set.add(os.path.normcase(str(f["path"]).replace("\\", "/")))

        if prev_model and turn_model and turn_model != prev_model:
            ctx_shift = True
        if turn_model:
            prev_model = turn_model
        has_chg = (added + removed > 0) or bool(files_set)
        out.append(TurnTelemetry(
            turn_index=turn.index,
            model=turn_model,
            input_tokens=inp,
            output_tokens=out_t,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
            total_tokens=total_tok,
            cost_usd=cost_sum if has_cost else None,
            added=added,
            removed=removed,
            files_changed=len(files_set),
            atomic_steps=atomic_n,
            has_code_change=has_chg,
            has_context_shift=ctx_shift,
            is_deep_turn=atomic_n >= 15,
            is_severe_abort=severe_abort,
        ))

    active_turns = sum(1 for tl in out if tl.total_tokens > 0 or (tl.cost_usd or 0.0) > 0)
    sum_cost = sum(tl.cost_usd or 0.0 for tl in out)
    sum_tok = sum(tl.total_tokens for tl in out)
    for tl in out:
        if sum_cost > 0 and tl.cost_usd:
            tl.cost_share = tl.cost_usd / sum_cost
        if sum_tok > 0 and tl.total_tokens:
            tl.token_share = tl.total_tokens / sum_tok
        if active_turns >= 2:
            if sum_cost >= 0.02:
                tl.is_hotspot = tl.cost_share >= 0.25 and (tl.cost_usd or 0.0) >= 0.01
            elif sum_tok >= 10_000:
                tl.is_hotspot = tl.token_share >= 0.25 and tl.total_tokens >= 4_000
    return out


def summarize_file_changes(tr: Transcript) -> list[FileChangeEntry]:
    """汇总会话内所有成功（非失败）改动步骤触及的文件、增删行数与编辑频次。"""
    entries: OrderedDict[str, FileChangeEntry] = OrderedDict()
    for turn in tr.turns:
        for st in _walk_atomic_steps(turn.steps):
            if st.kind != "tool" or st.call is None or is_failure_step(st) or not is_change_step(st):
                continue
            call = st.call
            diff = call.diff or {}
            raw_files = diff.get("files") or []
            if not raw_files and call.title:
                raw_files = [{
                    "path": call.title,
                    "op": "add" if call.kind == KIND_WRITE else "update",
                    "added": int(diff.get("added") or 0),
                    "removed": int(diff.get("removed") or 0),
                }]
            for f in raw_files:
                if not isinstance(f, dict):
                    continue
                raw_p = str(f.get("path") or "").strip()
                if not raw_p:
                    continue
                norm_k = os.path.normcase(raw_p.replace("\\", "/"))
                ent = entries.get(norm_k)
                if ent is None:
                    ent = FileChangeEntry(
                        path=raw_p,
                        display_path=ts_.shorten_path(raw_p, tr.cwd),
                        first_turn=turn.index,
                        first_step_id=st.id,
                    )
                    entries[norm_k] = ent
                ent.added += int(f.get("added") or 0)
                ent.removed += int(f.get("removed") or 0)
                ent.edit_count += 1
                op = str(f.get("op") or "update")
                ent.ops.add(op)
                if not ent.turn_indices or ent.turn_indices[-1] != turn.index:
                    ent.turn_indices.append(turn.index)
    return list(entries.values())


def extract_diff_new_code(lines: list[DiffLine]) -> str:
    """从解析后的 DiffLine 序列中提取改动后的新代码（保留 add 与 context 行，剔除 del / 行号）。"""
    if not any(ln.type == "add" for ln in lines):
        return ""
    return "\n".join(ln.text for ln in lines if ln.type in ("add", "context"))
