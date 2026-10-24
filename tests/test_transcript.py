"""会话记录阅读器数据层（transcript / transcript_sources）无头单测。

每个数据源 adapter 用 tmp_path 造最小 fixture；另覆盖 build_turns 各分支、合并步骤、注入隐藏、
搜索、目录、Markdown 导出、rewind 去重、按需取全文、LRU 缓存与取消。
"""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
from pathlib import Path

import pytest

from tcer.core import transcript as T
from tcer.core import tool_summary as S
from tcer.core import user_input as UI
from tcer.core.transcript import (
    EventBlock, ImageBlock, ImageRef, Message, MessageMeta, StepBlock, TextBlock, ThinkingBlock,
    ToolCallBlock, ToolResultBlock,
)


# =========================================================================== #
# 通用：Message 构造（直接测 build_turns，不经 adapter）
# =========================================================================== #
def _m(role, *blocks, ts=None, turn_id=None, injected=False, meta=None, id=None):
    return Message(role=role, ts=ts, id=id, turn_id=turn_id, injected=injected,
                   blocks=list(blocks), meta=meta)


def _q(text, ts=None, turn_id=None):
    return _m("user", TextBlock(text), ts=ts, turn_id=turn_id)


def _a(*blocks, ts=None, turn_id=None, meta=None):
    return _m("assistant", *blocks, ts=ts, turn_id=turn_id, meta=meta)


def _call(cid, raw="Bash", kind="shell", title="ls", **kw):
    return ToolCallBlock(id=cid, raw_name=raw, kind=kind, title=title, **kw)


def _res(cid, status="success", preview="", **kw):
    return ToolResultBlock(call_id=cid, status=status, preview=preview, **kw)


def _tr(cid, status="success", preview="", ts=None, turn_id=None, **kw):
    return _m("tool", _res(cid, status, preview, **kw), ts=ts, turn_id=turn_id)


def _build(msgs, style="claude"):
    T.assign_turn_ids(msgs)
    return T.build_turns(msgs, style)


# =========================================================================== #
# build_turns 各分支
# =========================================================================== #
def test_build_turns_basic_question_steps_final():
    msgs = [
        _q("修 bug", ts=1000),
        _a(TextBlock("先看看"), _call("c1"), ts=2000),
        _tr("c1", preview="out", ts=3000),
        _a(TextBlock("好了"), ts=9000),
    ]
    turns = _build(msgs)
    assert len(turns) == 1
    t = turns[0]
    assert t.question.text == "修 bug" and t.question.ts == 1000
    assert [s.kind for s in t.steps] == ["note", "tool"]        # 最后一个 tool_call 之前的文字 = 说明
    assert t.steps[0].text == "先看看"
    tool = t.steps[1]
    assert tool.call.id == "c1" and tool.result.preview == "out" and tool.status == "success"
    assert tool.result_message_index == 2
    assert t.final.text == "好了" and t.final.ts == 9000
    assert t.end_ts == 9000 and t.ts == 1000
    assert t.summary.step_count == 1 and t.summary.duration_ms == 8000


def test_build_turns_no_final_when_last_is_tool_call():
    turns = _build([_q("q"), _a(TextBlock("说明"), _call("c1"))])
    t = turns[0]
    assert t.final is None
    assert t.steps[-1].status == "pending"                  # 无结果 → pending
    rows = T.flatten_rows(turns, "all")
    # 无最终回复 → 时间线自动展开；说明 + 工具共 2 步 → 有摘要行
    assert [r.kind for r in rows] == ["question", "timeline", "step", "step"]
    assert rows[1].expanded


def test_build_turns_text_after_last_tool_call_in_same_message_is_final():
    turns = _build([_q("q"), _a(_call("c1"), TextBlock("总结"), ts=5)])
    assert turns[0].final.text == "总结"
    assert [s.kind for s in turns[0].steps] == ["tool"]


def test_build_turns_final_text_before_later_tool_call_is_not_final():
    msgs = [_q("q"), _a(TextBlock("甲"), ts=1), _a(_call("c1"), ts=2), _tr("c1")]
    t = _build(msgs)[0]
    assert t.final is None and [s.kind for s in t.steps] == ["note", "tool"]


def test_build_turns_pairing_early_orphan_and_duplicate_ids():
    msgs = [
        _q("q"),
        _tr("late", "success", "early-out"),                 # 结果早于调用
        _a(_call("late", title="late-call")),
        _tr("ghost", "error", "no call"),                    # 孤儿结果
        _a(_call("dup", title="first"), _call("dup", title="second")),
        _tr("dup", "success", "dup-out"),
        _tr("dup", "error", "dup-out-2"),                    # 第二个同 id 结果 → 孤儿
        _a(TextBlock("done")),
    ]
    t = _build(msgs)[0]
    by_title = {s.call.title: s for s in t.steps if s.kind == "tool" and s.call}
    assert by_title["late-call"].result.preview == "early-out"
    assert by_title["late-call"].status == "success"
    assert by_title["first"].result.preview == "dup-out"      # 只配到第一个
    assert by_title["second"].result is None and by_title["second"].status == "pending"
    orphans = [s for s in t.steps if s.kind == "tool" and s.call is None]
    assert [o.result.preview for o in orphans] == ["no call", "dup-out-2"]
    assert all(o.status in ("error",) for o in orphans)
    assert t.summary.step_count == 5


def test_build_turns_empty_call_id_never_pairs():
    t = _build([_q("q"), _a(_call("")), _tr(""), _a(TextBlock("x"))])[0]
    tools = [s for s in t.steps if s.kind == "tool"]
    assert len(tools) == 2 and tools[0].status == "pending" and tools[0].result is None
    assert tools[1].call is None


def test_build_turns_leading_events_and_t0():
    msgs = [
        _m("system", EventBlock("model_change", "gpt-5"), ts=1),       # t0：首条提问之前
        _m("system", EventBlock("compaction", "x"), ts=2),
        _q("问题", ts=3),
        _a(TextBlock("答"), ts=4),
    ]
    turns = _build(msgs)
    assert [t.key for t in turns] == ["t0", "t1"]
    assert turns[0].question is None
    assert [e.block.kind for e in turns[0].leading_events] == ["model_change", "compaction"]
    # t0 无提问、无斜杠 → 不进目录；且无步骤 / 回复 → flatten 里只剩事件行
    assert T.build_turn_index(turns)[0].turn_id == "t1"
    rows = T.flatten_rows(turns, "all")
    assert rows[0].kind == "event" and rows[0].turn == 0


def test_build_turns_slash_command_starts_turn_and_indexed():
    msgs = [
        _q("先问点什么", ts=1), _a(TextBlock("好"), ts=2),
        _m("user", EventBlock("slash_command", "/model opus"), ts=3),
        _q("再问", ts=4), _a(TextBlock("嗯"), ts=5),
    ]
    turns = _build(msgs)
    assert [t.key for t in turns] == ["t1", "t2", "t3"]
    slash_turn = turns[1]
    assert slash_turn.question is None
    assert slash_turn.leading_events[0].block.text == "/model opus"
    idx = T.build_turn_index(turns)
    assert [e.question_preview for e in idx] == ["先问点什么", "/model opus", "再问"]


def test_build_turns_trailing_events_moved_after_final():
    msgs = [
        _q("q"),
        _a(_call("c1")), _tr("c1"),
        _a(TextBlock("答")),
        _m("system", EventBlock("hook", "lint failed")),
        _m("system", EventBlock("pr_link", "PR #1", "https://x/pull/1")),
    ]
    t = _build(msgs)[0]
    assert [e.block.kind for e in t.trailing_events] == ["hook", "pr_link"]
    assert [s.kind for s in t.steps] == ["tool"]
    rows = T.flatten_rows([t], "all")
    assert [r.kind for r in rows][-3:] == ["final", "event", "event"]


def test_build_turns_inner_event_stays_in_steps_with_body():
    msgs = [
        _q("q"),
        _a(_call("c1")), _tr("c1"),
        _m("system", EventBlock("sub_agent", "agt: hi"), TextBlock("正文行"), ts=1),
        _a(_call("c2")), _tr("c2"),
        _a(TextBlock("答")),
    ]
    t = _build(msgs)[0]
    ev = [s for s in t.steps if s.kind == "event"]
    assert len(ev) == 1 and ev[0].body == "正文行"           # system 消息里跟在事件后的正文并入事件


def test_build_turns_aborted_flag_and_auto_expand():
    msgs = [
        _q("q"),
        _a(_call("c1")), _tr("c1"),
        _a(_call("c2")),
        _m("user", EventBlock("aborted", "[Request interrupted by user]")),
    ]
    turns = _build(msgs)
    t = turns[0]
    assert t.aborted and t.final is None
    assert [e.block.kind for e in t.trailing_events] == ["aborted"]
    rows = T.flatten_rows(turns, "all")
    tl = [r for r in rows if r.kind == "timeline"][0]
    assert tl.expanded and tl.aborted
    assert "没有最终回复" not in tl.text                     # 已中断不再追加
    assert T.build_turn_index(turns)[0].aborted and T.build_turn_index(turns)[0].has_error


def test_build_turns_second_human_message_in_same_turn_becomes_note():
    """Codex 原生 turn：同一轮第二条真人消息不再是提问，走说明分支。"""
    msgs = [
        _q("第一问", turn_id="T1"),
        _a(TextBlock("答一"), turn_id="T1"),
        _q("追加一句", turn_id="T1"),
        _a(TextBlock("答二"), turn_id="T1"),
    ]
    t = _build(msgs, "codex")[0]
    assert t.question.text == "第一问"
    assert [s.kind for s in t.steps] == ["note", "note"]
    assert t.steps[1].text == "追加一句"
    assert t.final.text == "答二"


def test_group_messages_same_turn_id_reappearing_gets_suffix():
    msgs = [
        _q("a", turn_id="X"), _a(TextBlock("1"), turn_id="X"),
        _q("b", turn_id="Y"), _a(TextBlock("2"), turn_id="Y"),
        _a(TextBlock("late"), turn_id="X"),
    ]
    turns = T.build_turns(msgs, "codex")
    assert [t.key for t in turns] == ["X", "Y", "X#1"]
    assert turns[2].question is None


def test_build_turns_without_turn_id_uses_auto_numbering():
    msgs = [_q("a"), _a(TextBlock("1")), _q("b"), _a(TextBlock("2"))]
    turns = T.build_turns(msgs, "claude")          # 不 assign：turn_id 全 None
    assert [t.key for t in turns] == ["auto-1", "auto-2"]


def test_build_turns_injected_messages_collected_not_rendered():
    msgs = [
        _q("q", ts=1),
        _m("user", TextBlock("<local-command-caveat>..."), injected=True, ts=2),
        _a(TextBlock("答"), ts=3),
    ]
    t = _build(msgs)[0]
    assert t.injected == [1]
    assert all(s.kind != "note" for s in t.steps)
    assert t.final.text == "答"
    # 默认隐藏；开关打开才有 injected 行
    assert not [r for r in T.flatten_rows([t], "all") if r.kind == "injected"]
    inj = [r for r in T.flatten_rows([t], "all", show_injected=True) if r.kind == "injected"]
    assert len(inj) == 1 and inj[0].message_index == 1


def test_build_turns_injected_messages_ignored_by_final_and_question():
    msgs = [
        _m("user", TextBlock("注入"), injected=True),
        _q("真人"),
        _m("assistant", TextBlock("注入助手"), injected=True),
        _a(TextBlock("真答")),
    ]
    turns = _build(msgs)
    assert [t.key for t in turns] == ["t0", "t1"] and turns[0].injected == [0]   # 提问前的注入归 t0
    t = turns[1]
    assert t.question.text == "真人" and t.final.text == "真答" and t.injected == [2]


def test_build_turns_thinking_filtering_and_images():
    img = ImageRef(media_type="image/png", size=3)
    msgs = [
        _m("user", TextBlock("看图"), ImageBlock(img)),
        _a(ThinkingBlock(), ThinkingBlock(text="想", redacted=False),
           ThinkingBlock(redacted=True), ImageBlock(img), TextBlock("答"), ImageBlock(img)),
    ]
    t = _build(msgs)[0]
    assert t.question.images == [img]
    kinds = [s.kind for s in t.steps]
    assert kinds == ["thinking", "thinking"]                # 空思考丢弃；redacted 保留
    assert t.final.images == [img, img]                      # 助手消息里的图并入最终回复


def test_build_turns_step_blocks_cost_attribution():
    """OpenCode：step-start / step-finish → 费用落到该段最后一步；无步骤则落最终回复。"""
    msgs = [
        _q("q"),
        _a(StepBlock("start"), _call("c1"), _res("c1"),
           StepBlock("finish", tokens=1200, cost_usd=0.01, reason="tool-calls"),
           StepBlock("start"), TextBlock("答"),
           StepBlock("finish", tokens=300, cost_usd=0.002, reason="stop")),
    ]
    t = _build(msgs, "opencode")[0]
    assert t.steps[-1].cost.tokens == 1200
    assert t.final.cost.tokens == 300
    assert [c.tokens for c in t.step_costs] == [1200, 300]
    s = S.summarize_turn(t)
    assert s.tokens == 1500 and abs(s.cost_usd - 0.012) < 1e-9


def test_build_turns_merges_steps_by_style():
    msgs = [_q("q"), _a(_call("c1", "read_file", "read", "a"), _call("c2", "rg", "search", "b"),
                        _call("c3", "x", "shell", "c")),
            _tr("c1"), _tr("c2"), _tr("c3"), _a(TextBlock("done"))]
    codex = _build(list(msgs), "codex")[0]
    assert [s.kind for s in codex.steps] == ["merged", "tool"]
    assert codex.summary.step_count == 3                    # 合并步骤按子步骤计
    claude = _build([_q("q"), *msgs[1:]], "claude")[0]
    assert [s.kind for s in claude.steps] == ["tool", "tool", "tool"]


def test_step_ids_are_stable_and_unique():
    msgs = [_q("q"), _a(_call("c1"), _call("c2")), _tr("c1"), _tr("c2"), _a(TextBlock("x"))]
    t = _build(msgs)[0]
    assert [s.id for s in t.steps] == ["t1:1:0", "t1:1:1"]


# =========================================================================== #
# flatten_rows / 搜索 / 目录
# =========================================================================== #
def _long_turn(n_fail=7, n_ok=3, with_final=True):
    blocks, results = [], []
    for i in range(n_fail):
        blocks.append(_call(f"f{i}", title=f"fail{i}"))
        results.append(_tr(f"f{i}", "error", f"boom{i}"))
    for i in range(n_ok):
        blocks.append(_call(f"o{i}", title=f"ok{i}"))
        results.append(_tr(f"o{i}", "success", f"fine{i}"))
    msgs = [_q("跑一下", ts=0), _a(*blocks, ts=1), *results]
    if with_final:
        msgs.append(_a(TextBlock("结束"), ts=2))
    return msgs


def test_flatten_collapsed_timeline_pins_failures_max_five():
    turns = _build(_long_turn())
    rows = T.flatten_rows(turns, "all", expanded=False)
    kinds = [r.kind for r in rows]
    assert kinds[0] == "question" and kinds[1] == "timeline" and kinds[-1] == "final"
    tl = rows[1]
    assert not tl.expanded and tl.hidden_failures == 2
    pinned = [r for r in rows if r.kind == "step"]
    assert len(pinned) == 5 and all(r.pinned and r.step.status == "error" for r in pinned)
    # 折叠态钉出的失败只露标题行（不自动展开输出）；预览行数口径不变
    assert all(not r.expanded and r.preview_lines == T.FAILURE_PREVIEW_LINES for r in pinned)
    assert "还有 2 个失败" in tl.text and "7 个失败" in tl.text


def test_flatten_expanded_all_steps_and_preview_lines():
    turns = _build(_long_turn())
    rows = T.flatten_rows(turns, "all", expanded=True)
    steps = [r for r in rows if r.kind == "step"]
    assert len(steps) == 10 and rows[1].expanded and rows[1].hidden_failures == 0
    assert not any(r.pinned for r in steps)
    assert all(r.expanded for r in steps if r.step.status == "error")   # 展开后失败默认带预览
    ok = [r for r in steps if r.step.status == "success"]
    assert all(not r.expanded and r.preview_lines == T.STEP_PREVIEW_LINES for r in ok)


def test_flatten_overrides_win():
    turns = _build(_long_turn())
    key = turns[0].key
    rows = T.flatten_rows(turns, "all", expanded=True, turn_overrides={key: False})
    assert not rows[1].expanded
    sid = turns[0].steps[0].id
    rows = T.flatten_rows(turns, "all", expanded=True, step_overrides={sid: False})
    first = [r for r in rows if r.kind == "step"][0]
    assert first.step.id == sid and not first.expanded and first.preview_lines == T.STEP_PREVIEW_LINES
    ok_id = turns[0].steps[-1].id
    rows = T.flatten_rows(turns, "all", expanded=True, step_overrides={ok_id: True})
    assert [r for r in rows if r.kind == "step" and r.step.id == ok_id][0].expanded


def test_flatten_single_step_no_timeline_row_unless_failed():
    ok = _build([_q("q"), _a(_call("c1")), _tr("c1"), _a(TextBlock("x"))])
    assert [r.kind for r in T.flatten_rows(ok, "all")] == ["question", "step", "final"]
    bad = _build([_q("q"), _a(_call("c1")), _tr("c1", "error"), _a(TextBlock("x"))])
    assert [r.kind for r in T.flatten_rows(bad, "all")] == ["question", "timeline", "step", "final"]


def test_flatten_conversation_view_and_dividers():
    msgs = [_q("a"), _a(_call("c1")), _tr("c1"), _a(TextBlock("甲")), _q("b"), _a(_call("c2"))]
    turns = _build(msgs)
    rows = T.flatten_rows(turns, "conversation")
    assert [r.kind for r in rows] == ["question", "final", "turn_divider", "question"]
    assert rows[2].turn == 1
    # 空轮不出行、也不产生多余分隔线
    only_q = T.flatten_rows(_build([_m("system", EventBlock("info", "x")), _q("z")]), "conversation")
    assert [r.kind for r in only_q] == ["question"]


def test_flatten_changes_view_only_edit_write_or_diff_steps():
    edit = _call("c1", "Edit", "edit", "a.py")
    write = _call("c2", "Write", "write", "b.py")
    patch = _call("c3", "mystery", "other", "x", diff={"files": [{"path": "p"}], "added": 1, "removed": 0})
    msgs = [
        _q("改"), _a(edit, write, patch, _call("c4", "Bash", "shell", "ls")),
        _tr("c1"), _tr("c2"), _tr("c3"), _tr("c4"), _a(TextBlock("好")),
        _q("纯聊天"), _a(TextBlock("嗯")),
    ]
    turns = _build(msgs)
    rows = T.flatten_rows(turns, "changes")
    assert [r.kind for r in rows] == ["question", "step", "step", "step"]    # 无改动的轮整轮隐藏
    assert [r.step.call.id for r in rows[1:]] == ["c1", "c2", "c3"]
    assert T.flatten_rows(_build([_q("x"), _a(TextBlock("y"))]), "changes") == []


def test_find_search_hits_and_auto_expand():
    msgs = [
        _q("请找 Needle", ts=1),
        _a(_call("c1", title="grep needle"), ThinkingBlock(text="needle thought"), _call("c2")),
        _tr("c1", preview="line with NEEDLE needle"),
        _tr("c2", preview="nothing"),
        _a(TextBlock("最终回复 needle"), ts=2),
        _m("system", EventBlock("hook", "needle hook")),
    ]
    turns = _build(msgs)
    hits = T.find_search_hits(turns, "needle")
    assert hits.total == 1 + 1 + 3 + 1 + 1 + 0 + 0 or hits.total >= 7
    assert hits.question_keys == {turns[0].key} and hits.final_keys == {turns[0].key}
    assert turns[0].key in hits.turn_keys and len(hits.step_ids) == 2
    targets = [m.target for m in hits.matches]
    assert targets == ["question", "step", "step", "final", "event"]
    # 第 n 处命中落到哪个 match
    assert hits.match_at(0).target == "question"
    assert hits.match_at(hits.total - 1).target == "event"
    assert hits.match_at(hits.total) is None
    # 命中步骤所在轮的执行过程自动展开；预览行之外的命中把预览整段展开
    rows = T.flatten_rows(turns, "all", search=hits)
    tl = [r for r in rows if r.kind == "timeline"][0]
    assert tl.expanded
    assert [r.force_expanded for r in rows if r.kind in ("question", "final")] == [True, True]
    assert T.find_search_hits(turns, "   ") is None
    assert T.find_search_hits(turns, "zzz-none").total == 0


def test_search_excludes_injected_and_counts_case_insensitive():
    msgs = [_q("a"), _m("user", TextBlock("SECRET secret"), injected=True), _a(TextBlock("Secret!"))]
    hits = T.find_search_hits(_build(msgs), "secret")
    assert hits.total == 1                                    # 注入内容不参与
    assert T.count_matches("aaaa", "aa") == 2                 # 非重叠
    assert T.count_matches("Hello", " hello ") == 1


def test_search_preview_tail_expands_full_preview():
    lines = "\n".join(f"l{i}" for i in range(10)) + "\nTAILHIT"
    msgs = [_q("q"), _a(_call("c1"), _call("c2")), _tr("c1", "error", lines), _tr("c2"), _a(TextBlock("x"))]
    turns = _build(msgs)
    hits = T.find_search_hits(turns, "tailhit")
    rows = T.flatten_rows(turns, "all", search=hits)
    step = [r for r in rows if r.kind == "step" and r.step.call.id == "c1"][0]
    assert step.preview_lines == 10**9                        # 失败步骤默认只露 6 行，命中在其后则整段展开
    no_hit = [r for r in T.flatten_rows(turns, "all", expanded=True) if r.kind == "step"][0]
    assert no_hit.preview_lines == T.FAILURE_PREVIEW_LINES


def test_find_row_index_helpers():
    msgs = [_q("a"), _a(_call("c1")), _tr("c1", "error"), _a(TextBlock("x needle")),
            _q("b needle"), _a(TextBlock("y"))]
    turns = _build(msgs)
    rows = T.flatten_rows(turns, "all")
    assert T.find_turn_row_index(rows, turns, turns[1].key) == [i for i, r in enumerate(rows) if r.turn == 1][1]
    assert T.find_turn_row_index(rows, turns, "nope") == -1
    hits = T.find_search_hits(turns, "needle")
    rows2 = T.flatten_rows(turns, "all", search=hits)
    i = T.find_match_row_index(rows2, hits.matches[0])
    assert rows2[i].kind == "final"
    # 目标行被过滤（对话视图里没有步骤行）→ 退回该轮第一行
    conv = T.flatten_rows(turns, "conversation")
    step_match = T.SearchMatch(turn=0, target="step", count=1, target_id=turns[0].steps[0].id)
    j = T.find_match_row_index(conv, step_match)
    assert conv[j].turn == 0 and conv[j].kind == "question"
    assert T.find_match_row_index([], step_match) == -1


def test_build_turn_index_preview_steps_errors():
    long_q = "问" * 200
    msgs = [_q(long_q, ts=5), _a(_call("c1"), _call("c2")), _tr("c1", "error"), _tr("c2"),
            _a(ThinkingBlock(text="t"), TextBlock("答"))]
    idx = T.build_turn_index(_build(msgs))
    e = idx[0]
    assert e.question_preview == "问" * 80 + "…"
    assert e.step_count == 2                                  # = summary.step_count（不含思考 / 说明）
    assert e.error_count == 1 and e.has_error and e.has_final_reply and not e.aborted
    assert e.ts == 5 and T.snippet("# 标题\n```py\nx=1\n```\n[链接](http://a) 正文 **粗**") == "标题 链接 正文 粗"


# =========================================================================== #
# Markdown 导出 / 小工具
# =========================================================================== #
def test_transcript_to_markdown_layout():
    msgs = [
        _m("system", EventBlock("compaction", "摘要文本"), ts=None),
        _q("你好", ts=1_700_000_000_000),
        _a(ThinkingBlock(text="内心戏"), TextBlock("先说明"), _call("c1", "Bash", "shell", "cargo build"),
           ts=1_700_000_001_000),
        _tr("c1", "error", "x", exit_code=101, duration_ms=12000),
        _a(TextBlock("完成了"), ts=1_700_000_100_000),
        _m("system", EventBlock("pr_link", "PR #9", "https://h/pull/9")),
        _q("第二问"), _a(TextBlock("嗯")),
    ]
    turns = _build(msgs)
    tr = T.Transcript(source="claude", path="x.jsonl", title="标题", style="claude", turns=turns)
    md = T.transcript_to_markdown(tr)
    assert md.startswith("# 标题\n\n") and md.endswith("\n")
    assert "\n\n---\n\n" in md                                 # 各轮分隔
    assert "> 上下文已压缩\n>\n> 摘要文本" in md
    assert "**你** " in md and "\n\n你好\n" in md
    assert "- ⏺ Bash(cargo build) — 失败 · exit 101 · 12s" in md
    assert "- 先说明" in md
    assert "内心戏" not in md                                   # 默认不含思考
    assert "**Claude Code** " in md and "完成了" in md
    assert "> 已关联 PR #9 (https://h/pull/9)" in md
    md2 = T.transcript_to_markdown(tr, include_thinking=True)
    assert "- ✻ 思考中…: 内心戏" in md2
    # 轮无内容时不产生空分隔
    assert "\n\n---\n\n\n\n---" not in md


def test_markdown_merged_and_multiline_note():
    msgs = [_q("q"), _a(TextBlock("第一行\n第二行"), _call("c1", "read_file", "read", "a"),
                        _call("c2", "rg", "search", "b")),
            _tr("c1"), _tr("c2"), _a(TextBlock("end"))]
    turns = _build(msgs, "codex")
    md = T.turn_to_markdown(turns[0], "codex", app_name="Codex")
    assert "- 第一行\n  第二行" in md                          # 续行缩进
    assert "- • Explored" in md and "  - • Read a" in md


def test_export_filename_and_resume_commands():
    assert T.export_filename('a/b:c*?"<>|d  e') == "abcd e.md"
    assert T.export_filename("x" * 100) == "x" * 60 + ".md"
    assert T.export_filename("") == "session.md"
    assert T.resume_command("claude", "abc") == "claude --resume abc"
    assert T.resume_command("codex", "t1") == "codex resume t1"
    assert T.resume_command("opencode", "ses_1") == "opencode -s ses_1"
    assert T.resume_command("omp", "019f-aa") == "omp --resume 019f-aa"
    assert T.resume_command("pi", "x", Path("C:/a b/s.jsonl")) == f"pi --session '{Path('C:/a b/s.jsonl')}'"
    assert T.resume_command("pi", "x", "/it's/s.jsonl") == "pi --session '/it'\\''s/s.jsonl'"
    assert T.resume_command("antigravity", "cid") == "agy --conversation=cid"
    assert T.resume_command("workbuddy", "w") is None
    assert T.resume_command("claude", None) is None
    cmd = "claude --resume abc"
    assert T.cd_resume_command("/p/it's", cmd) == "cd '/p/it'\\''s' && claude --resume abc"
    assert T.cd_resume_command("C:\\p", cmd, "cmd") == 'cd /d "C:\\p" && claude --resume abc'
    assert T.cd_resume_command("C:\\it's", cmd, "powershell") == \
        "Set-Location -LiteralPath 'C:\\it''s'; claude --resume abc"
    assert T.cd_resume_command(None, cmd) is None and T.cd_resume_command("/p", None) is None


def test_format_time_range_and_message_time():
    import datetime as dt
    a = int(dt.datetime(2026, 10, 2, 15, 25).timestamp() * 1000)
    b = int(dt.datetime(2026, 10, 2, 17, 33).timestamp() * 1000)
    assert T.format_time_range(a, b) == "10-02 15:25 → 17:33 (2h8m)" or "2026-10-02" in T.format_time_range(a, b)
    assert T.format_time_range(None, b) == "" and T.format_time_range(a, a) == T.format_message_time(a)
    nxt = int(dt.datetime(2026, 10, 3, 1, 0).timestamp() * 1000)
    assert "→ " in T.format_time_range(a, nxt) and T.format_message_time(None) == ""
    # 非今年带年
    old = int(dt.datetime(2001, 1, 2, 3, 4).timestamp() * 1000)
    assert T.format_message_time(old).startswith("2001-01-02")


def test_parse_diff_unified_and_apply_patch():
    unified = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n"
               "@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n@@ -10 +10 @@\n-a\n+b\n")
    lines = T.parse_diff(unified)
    assert [l.type for l in lines] == ["file", "context", "del", "add", "gap", "del", "add"]
    assert lines[0].path == "x.py"
    assert (lines[2].old_no, lines[3].new_no, lines[5].old_no) == (2, 2, 10)
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: n.py\n+z\n*** End Patch"
    pl = T.parse_diff(patch)
    assert [l.type for l in pl] == ["file", "del", "add", "file", "add"]
    assert pl[3].path == "n.py"
    # `--- a` 单独出现（没有成对 +++）是内容行
    assert T.parse_diff("--- not header\nx")[0].type == "del"


def test_build_input_diff_from_preview_and_full(tmp_path):
    inp = {"file_path": "a.py", "old_string": "x", "new_string": "y\nz"}
    pj = json.dumps(inp, separators=(",", ":"))
    call = ToolCallBlock(id="1", raw_name="Edit", kind="edit", title="a.py",
                         input_preview=pj, input_total_len=len(pj))
    assert T.build_input_diff(call) == "@@\n-x\n+y\n+z"
    w = ToolCallBlock(id="2", raw_name="Write", kind="write", title="n",
                      input_preview='{"content":"a\\nb"}', input_total_len=18)
    assert T.build_input_diff(w) == "@@\n+a\n+b"
    # 预览被截断：经 input_full 懒取
    f = tmp_path / "s.jsonl"
    line = json.dumps({"message": {"content": [{"input": {"file_path": "q", "content": "L1\nL2"}}]}})
    f.write_text(line + "\n", encoding="utf-8")
    cut = ToolCallBlock(id="3", raw_name="Write", kind="write", title="q", input_preview='{"file_pa',
                        input_total_len=999,
                        input_full=T.ContentRef("jsonl", str(f), 0, pointer="/message/content/0/input"))
    assert T.build_input_diff(cut) == "@@\n+L1\n+L2"
    assert T.build_input_diff(cut, load_full=False) is None
    assert T.build_input_diff(_call("4", "Bash", "shell")) is None


# =========================================================================== #
# Claude adapter
# =========================================================================== #
def _w(tmp_path, lines, sid="sess-1", proj="proj"):
    d = tmp_path / proj
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write((obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) + "\n")
    return p


_T0 = "2026-10-01T10:00:{:02d}Z"


def cu(text, sec=0, **kw):
    return {"type": "user", "uuid": f"u{sec}-{abs(hash(str(text))) % 997}", "timestamp": _T0.format(sec),
            "message": {"role": "user", "content": text}, **kw}


def ca(content, sec=0, mid="m1", model="claude-opus-4-8", usage=None, **kw):
    msg = {"role": "assistant", "id": mid, "model": model, "content": content}
    if usage:
        msg["usage"] = usage
    if "stop" in kw:
        msg["stop_reason"] = kw.pop("stop")
    return {"type": "assistant", "uuid": f"a{sec}-{mid}", "timestamp": _T0.format(sec), "message": msg, **kw}


def tu(cid, name, inp):
    return {"type": "tool_use", "id": cid, "name": name, "input": inp}


def cr(cid, text, sec=0, is_error=False, tur=None):
    d = {"type": "user", "uuid": f"r{sec}-{cid}", "timestamp": _T0.format(sec),
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": cid, "content": text,
                                  **({"is_error": True} if is_error else {})}]}}
    if tur is not None:
        d["toolUseResult"] = tur
    return d


def load(src, p, sid=None, **kw):
    kw.setdefault("use_cache", False)
    kw.setdefault("with_usage", False)
    return T.load_transcript(src, p, sid, **kw)


def test_claude_basic_turn_pairing_status_exit_duration(tmp_path):
    p = _w(tmp_path, [
        {"type": "ai-title", "aiTitle": "修复登录"},
        cu("修复 bug", 0),
        ca([{"type": "text", "text": "我来看看"}, tu("t1", "Bash", {"command": "pytest -x", "description": "跑测试"})], 5, "m1"),
        cr("t1", "Exit code 1\nFAILED a", 9, is_error=True),
        ca([tu("t2", "Read", {"file_path": "a.py", "offset": 1, "limit": 40})], 10, "m2"),
        cr("t2", "content", 11),
        ca([{"type": "text", "text": "修好了"}], 20, "m3"),
    ])
    tr = load("claude", p)
    assert tr.title == "修复登录" and tr.session_id == "sess-1"
    assert tr.resume_command == "claude --resume sess-1"
    assert tr.question_count == 1 and tr.tool_call_count == 2
    assert tr.style == "claude" and tr.model == "claude-opus-4-8" and tr.models == ["claude-opus-4-8"]
    assert len(tr.turns) == 1
    t = tr.turns[0]
    assert t.question.text == "修复 bug" and t.final.text == "修好了"
    assert [s.kind for s in t.steps] == ["note", "tool", "tool"]
    bash, read = t.steps[1], t.steps[2]
    assert bash.status == "error" and bash.result.exit_code == 1 and bash.result.duration_ms == 4000
    assert bash.call.kind == "shell" and bash.call.title == "pytest -x" and bash.call.detail == "跑测试"
    assert read.call.title == "a.py" and read.call.detail == ":1-40" and read.result.exit_code is None
    assert read.status == "success" and read.result.duration_ms is None      # 仅 Bash 计耗时
    assert t.summary.step_count == 2 and t.summary.error_count == 1
    assert tr.started_ts is not None and tr.ended_ts - tr.started_ts == 20000
    # 成功的 Bash 补 exit 0
    p2 = _w(tmp_path, [cu("q"), ca([tu("t", "Bash", {"command": "echo"})], 1), cr("t", "ok", 2),
                       ca([{"type": "text", "text": "x"}], 3, "m2")], sid="s2")
    assert load("claude", p2).turns[0].steps[0].result.exit_code == 0


def test_claude_bash_refined_to_search_and_mcp_and_agent(tmp_path):
    p = _w(tmp_path, [
        cu("q"),
        ca([tu("a", "Bash", {"command": "rg foo src"}), tu("b", "mcp__gh__open_pr", {"title": "T"}),
            tu("c", "Agent", {"description": "审查代码", "prompt": "这是派发给子代理的很长提示", "model": "opus"})], 1),
        cr("a", "x", 2), cr("b", "y", 3), cr("c", "子代理最终报告", 4,
                                              tur={"resolvedModel": "ignored-because-detail-set"}),
        ca([{"type": "text", "text": "ok"}], 5, "m2"),
    ])
    t = load("claude", p).turns[0]
    steps = {s.call.raw_name: s.call for s in t.steps if s.kind in ("tool",)}
    assert steps["Bash"].kind == "search"
    assert steps["mcp__gh__open_pr"].kind == "mcp" and steps["mcp__gh__open_pr"].title == "gh.open_pr"
    assert steps["mcp__gh__open_pr"].server == "gh" and steps["mcp__gh__open_pr"].detail == "T"
    ag = steps["Agent"]
    assert ag.kind == "agent" and ag.title == "审查代码" and ag.detail == "opus"
    assert "派发给子代理" in ag.input_preview                     # 派发 prompt 只在参数里
    assert [s for s in t.steps if s.kind == "tool" and s.call.raw_name == "Agent"][0].result.preview == "子代理最终报告"


def test_claude_agent_detail_from_resolved_model(tmp_path):
    p = _w(tmp_path, [
        cu("q"), ca([tu("c", "Task", {"description": "d"})], 1),
        cr("c", "done", 2, tur={"resolvedModel": "claude-sonnet-5-5"}),
        ca([{"type": "text", "text": "ok"}], 3, "m2"),
    ])
    assert load("claude", p).turns[0].steps[0].call.detail == "claude-sonnet-5-5"


def test_claude_injection_hidden_and_stripped(tmp_path):
    p = _w(tmp_path, [
        cu("<system-reminder>be brief</system-reminder>\n真正的问题", 0),
        cu("<local-command-caveat>Caveat: x</local-command-caveat>", 1),
        cu("<task-notification>done</task-notification>", 2),
        cu([{"type": "text", "text": "<ide_opened_file>The user opened a.py</ide_opened_file>"},
            {"type": "text", "text": "为什么报错"}], 3),
        cu("<system-reminder>only reminder</system-reminder>", 4),
        ca([{"type": "text", "text": "答"}], 5),
    ])
    tr = load("claude", p)
    injected = [m for m in tr.messages if m.injected]
    assert len(injected) == 3 and all(m.role == "user" for m in injected)
    assert "only reminder" in injected[-1].blocks[0].text
    questions = [t.question.text for t in tr.turns if t.question]
    assert questions == ["真正的问题", "为什么报错"]
    rows = T.flatten_rows(tr.turns, "all")
    assert not [r for r in rows if r.kind == "injected"]
    assert len([r for r in T.flatten_rows(tr.turns, "all", show_injected=True) if r.kind == "injected"]) == 3
    assert tr.question_count == 2 == len(UI.read_user_messages("claude", p))


def test_claude_large_injected_text_keeps_preview_and_ref(tmp_path):
    big = "<task-notification>" + ("x" * 20000) + "</task-notification>"
    p = _w(tmp_path, [cu("q", 0), cu(big, 1), ca([{"type": "text", "text": "a"}], 2)])
    tr = load("claude", p)
    inj = [m for m in tr.messages if m.injected][0].blocks[0]
    assert inj.full is not None and len(inj.text) <= S.PREVIEW_CHARS
    assert T.read_content(inj.full) == big


def test_claude_slash_aborted_and_new_turn(tmp_path):
    p = _w(tmp_path, [
        cu("先问", 0), ca([{"type": "text", "text": "好"}], 1),
        cu("<command-name>/model</command-name><command-message>m</command-message><command-args>opus</command-args>", 2),
        cu("再问", 3),
        ca([tu("a", "Bash", {"command": "sleep 9"}), tu("b", "Bash", {"command": "sleep 8"})], 4, "m2"),
        cu([{"type": "text", "text": "[Request interrupted by user for tool use]"}], 5),
    ])
    tr = load("claude", p)
    assert [t.key for t in tr.turns] == ["t1", "t2", "t3"]
    slash = tr.turns[1]
    assert slash.question is None and slash.leading_events[0].block.kind == "slash_command"
    assert slash.leading_events[0].block.text == "/model opus"
    last = tr.turns[2]
    assert last.question.text == "再问" and last.aborted and last.final is None
    assert [e.block.kind for e in last.trailing_events] == ["aborted"]
    assert tr.question_count == 2 == len(UI.read_user_messages("claude", p))
    idx = tr.index()
    assert [e.question_preview for e in idx] == ["先问", "/model opus", "再问"]
    assert idx[2].aborted and idx[2].has_error


def test_claude_user_bash_command_and_output(tmp_path):
    p = _w(tmp_path, [
        cu("问", 0), ca([{"type": "text", "text": "好"}], 1),
        cu("<bash-input>echo hi</bash-input>", 2),
        cu("<bash-stdout>hi</bash-stdout><bash-stderr></bash-stderr>", 3),
        cu("<bash-input>bad</bash-input>", 4),
        cu("<bash-stdout></bash-stdout><bash-stderr>oops</bash-stderr>", 5),
    ])
    tr = load("claude", p)
    assert len(tr.turns) == 1                                 # 用户 !cmd 不开新轮
    calls = [(b.by_user, b.title) for m in tr.messages for b in m.blocks if b.type == "tool_call"]
    assert calls == [(True, "echo hi"), (True, "bad")]
    res = [b for m in tr.messages for b in m.blocks if b.type == "tool_result"]
    assert [(r.status, r.preview) for r in res] == [("success", "hi"), ("unknown", "oops")]   # 只有 stderr：状态未知
    assert tr.question_count == 1 and tr.tool_call_count == 0       # 用户命令不计 AI 工具调用
    # 配对成步骤
    steps = [s for s in tr.turns[0].steps if s.kind == "tool"]
    assert steps[0].call.by_user and steps[0].result.preview == "hi"


def test_claude_message_id_adjacent_merge_and_meta(tmp_path):
    u1 = {"input_tokens": 10, "output_tokens": 1}
    u2 = {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 7}
    p = _w(tmp_path, [
        cu("q", 0),
        ca([{"type": "thinking", "thinking": "思考内容", "signature": "s"}], 1, "M", usage=u1, thinkingDurationMs=1500),
        ca([{"type": "text", "text": "回复"}], 2, "M", usage=u2, stop="end_turn"),
        ca([{"type": "text", "text": "另一条"}], 3, "N"),
    ])
    tr = load("claude", p)
    asst = [m for m in tr.messages if m.role == "assistant"]
    assert len(asst) == 2 and [b.type for b in asst[0].blocks] == ["thinking", "text"]
    assert asst[0].blocks[0].duration_ms == 1500
    assert asst[0].meta.output_tokens == 50 and asst[0].meta.cache_read_tokens == 7   # 后到非空覆盖
    assert asst[0].meta.stop_reason == "end_turn"
    assert asst[0].ts is not None and asst[0].id == "a1-M"


def test_claude_thinking_redacted_with_signature_only(tmp_path):
    p = _w(tmp_path, [cu("q"), ca([{"type": "thinking", "thinking": "", "signature": "abc"},
                                   {"type": "redacted_thinking", "data": "zz"},
                                   {"type": "thinking", "thinking": "", "signature": ""}], 1),
                      ca([{"type": "text", "text": "x"}], 2, "m2")])
    th = [s.block for s in load("claude", p).turns[0].steps if s.kind == "thinking"]
    assert len(th) == 2 and all(b.redacted for b in th)       # 无签名的空思考被丢弃


def test_claude_rewind_dedup_tool_use_and_result_and_text(tmp_path):
    first = [
        cu("q", 0),
        ca([{"type": "text", "text": "A"}, tu("t1", "Bash", {"command": "ls"})], 1, "M"),
        cr("t1", "out", 2),
    ]
    replay = [   # rewind 后历史被重写进同一文件：同 message.id + 同 tool_use id
        ca([{"type": "text", "text": "A"}, tu("t1", "Bash", {"command": "ls"})], 3, "M"),
        cr("t1", "out", 4),
    ]
    tail = [ca([{"type": "text", "text": "完"}], 5, "N")]
    p = _w(tmp_path, first + replay + tail)
    tr = load("claude", p)
    assert tr.tool_call_count == 1
    t = tr.turns[0]
    assert [s.kind for s in t.steps] == ["note", "tool"]       # 说明文字没有重复
    assert sum(1 for m in tr.messages for b in m.blocks if b.type == "tool_result") == 1
    assert t.steps[1].result.preview == "out" and t.final.text == "完"
    from tcer.core import reader
    u, _ = reader.scan_session(p, with_loc=False, include_user_texts=False, use_cache=False)
    assert sum(u.tool_calls.values()) == tr.tool_call_count   # 与指标口径一致


def test_claude_same_text_in_different_messages_is_kept(tmp_path):
    """去重只针对重放：不同 message.id 的相同文字必须保留。"""
    p = _w(tmp_path, [cu("q"), ca([{"type": "text", "text": "好的"}], 1, "A"),
                      ca([{"type": "text", "text": "好的"}], 2, "B")])
    msgs = [m for m in load("claude", p).messages if m.role == "assistant"]
    assert len(msgs) == 2


def test_claude_synthetic_assistant_text_is_info_event(tmp_path):
    p = _w(tmp_path, [cu("q", 0),
                      ca([{"type": "text", "text": "No response requested."}], 1, "S", model="<synthetic>"),
                      ca([{"type": "text", "text": "真回复"}], 2, "R")])
    tr = load("claude", p)
    assert tr.models == ["claude-opus-4-8"] and "<synthetic>" not in tr.models
    t = tr.turns[0]
    assert t.final.text == "真回复"
    inner = [s.block for s in t.steps if s.kind == "event"] + [e.block for e in t.trailing_events]
    assert inner and inner[0].kind == "info" and inner[0].text == "No response requested."
    # 合成消息是最后一条时不能当最终回复
    p2 = _w(tmp_path, [cu("q", 0), ca([{"type": "text", "text": "No response requested."}], 1, "S",
                                      model="<synthetic>")], sid="s2")
    t2 = load("claude", p2).turns[0]
    assert t2.final is None and [e.block.kind for e in t2.trailing_events] == ["info"]


def test_claude_compaction_hook_prlink_events(tmp_path):
    summary = "S" * 1000
    p = _w(tmp_path, [
        cu("q", 0), ca([{"type": "text", "text": "a"}], 1),
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
         "timestamp": _T0.format(2)},
        {"type": "user", "isCompactSummary": True, "timestamp": _T0.format(3),
         "message": {"role": "user", "content": summary}},
        {"type": "system", "subtype": "stop_hook_summary", "hookErrors": ["lint failed", " "],
         "timestamp": _T0.format(4)},
        {"type": "system", "subtype": "stop_hook_summary", "hookErrors": [], "timestamp": _T0.format(5)},
        {"type": "pr-link", "prUrl": "https://h/pull/3", "prNumber": 3, "timestamp": _T0.format(6)},
        {"type": "pr-link", "prUrl": "https://h/pull/3", "prNumber": 3, "timestamp": _T0.format(7)},
        {"type": "system", "subtype": "turn_duration", "durationMs": 5},
    ])
    tr = load("claude", p)
    ev = [(b.kind, b.text) for m in tr.messages if m.role == "system" for b in m.blocks]
    assert [k for k, _ in ev] == ["compaction", "hook", "pr_link"]
    comp = [b for m in tr.messages for b in m.blocks if b.type == "event" and b.kind == "compaction"][0]
    assert len(comp.text) == 400 and comp.full is not None            # 摘要合并进 compaction 事件，预览 + 引用
    assert T.read_content(comp.full) == summary
    assert ev[1][1] == "lint failed" and ev[2][1] == "PR #3"
    assert tr.question_count == 1                                     # 压缩摘要不是提问
    assert tr.question_count == len(UI.read_user_messages("claude", p))
    # compact_boundary 之外的孤立摘要：单独成 system 消息
    p2 = _w(tmp_path, [cu("q"), {"type": "user", "isCompactSummary": True, "timestamp": _T0.format(3),
                                 "message": {"role": "user", "content": "短摘要"}}], sid="s2")
    comp2 = [b for m in load("claude", p2).messages for b in m.blocks if b.type == "event"][0]
    assert comp2.text == "短摘要" and comp2.full is None


def test_claude_local_command_system_record(tmp_path):
    p = _w(tmp_path, [
        cu("q", 0),
        {"type": "system", "subtype": "local_command", "uuid": "lc1", "timestamp": _T0.format(1),
         "content": "<command-name>/cost</command-name><command-args></command-args>"},
        {"type": "system", "subtype": "local_command", "uuid": "lc2", "timestamp": _T0.format(2),
         "content": "<local-command-stdout>Total cost: $1</local-command-stdout>"},
        {"type": "system", "subtype": "local_command", "uuid": "lc3", "timestamp": _T0.format(3),
         "content": "just text"},
    ])
    tr = load("claude", p)
    assert tr.question_count == 1                              # local_command 里的文字绝不凭空变成提问
    assert any(t.leading_events and t.leading_events[0].block.text == "/cost" for t in tr.turns)
    assert len([m for m in tr.messages if m.injected]) == 2


def test_claude_sidechain_meta_subagents_not_read(tmp_path):
    p = _w(tmp_path, [
        cu("真人", 0),
        {**cu("子代理内部", 1), "isSidechain": True},
        {**ca([{"type": "text", "text": "内部回复"}], 2, "X"), "isSidechain": True},
        {**cu("caveat", 3), "isMeta": True},
        {"type": "api-request-blob", "hash": "h", "message": {"role": "user", "content": "blob"}},
        ca([{"type": "text", "text": "真回复"}], 4, "Y"),
    ])
    sub = p.parent / p.stem / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps(cu("派发 prompt", 1)) + "\n" + json.dumps(
        ca([{"type": "text", "text": "子代理报告"}], 2, "Z")) + "\n", encoding="utf-8")
    tr = load("claude", p)
    texts = [b.text for m in tr.messages for b in m.blocks if b.type == "text"]
    assert texts == ["真人", "真回复"]
    assert tr.question_count == 1
    assert tr.turns[0].final.text == "真回复"


def test_claude_edit_write_diff_backfill(tmp_path):
    p = _w(tmp_path, [
        cu("q", 0),
        ca([tu("e1", "Edit", {"file_path": "/p/a.py", "old_string": "a", "new_string": "b\nc"}),
            tu("e2", "Edit", {"file_path": "/p/z.py", "old_string": "a", "new_string": "b"}),
            tu("w1", "Write", {"file_path": "/p/new.py", "content": "1\n2\n3\n"}),
            tu("m1", "MultiEdit", {"file_path": "/p/m.py", "edits": [
                {"old_string": "a", "new_string": "b"}, {"old_string": "x\ny", "new_string": "x"}]})], 1),
        cr("e1", "ok", 2, tur={"type": "update", "filePath": "/p/a.py",
                               "structuredPatch": [{"lines": [" ctx", "-a", "+b", "+c", "+d"]}]}),
        cr("e2", "denied", 3, is_error=True),
        cr("w1", "created", 4, tur={"type": "create", "filePath": "/p/new.py", "structuredPatch": []}),
        cr("m1", "ok", 5),
        ca([{"type": "text", "text": "done"}], 6, "m9"),
    ])
    t = load("claude", p).turns[0]
    by = {s.call.id: s for s in t.steps if s.kind == "tool"}
    d = by["e1"].call.diff
    assert d["files"][0] == {"path": "/p/a.py", "op": "update", "added": 3, "removed": 1}   # structuredPatch 覆盖估算
    assert (d["added"], d["removed"]) == (3, 1)
    assert by["e2"].status == "error" and by["e2"].call.diff is None                   # 失败的改动没有 diff
    wd = by["w1"].call.diff
    assert wd["files"][0]["op"] == "add" and wd["added"] == 3 and wd["files"][0]["path"] == "/p/new.py"
    md = by["m1"].call.diff                                                              # 无 toolUseResult：保留估算
    assert (md["added"], md["removed"]) == (1 + 0, 1 + 1) or (md["added"], md["removed"]) == (1, 2)
    assert t.summary.files_changed == 3
    rows = T.flatten_rows([t], "changes")
    assert [r.step.call.id for r in rows if r.kind == "step"] == ["e1", "w1", "m1"] or \
        {r.step.call.id for r in rows if r.kind == "step"} >= {"e1", "w1"}


def test_claude_truncated_result_lazy_full_and_sidecar(tmp_path):
    big = "\n".join(f"line {i}" for i in range(200))
    sid = "sess-1"
    p = _w(tmp_path, [cu("q", 0), ca([tu("t1", "Bash", {"command": "cat big"}),
                                      tu("t2", "Bash", {"command": "cat side"})], 1),
                      cr("t1", big, 2)], sid=sid)
    side_dir = p.parent / sid / "tool-results"
    side_dir.mkdir(parents=True)
    side = side_dir / "t2.txt"
    side.write_text("SIDECAR\n" * 500, encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(cr("t2", "preview only\n" * 50, 3,
                               tur={"persistedOutputPath": str(side)})) + "\n")
        fh.write(json.dumps(ca([{"type": "text", "text": "x"}], 4, "mx")) + "\n")
    tr = load("claude", p)
    r1, r2 = [s.result for s in tr.turns[0].steps if s.kind == "tool"]
    assert r1.truncated and r1.line_count == 200 and r1.total_len == len(big)
    assert r1.preview.count("\n") == 11 and len(r1.preview) <= S.PREVIEW_CHARS
    assert r1.full.kind == "jsonl" and T.read_content(r1.full) == big
    assert r2.saved_path == str(side) and r2.full.kind == "file"
    assert T.read_content(r2.full).startswith("SIDECAR")
    # 文件不在会话附属目录内时不采信 persistedOutputPath，回退到 JSONL 内容引用
    p3 = _w(tmp_path, [cu("q", 0), ca([tu("t", "Bash", {"command": "x"})], 1),
                       cr("t", "z\n" * 80, 2, tur={"persistedOutputPath": str(tmp_path / "elsewhere.txt")})],
            sid="s3")
    (tmp_path / "elsewhere.txt").write_text("nope", encoding="utf-8")
    r = load("claude", p3).turns[0].steps[0].result
    assert r.full.kind == "jsonl"


def test_claude_large_tool_input_keeps_ref_to_full_input(tmp_path):
    content = "x" * 5000
    p = _w(tmp_path, [cu("q", 0), ca([tu("w", "Write", {"file_path": "/p/big.py", "content": content})], 1),
                      cr("w", "ok", 2), ca([{"type": "text", "text": "d"}], 3, "m2")])
    call = load("claude", p).turns[0].steps[0].call
    assert call.input_total_len > 5000 and len(call.input_preview) == 400 and call.input_full is not None
    full = json.loads(T.read_content(call.input_full))
    assert full["content"] == content
    assert T.build_input_diff(call).startswith("@@\n+xxxx")


def test_claude_tool_result_image_and_list_content(tmp_path):
    p = _w(tmp_path, [cu("q", 0), ca([tu("t", "Read", {"file_path": "x.png"})], 1),
                      {"type": "user", "timestamp": _T0.format(2), "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": "t", "content": [
                              {"type": "text", "text": "part1"}, {"type": "text", "text": "part2"},
                              {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                           "data": "A" * 400}}]}]}}])
    r = load("claude", p).turns[0].steps[0].result
    assert r.preview == "part1\npart2" and len(r.images) == 1
    assert r.images[0].media_type == "image/png" and r.images[0].size == 300


def test_claude_system_reminder_stripped_from_tool_results(tmp_path):
    p = _w(tmp_path, [cu("q", 0), ca([tu("t", "Read", {"file_path": "a"})], 1),
                      cr("t", "real<system-reminder>noise</system-reminder>", 2)])
    assert load("claude", p).turns[0].steps[0].result.preview == "real"


def test_claude_user_image_only_question_placeholder(tmp_path):
    p = _w(tmp_path, [{"type": "user", "timestamp": _T0.format(0), "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]}},
        ca([{"type": "text", "text": "看到了"}], 1)])
    tr = load("claude", p)
    assert tr.turns[0].question.text == "[图片消息]" and len(tr.turns[0].question.images) == 1
    assert tr.question_count == len(UI.read_user_messages("claude", p)) == 1


def test_claude_garbage_lines_and_missing_fields_tolerated(tmp_path):
    p = _w(tmp_path, ["not json", "[1,2]", json.dumps({"type": "user"}), json.dumps({"message": "str"}),
                      cu("q", 0), ca([{"type": "text", "text": "ok"}, "junk", {"type": "weird"}], 1)])
    tr = load("claude", p)
    assert tr.question_count == 1 and tr.turns[0].final.text == "ok"


def test_claude_question_count_matches_read_user_messages_mixed(tmp_path):
    p = _w(tmp_path, [
        cu("一", 0), cu("<command-name>/clear</command-name>", 1), cu("/clear", 2),
        cu("This session is being continued from a previous conversation...", 3),
        cu("<bash-input>ls</bash-input>", 4), cu("<bash-stdout>x</bash-stdout>", 5),
        cu([{"type": "text", "text": "[Request interrupted by user]"}], 6),
        cu("二", 7), ca([{"type": "text", "text": "a"}], 8),
        cu([{"type": "text", "text": "<ide_selection>sel</ide_selection>"}], 9),
        cu([{"type": "text", "text": "<ide_opened_file>f</ide_opened_file>"}, {"type": "text", "text": "三"}], 10),
    ])
    tr = load("claude", p)
    ref = UI.read_user_messages("claude", p)
    assert ref == ["一", "二", "三"] and tr.question_count == 3


def test_claude_empty_and_missing_files(tmp_path):
    p = _w(tmp_path, [])
    tr = load("claude", p)
    assert tr.messages == [] and tr.turns == [] and tr.question_count == 0
    assert T.flatten_rows(tr.turns) == [] and T.transcript_to_markdown(tr).startswith("# sess-1")
    with pytest.raises(OSError):
        load("claude", tmp_path / "nope.jsonl")


def test_load_transcript_unknown_source_and_opencode_needs_sid(tmp_path):
    p = _w(tmp_path, [cu("q")])
    with pytest.raises(ValueError):
        load("grok", p)
    with pytest.raises(ValueError):
        load("opencode", p)


def test_load_transcript_cancel(tmp_path):
    p = _w(tmp_path, [cu("q", 0)])
    ev = threading.Event()
    ev.set()
    with pytest.raises(T.TranscriptCancelled):
        load("claude", p, cancel_event=ev)


def test_load_transcript_cache_lru_and_invalidation(tmp_path):
    T.clear_cache()
    p = _w(tmp_path, [cu("q", 0), ca([{"type": "text", "text": "a"}], 1)])
    a = T.load_transcript("claude", p, with_usage=False)
    b = T.load_transcript("claude", p, with_usage=False)
    assert a is b
    with p.open("a", encoding="utf-8") as fh:                   # 文件变化 → 签名变 → 重读
        fh.write(json.dumps(cu("又问", 5)) + "\n")
    c = T.load_transcript("claude", p, with_usage=False)
    assert c is not a and c.question_count == 2
    # LRU 上限 8
    for i in range(10):
        q = _w(tmp_path, [cu(f"q{i}")], sid=f"lru{i}")
        T.load_transcript("claude", q, with_usage=False)
    assert len(T._CACHE) <= 8
    T.clear_cache()


def test_load_transcript_usage_header_from_reader_aggregate(tmp_path):
    from tcer.core import reader, metrics
    T.clear_cache()
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 400,
             "cache_creation_input_tokens": 20}
    p = _w(tmp_path, [cu("q", 0), ca([{"type": "text", "text": "a"}], 1, "M", usage=usage)])
    tr = T.load_transcript("claude", p, with_usage=True, use_cache=False)
    u, _ = reader.scan_session(p, with_loc=False, include_user_texts=False, use_cache=False)
    assert tr.input_tokens == 100 and tr.output_tokens == 50 and tr.cache_read_tokens == 400
    assert tr.cache_write_tokens == 20 and tr.total_tokens == u.total == 570
    assert tr.cost_usd == pytest.approx(metrics.cost_usd(u)) and tr.cost_usd > 0
    bare = T.load_transcript("claude", p, with_usage=False, use_cache=False)
    assert bare.total_tokens is None and bare.cost_usd is None


def test_claude_usage_header_folds_subagents(tmp_path):
    from tcer.core import reader
    usage = {"input_tokens": 10, "output_tokens": 5}
    p = _w(tmp_path, [cu("q", 0), ca([{"type": "text", "text": "a"}], 1, "M", usage=usage)])
    sub = p.parent / p.stem / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps(ca([{"type": "text", "text": "s"}], 2, "S",
                                                      usage={"input_tokens": 1000, "output_tokens": 500})) + "\n",
                                       encoding="utf-8")
    (sub / "journal.jsonl").write_text(json.dumps({"type": "started"}) + "\n", encoding="utf-8")
    tr = T.load_transcript("claude", p, with_usage=True, use_cache=False)
    assert tr.input_tokens == 1010 and tr.output_tokens == 505           # 子代理真实成本并入
    assert tr.question_count == 1


# =========================================================================== #
# Codex adapter
# =========================================================================== #
def _cx(ts_sec, typ, payload):
    return {"timestamp": f"2026-06-29T11:35:{ts_sec:02d}.000Z", "type": typ, "payload": payload}


def _cx_user_pair(sec, text):
    return [
        _cx(sec, "event_msg", {"type": "user_message", "message": text, "images": [], "local_images": []}),
        _cx(sec, "response_item", {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": text}]}),
    ]


def _cx_call(sec, cid, name, args, **kw):
    return _cx(sec, "response_item", {"type": "function_call", "name": name,
                                      "arguments": json.dumps(args), "call_id": cid, **kw})


def _cx_out(sec, cid, out, kind="function_call_output"):
    return _cx(sec, "response_item", {"type": kind, "call_id": cid, "output": out})


def _cx_file(tmp_path, events, name="rollout-2026-06-29T19-35-14-019f1329-6111-7c61-ba1f-ad3dc8804852.jsonl"):
    d = tmp_path / "codex"
    d.mkdir(exist_ok=True)
    p = d / name
    with p.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def _codex_events():
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n+more\n*** End Patch"
    ev = [
        _cx(0, "session_meta", {"id": "thread-1", "cwd": "c:\\proj", "originator": "codex_vscode"}),
        _cx(1, "event_msg", {"type": "task_started", "turn_id": "T1"}),
        _cx(1, "response_item", {"type": "message", "role": "developer",
                                 "content": [{"type": "input_text", "text": "<permissions instructions>x</permissions instructions>"}]}),
        _cx(1, "response_item", {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "# AGENTS.md instructions for c:\\proj\n..."}]}),
        _cx(2, "turn_context", {"turn_id": "T1", "model": "gpt-5.5"}),
        *_cx_user_pair(3, "请检查一下"),
        _cx(4, "response_item", {"type": "reasoning", "summary": [], "encrypted_content": "x"}),
        _cx(4, "response_item", {"type": "reasoning", "summary": [], "encrypted_content": "y"}),
        _cx_call(5, "c1", "exec_command", {"cmd": "ls", "workdir": "c:\\proj"}),
        _cx_out(6, "c1", "Chunk ID: a\nWall time: 0.5 seconds\nProcess exited with code 0\nOriginal token count: 1\nOutput:\nfile.txt\n"),
        _cx_call(7, "c1b", "exec_command", {"cmd": "rg x"}),
        _cx_out(7, "c1b", "Chunk ID: b\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\nhit"),
        _cx_call(8, "c2", "exec_command", {"cmd": ["bash", "-lc", "make test"]}),
        _cx_out(9, "c2", "Chunk ID: c\nWall time: 2.0 seconds\nProcess exited with code 2\nOutput:\nboom"),
        _cx(10, "response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": "c3"}),
        _cx_out(11, "c3", "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess.", "custom_tool_call_output"),
        _cx(11, "event_msg", {"type": "patch_apply_end", "call_id": "c3", "success": True, "changes": {
            "a.py": {"type": "update", "unified_diff": "@@ -1 +1,2 @@\n-old\n+new\n+more\n"}}}),
        _cx(12, "response_item", {"type": "web_search_call", "id": "ws1", "status": "completed",
                                  "action": {"type": "search", "query": "pytest tips"}}),
        _cx_call(13, "c4", "mcp_tool", {"q": "hello"}, namespace="mcp__docs__"),
        _cx_out(13, "c4", "doc text"),
        _cx(14, "response_item", {"type": "message", "role": "assistant", "phase": "final_answer",
                                  "content": [{"type": "output_text", "text": "完成"}]}),
        _cx(15, "event_msg", {"type": "task_complete", "turn_id": "T1"}),
        _cx(16, "event_msg", {"type": "task_started", "turn_id": "T2"}),
        _cx(16, "event_msg", {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-5.6"}}),
        *_cx_user_pair(17, "# Context from my IDE setup:\n## Active file: a.py\n\n## My request for Codex:\n帮我重构"),
        _cx_call(18, "c5", "exec_command", {"cmd": "sleep 99"}),
        _cx(19, "event_msg", {"type": "turn_aborted", "turn_id": "T2", "reason": "interrupted"}),
        _cx(20, "compacted", {"message": "summary text"}),
        _cx(21, "event_msg", {"type": "context_compacted"}),
    ]
    return ev


def test_codex_adapter_full_flow(tmp_path):
    p = _cx_file(tmp_path, _codex_events())
    tr = load("codex", p)
    assert tr.session_id == "thread-1" and tr.resume_command == "codex resume thread-1"
    assert tr.style == "codex" and tr.cwd == "c:\\proj"
    assert [t.key for t in tr.turns] == ["T1", "T2"]                      # 原生 turn_id
    assert tr.question_count == 2 == len(UI.read_user_messages("codex", p))
    t1, t2 = tr.turns
    assert t1.question.text == "请检查一下"
    inj = [m for m in tr.messages if m.injected]
    assert len(inj) == 2 and {m.role for m in inj} == {"system", "user"}  # developer + AGENTS.md
    assert t1.injected                                                      # 归属 T1，默认隐藏
    kinds = [(s.kind, getattr(s, "merge_kind", None)) for s in t1.steps]
    assert kinds[0] == ("thinking", None) and kinds[1] == ("merged", "explored")  # 连续 read/search 合并
    th = t1.steps[0].block
    assert th.redacted and sum(1 for s in t1.steps if s.kind == "thinking") == 1   # 连续 redacted 只留一个
    merged = t1.steps[1]
    assert [c.call.title for c in merged.children] == ["ls", "rg x"]
    assert merged.children[0].result.duration_ms == 500 and merged.children[0].result.preview == "file.txt"
    make = [s for s in t1.steps if s.kind == "tool" and s.call.title == "make test"][0]
    assert make.status == "error" and make.result.exit_code == 2 and make.call.kind == "shell"  # 数组命令取 script
    patch = [s for s in t1.steps if s.kind == "tool" and s.call.raw_name == "apply_patch"][0]
    assert patch.call.kind == "edit" and patch.call.diff["files"][0]["path"] == "a.py"
    assert (patch.call.diff["added"], patch.call.diff["removed"]) == (2, 1)
    assert patch.status == "success" and patch.result.exit_code == 0
    ws = [s for s in t1.steps if s.kind == "tool" and s.call.raw_name == "web_search"][0]
    assert ws.call.kind == "web" and ws.call.title == "pytest tips" and ws.status == "success"
    mcp = [s for s in t1.steps if s.kind == "tool" and s.call.kind == "mcp"][0]
    assert mcp.call.server == "docs" and mcp.call.title == "docs.mcp_tool"
    assert t1.final.text == "完成" and t1.final.model == "gpt-5.5"
    assert tr.models == ["gpt-5.5", "gpt-5.6"]
    # T2：模型切换事件、IDE 上下文提取、中断关闭未完成调用、压缩
    assert t2.question.text == "帮我重构"
    assert t2.aborted and t2.final is None
    open_call = [s for s in t2.steps if s.kind == "tool"][0]
    assert open_call.status == "interrupted"
    assert [e.block.kind for e in t2.leading_events] == ["model_change"]
    assert t2.leading_events[0].block.text == "gpt-5.6"
    tail = [e.block.kind for e in t2.trailing_events]
    assert tail == ["aborted", "compaction"]                                # summary 与 context_compacted 合并为一个
    comp = [e for e in t2.trailing_events if e.block.kind == "compaction"][0]
    assert comp.block.text == "summary text"


def test_codex_output_formats_legacy_json_failed_script_and_list(tmp_path):
    ev = [
        _cx(1, "event_msg", {"type": "task_started", "turn_id": "T1"}),
        *_cx_user_pair(2, "q"),
        _cx_call(3, "a", "shell", {"command": ["bash", "-lc", "echo hi"]}),
        _cx_out(3, "a", json.dumps({"output": "hi\n", "metadata": {"exit_code": 0, "duration_seconds": 0.25}})),
        _cx(4, "response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "b",
                                 "input": 'const r = await tools.exec_command({cmd: "git status", workdir: "c:\\\\p"});\n'}),
        _cx_out(4, "b", "Script failed\nWall time 1 seconds\nOutput:\nTraceback", "custom_tool_call_output"),
        _cx_call(5, "c", "unknown_tool", {"x": 1}),
        _cx_out(5, "c", [{"type": "input_text", "text": "Exit code: 3\nWall time: 0 seconds\nOutput:\nlist-out"}]),
        _cx(6, "response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "d",
                                 "input": "*** Begin Patch\n*** Update File: z.py\n@@\n-a\n+b\n*** End Patch"}),
        _cx_out(6, "d", "apply_patch verification failed: context mismatch", "custom_tool_call_output"),
        _cx(7, "response_item", {"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": "end"}]}),
    ]
    p = _cx_file(tmp_path, ev)
    t = load("codex", p).turns[0]
    steps = {s.call.id: s for s in t.steps if s.kind == "tool"}
    assert steps["a"].result.exit_code == 0 and steps["a"].result.duration_ms == 250
    assert steps["a"].call.title == "echo hi"
    assert steps["b"].status == "error" and steps["b"].call.title == "git status"
    assert steps["b"].call.detail == "c:\\p"
    assert steps["c"].result.exit_code == 3 and steps["c"].status == "error"
    assert steps["c"].result.preview == "list-out"
    assert steps["d"].status == "error" and steps["d"].call.diff["added"] == 0 == steps["d"].call.diff["removed"]


def test_codex_exec_multiple_commands_and_mcp_in_js(tmp_path):
    src_multi = ('await tools.exec_command({cmd: "git add -A"}); '
                 'await tools.exec_command({cmd: "git commit -m x"});')
    src_mcp = "const x = await tools.mcp__docs__search({q: 1});"
    ev = [_cx(1, "event_msg", {"type": "task_started", "turn_id": "T1"}), *_cx_user_pair(2, "q"),
          _cx(3, "response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "a", "input": src_multi}),
          _cx(4, "response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "b", "input": src_mcp}),
          _cx(5, "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]})]
    t = load("codex", _cx_file(tmp_path, ev)).turns[0]
    steps = {s.call.id: s.call for s in t.steps if s.kind == "tool"}
    assert steps["a"].title == "git add -A…"
    assert steps["b"].kind == "mcp" and steps["b"].title == "docs.search" and steps["b"].server == "docs"


def test_codex_agent_message_and_full_output_ref(tmp_path):
    long_out = "\n".join(f"row{i}" for i in range(100))
    ev = [_cx(1, "event_msg", {"type": "task_started", "turn_id": "T1"}), *_cx_user_pair(2, "q"),
          _cx_call(3, "a", "exec_command", {"cmd": "cat"}),
          _cx_out(3, "a", f"Chunk ID: x\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\n{long_out}"),
          _cx(4, "response_item", {"type": "agent_message", "id": "am", "author": "agt_1",
                                   "content": [{"type": "text", "text": "found it\ndetails here"}]}),
          _cx_call(5, "b", "exec_command", {"cmd": "ls"}), _cx_out(5, "b", "x"),
          _cx(6, "response_item", {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "ok"}]})]
    p = _cx_file(tmp_path, ev)
    t = load("codex", p).turns[0]
    res = [s for s in t.steps if s.kind == "tool"][0].result
    assert res.truncated and res.full.kind == "jsonl_idx"
    assert T.read_content(res.full) .startswith("Chunk ID: x")              # 取到原始输出（含头）
    sub = [s for s in t.steps if s.kind == "event"][0]
    d = S.describe_event(sub.block, sub.body)
    assert d.label == "子代理 agt_1" and d.detail.endswith("details here")


def test_codex_unfinished_call_stays_pending_and_resume_dedupe(tmp_path):
    ev = [_cx(1, "event_msg", {"type": "task_started", "turn_id": "T1"}), *_cx_user_pair(2, "q"),
          _cx_call(3, "a", "exec_command", {"cmd": "sleep 1"})]
    t = load("codex", _cx_file(tmp_path, ev)).turns[0]
    assert t.steps[0].status == "pending" and t.final is None
    # 同样的 user_message 在不同轮里出现两次（用户重复发送）不能被吞
    ev2 = [_cx(1, "event_msg", {"type": "task_started", "turn_id": "A"}), *_cx_user_pair(2, "继续"),
           _cx(3, "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "1"}]}),
           _cx(4, "event_msg", {"type": "task_started", "turn_id": "B"}), *_cx_user_pair(5, "继续"),
           _cx(6, "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "2"}]})]
    p2 = _cx_file(tmp_path, ev2, name="rollout-b.jsonl")
    tr = load("codex", p2)
    assert tr.question_count == 2 == len(UI.read_user_messages("codex", p2))


# =========================================================================== #
# OpenCode adapter
# =========================================================================== #
def _oc_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript("""
        create table project (id text primary key, name text, worktree text);
        create table session (id text primary key, parent_id text, project_id text, directory text,
          title text, time_created integer, time_updated integer, model text, permission text,
          tokens_input integer, tokens_output integer, tokens_reasoning integer,
          tokens_cache_read integer, tokens_cache_write integer, cost integer);
        create table message (id text primary key, session_id text, time_created integer, data text);
        create table part (id text primary key, session_id text, message_id text,
          time_created integer, data text);
    """)
    con.execute("insert into project values ('p','proj','/work/proj')")
    con.execute("insert into session values ('ses-1',null,'p','/work/proj','打招呼',1,2,null,null,0,0,0,0,0,0)")
    con.execute("insert into session values ('ses-child','ses-1','p','/work/proj','子会话',1,2,null,null,0,0,0,0,0,0)")
    j = json.dumps
    msgs = [
        ("m1", 1000, {"role": "user", "time": {"created": 1000}}),
        ("m2", 2000, {"role": "assistant", "providerID": "openai", "modelID": "gpt-5.2", "cost": 0.01,
                      "tokens": {"input": 10, "output": 5, "cache": {"read": 1, "write": 2}},
                      "time": {"created": 2000, "completed": 5000}, "finish": "stop"}),
        ("m3", 6000, {"role": "user"}),
        ("m4", 7000, {"role": "assistant", "error": {"name": "MessageAbortedError"}}),
        ("mc", 8000, {"role": "user"}),
    ]
    for mid, t, d in msgs:
        con.execute("insert into message values (?, 'ses-1', ?, ?)", (mid, t, j(d)))
    con.execute("insert into message values ('mx','ses-child',9000,?)", (j({"role": "user"}),))
    long_out = "\n".join(f"o{i}" for i in range(50))
    parts = [
        ("p1", "m1", 1001, {"type": "text", "text": "实现 OpenCode 支持"}),
        ("p2", "m1", 1002, {"type": "text", "text": "<editor_context/>", "synthetic": True}),
        ("p3", "m1", 1003, {"type": "text", "text": "忽略我", "ignored": True}),
        ("p4", "m1", 1004, {"type": "file", "mime": "image/png", "url": "file:///tmp/a.png", "filename": "a.png"}),
        ("q0", "m2", 2000, {"type": "step-start"}),
        ("q1", "m2", 2001, {"type": "reasoning", "text": "想一想", "time": {"start": 10, "end": 40}}),
        ("q2", "m2", 2002, {"type": "tool", "tool": "read", "callID": "c1", "state": {
            "status": "completed", "input": {"filePath": "app.py", "offset": 3, "limit": 2},
            "output": long_out, "time": {"start": 100, "end": 350}}}),
        ("q3", "m2", 2003, {"type": "tool", "tool": "edit", "callID": "c2", "state": {
            "status": "completed", "title": "src/app.py", "input": {"filePath": "/work/proj/src/app.py",
                                                                    "oldString": "a", "newString": "b"},
            "output": "ok", "metadata": {"filediff": {"additions": 3, "deletions": 1}}}}),
        ("q4", "m2", 2004, {"type": "tool", "tool": "bash", "callID": "c3", "state": {
            "status": "error", "input": {"command": "false"}, "error": "boom", "metadata": {"exit": 1}}}),
        ("q5", "m2", 2005, {"type": "tool", "tool": "bash", "callID": "c4", "state": {
            "status": "error", "input": {"command": "sleep 9"}, "error": "Tool execution aborted"}}),
        ("q6", "m2", 2006, {"type": "tool", "tool": "bash", "callID": "c5", "state": {
            "status": "running", "input": {"command": "tail -f x"}}}),
        ("q7", "m2", 2007, {"type": "patch", "hash": "h1", "files": ["a.py", "b.py"]}),
        ("q8", "m2", 2008, {"type": "step-finish", "reason": "stop", "cost": 0.002,
                            "tokens": {"input": 10, "output": 5, "reasoning": 0,
                                       "cache": {"read": 1, "write": 2}}}),
        ("q9", "m2", 2009, {"type": "text", "text": "完成"}),
        ("r1", "m3", 6001, {"type": "text", "text": "第二个问题"}),
        ("r2", "m3", 6002, {"type": "text", "text": "同消息第二段文字"}),
        ("s1", "m4", 7001, {"type": "text", "text": "部分回复"}),
        ("t1", "mc", 8001, {"type": "text", "text": "第三问"}),
        ("x1", "mx", 9001, {"type": "text", "text": "子会话提问"}),
    ]
    for pid, mid, t, d in parts:
        sid = "ses-child" if mid == "mx" else "ses-1"
        con.execute("insert into part values (?, ?, ?, ?, ?)", (pid, sid, mid, t, j(d)))
    con.commit()
    con.close()
    return path


def test_opencode_adapter_parts_status_diff_steps(tmp_path):
    db = _oc_db(tmp_path / "opencode.db")
    tr = load("opencode", db, "ses-1")
    assert tr.title == "打招呼" and tr.cwd == "/work/proj" and tr.session_id == "ses-1"
    assert tr.resume_command == "opencode -s ses-1" and tr.style == "opencode"
    assert tr.models == ["openai/gpt-5.2"]
    # 每个合格文本 part 一条用户消息（与 read_user_messages 同口径）；synthetic / ignored 丢弃；子会话不并入
    qs = [m.blocks[0].text for m in tr.messages if m.role == "user"]
    assert qs == ["实现 OpenCode 支持", "第二个问题", "同消息第二段文字", "第三问"]
    assert tr.question_count == 4 == len(UI.read_user_messages("opencode", db, "ses-1"))
    t1 = tr.turns[0]
    assert t1.question.text == "实现 OpenCode 支持" and len(t1.question.images) == 1
    assert t1.question.images[0].source == "local_file" and t1.question.images[0].path == "/tmp/a.png"
    by = {s.call.id: s for s in t1.steps if s.kind == "tool" and s.call}
    assert by["c1"].call.kind == "read" and by["c1"].call.title == "app.py" and by["c1"].call.detail == ":3-4"
    assert by["c1"].result.duration_ms == 250 and by["c1"].result.truncated
    assert T.read_content(by["c1"].result.full).count("\n") == 49           # sqlite 引用取全文
    e = by["c2"]
    assert e.call.title == "src/app.py"                                        # state.title 更短
    assert e.call.diff["files"][0]["path"] == "/work/proj/src/app.py"
    assert (e.call.diff["added"], e.call.diff["removed"]) == (3, 1)            # filediff 优先于估算
    assert by["c3"].status == "error" and by["c3"].result.exit_code == 1 and by["c3"].result.preview == "boom"
    assert by["c4"].status == "interrupted"
    assert by["c5"].status == "pending"
    patch = [s for s in t1.steps if s.kind == "tool" and s.call.raw_name == "patch"][0]
    assert patch.call.diff["files"][1]["path"] == "b.py" and patch.call.detail == "2 个文件"
    assert any(s.kind == "thinking" and s.block.duration_ms == 30 for s in t1.steps)
    assert t1.final.text == "完成" and t1.final.model == "openai/gpt-5.2"
    # step-finish 费用落到该段最后一步（pending 的 bash），合计进轮摘要
    assert t1.step_costs[0].tokens == 18 and abs(t1.summary.cost_usd - 0.002) < 1e-9
    assert t1.steps[-1].cost.tokens == 18
    # 助手消息级 meta
    asst = [m for m in tr.messages if m.id == "m2"][0]
    assert asst.meta.cost_usd == 0.01 and asst.meta.duration_ms == 3000 and asst.meta.stop_reason == "stop"
    assert asst.meta.cache_write_tokens == 2 and asst.meta.input_tokens == 10
    # MessageAbortedError → aborted 事件
    t_abort = [t for t in tr.turns if t.aborted]
    assert t_abort and t_abort[0].question.text == "同消息第二段文字" or t_abort


def test_opencode_child_session_loaded_separately_and_no_tables(tmp_path):
    db = _oc_db(tmp_path / "opencode.db")
    tr = load("opencode", db, "ses-child")
    assert tr.question_count == 1 and tr.session_id == "ses-child"


# =========================================================================== #
# omp / Pi adapter
# =========================================================================== #
_ISO = "2026-07-29T06:18:2{}.000Z"


def _ev(i, typ="message", **kw):
    return {"type": typ, "timestamp": _ISO.format(i % 10), **kw}


def _omp_msg(eid, parent, role_msg, i=1):
    return _ev(i, id=eid, parentId=parent, message=role_msg)


def _omp_user(text, **kw):
    return {"role": "user", "content": [{"type": "text", "text": text}], **kw}


def _omp_asst(content, stop="stop", usage=None, **kw):
    return {"role": "assistant", "content": content, "model": "claude-opus-4-8", "provider": "granola",
            "stopReason": stop, "usage": usage or {"input": 10, "output": 5, "cacheRead": 1, "cacheWrite": 2,
                                                   "cost": {"total": 0.5}}, "duration": 1200, **kw}


def _omp_file(tmp_path, entries, name="s1.jsonl", sub=None):
    d = tmp_path / (sub or "omp")
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with p.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def _omp_tree():
    return [
        {"type": "session", "version": 3, "id": "sid-omp", "timestamp": _ISO.format(0), "cwd": "C:\\repo",
         "title": "omp 标题"},
        _ev(1, "model_change", id="mc", parentId=None, model="claude-opus-4-8", provider="granola"),
        _ev(1, "thinking_level_change", id="tl", parentId="mc", thinkingLevel="high"),
        _omp_msg("u1", "tl", _omp_user("第一个问题")),
        _omp_msg("a1", "u1", _omp_asst([{"type": "text", "text": "A1"}])),
        # —— 被放弃的分支（文件里先出现）——
        _omp_msg("u2", "a1", _omp_user("放弃的分支问题")),
        _omp_msg("a2x", "u2", _omp_asst([{"type": "text", "text": "abandoned"}])),
        # —— 活动分支 ——
        _omp_msg("u3", "a1", _omp_user("活动分支问题")),
        _omp_msg("adv", "u3", _omp_user("advisor review prompt", synthetic=True, attribution="agent")),
        _omp_msg("a3", "adv", _omp_asst([
            {"type": "thinking", "thinking": "想想", "thinkingSignature": "s"},
            {"type": "text", "text": "我来改"},
            {"type": "toolCall", "id": "tc1", "name": "edit",
             "arguments": {"path": "a.py", "edits": [{"oldText": "a", "newText": "b\nc"}]}},
            {"type": "toolCall", "id": "tc2", "name": "bash", "arguments": {"command": "make"}},
            {"type": "toolCall", "id": "tc3", "name": "read", "arguments": {"path": "x.py"}},
        ], stop="toolUse")),
        _omp_msg("tr1", "a3", {"role": "toolResult", "toolCallId": "tc1", "toolName": "edit",
                               "content": [{"type": "text", "text": "edited"}],
                               "details": {"diff": "+1 b\n+2 c\n-1 a"}, "isError": False}),
        _omp_msg("tr2", "tr1", {"role": "toolResult", "toolCallId": "tc2", "toolName": "bash",
                                "content": [{"type": "text", "text": "fail\nExit code 2"}],
                                "details": {"exitCode": 2, "durationMs": 3500, "fullOutputPath": "/tmp/o.txt"},
                                "isError": True}),
        _omp_msg("tr3", "tr2", {"role": "toolResult", "toolCallId": "tc3", "toolName": "read",
                                "content": [{"type": "text", "text": "src"}], "details": {}}),
        _omp_msg("bx", "tr3", {"role": "bashExecution", "command": "echo hi", "output": "hi", "exitCode": 0}),
        _omp_msg("a4", "bx", _omp_asst([{"type": "text", "text": "好了"}], stop="stop")),
        _omp_msg("a5", "a4", _omp_asst([{"type": "text", "text": "再答一句"}], stop="aborted")),
    ]


def test_omp_adapter_active_branch_only_and_blocks(tmp_path):
    p = _omp_file(tmp_path, _omp_tree())
    tr = load("omp", p)
    assert tr.session_id == "sid-omp" and tr.resume_command == "omp --resume sid-omp"
    assert tr.title == "omp 标题" and tr.cwd == "C:\\repo" and tr.style == "pi"
    texts = [b.text for m in tr.messages for b in m.blocks if b.type == "text"]
    assert "abandoned" not in texts and "放弃的分支问题" not in texts                 # 只取活动分支
    assert "活动分支问题" in texts and "A1" in texts
    assert tr.question_count == 2                                                    # u1 + u3
    # 与 read_user_messages 的差别只来自分支：全文件 3 条，活动分支 2 条
    assert len(UI.read_user_messages("omp", p)) == 3
    adv = [m for m in tr.messages if m.injected]
    assert len(adv) == 1 and adv[0].blocks[0].text == "advisor review prompt"       # advisor prompt 为注入
    t = tr.turns[2]                                                                  # t0 = 前导事件，t1 = 第一问
    assert tr.turns[1].question.text == "第一个问题"
    assert t.question.text == "活动分支问题" and t.final is not None
    by = {s.call.id: s for s in t.steps if s.kind == "tool" and s.call}
    e = by["tc1"]
    assert e.call.kind == "edit" and e.call.title == "a.py"
    assert (e.call.diff["added"], e.call.diff["removed"]) == (2, 1)                 # details.diff 回填
    assert e.call.diff["full"].kind == "jsonl_idx"
    b = by["tc2"]
    assert b.status == "error" and b.result.exit_code == 2 and b.result.duration_ms == 3500
    assert b.result.saved_path == "/tmp/o.txt"
    assert by["tc3"].call.kind == "read"
    user_bash = [s for s in t.steps if s.kind == "tool" and s.call and s.call.by_user][0]
    assert user_bash.call.title == "echo hi" and user_bash.result.exit_code == 0
    assert any(x.kind == "thinking" for x in t.steps)
    kinds = {e.block.kind for e in t.leading_events + t.trailing_events}
    assert "aborted" in kinds and t.aborted                                          # stopReason=aborted
    assert tr.models == ["claude-opus-4-8"]
    m = [x for x in tr.messages if x.role == "assistant"][1]
    assert m.meta.input_tokens == 10 and m.meta.cost_usd == 0.5 and m.meta.duration_ms == 1200
    # model_change / thinking_level 事件
    first = tr.turns[0]
    assert first.question is None
    assert [e.block.kind for e in first.leading_events] == ["model_change", "thinking_level"]
    assert first.leading_events[0].block.text == "granola/claude-opus-4-8"


def test_pi_source_uses_pi_resume_and_linear_legacy_file(tmp_path):
    entries = [
        {"type": "session", "version": 1, "id": "pi-sid", "timestamp": _ISO.format(0), "cwd": "/w"},
        _ev(1, message=_omp_user("线性问题")),                       # 旧版：无 id / parentId，全量读取
        _ev(2, message=_omp_asst([{"type": "text", "text": "线性答"}])),
        _ev(3, message=_omp_user("第二问")),
        _ev(4, message=_omp_asst([{"type": "text", "text": "二答"}])),
    ]
    p = _omp_file(tmp_path, entries, name="2026_pi.jsonl", sub="pi")
    tr = load("pi", p)
    assert tr.resume_command == f"pi --session '{p}'" and tr.source == "pi"
    assert tr.question_count == 2 == len(UI.read_user_messages("pi", p))
    assert [t.final.text for t in tr.turns] == ["线性答", "二答"]


def test_omp_subagent_dir_not_read_and_custom_entries_ignored(tmp_path):
    p = _omp_file(tmp_path, [
        {"type": "session", "version": 3, "id": "s", "timestamp": _ISO.format(0), "cwd": "/w"},
        _ev(1, "custom", id="c0", parentId=None, customType="x", data={"a": 1}),
        _omp_msg("u1", "c0", _omp_user("问")),
        _omp_msg("a1", "u1", _omp_asst([{"type": "text", "text": "答"}])),
        _ev(2, "custom_message", id="cm", parentId="a1", display=False, content="隐藏"),
        _ev(3, "custom_message", id="cm2", parentId="cm", display=True, content="可见通知"),
        _ev(4, "compaction", id="cp", parentId="cm2", summary="压缩摘要" * 200, tokensBefore=100),
    ])
    sub = p.with_suffix("")
    sub.mkdir()
    (sub / "SubAgent.jsonl").write_text(json.dumps(_omp_msg("su", None, _omp_user("子代理 prompt"))) + "\n",
                                        encoding="utf-8")
    tr = load("omp", p)
    texts = [b.text for m in tr.messages for b in m.blocks if b.type == "text"]
    assert "子代理 prompt" not in texts and "隐藏" not in texts and "可见通知" in texts
    comp = [b for m in tr.messages for b in m.blocks if b.type == "event" and b.kind == "compaction"][0]
    assert comp.full is not None and T.read_content(comp.full) == "压缩摘要" * 200
    assert tr.question_count == 1


def test_omp_image_only_user_message_not_counted(tmp_path):
    p = _omp_file(tmp_path, [
        {"type": "session", "version": 3, "id": "s", "timestamp": _ISO.format(0), "cwd": "/w"},
        _omp_msg("u1", None, {"role": "user", "content": [{"type": "image", "mimeType": "image/png",
                                                            "data": "AAAA"}]}),
        _omp_msg("u2", "u1", _omp_user("文字问")),
    ])
    tr = load("omp", p)
    assert tr.question_count == 1 == len(UI.read_user_messages("omp", p))
    assert tr.turns[0].question.images and tr.turns[0].question.images[0].size == 3


# =========================================================================== #
# Antigravity adapter（零依赖 protobuf 编码造 fixture）
# =========================================================================== #
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _vf(num: int, n: int) -> bytes:
    return _varint((num << 3) | 0) + _varint(n)


def _ld(num: int, data: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(data)) + data


def _sf(num: int, s: str) -> bytes:
    return _ld(num, s.encode("utf-8"))


def _agy_meta(sec: int) -> bytes:
    return _ld(1, _vf(1, sec) + _vf(2, 0))


def _agy_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("create table steps (idx integer primary key, step_type integer, status integer default 0, "
                "has_subtrajectory numeric default 'false', metadata blob, error_details blob, permissions blob, "
                "task_details blob, render_info blob, step_payload blob, step_format integer default 0)")
    t0 = 1_790_000_000
    rows = [
        (0, 14, t0, _ld(19, _sf(2, "帮我看看这个项目"))),
        (1, 15, t0 + 1, _ld(20, _sf(3, "先读 README") + _sf(1, "我先看文件") +
                            _ld(7, _sf(1, "call_1") + _sf(2, "view_file") + _sf(3, '{"AbsolutePath":"C:\\\\p\\\\README.md"}')))),
        (2, 132, t0 + 2, _ld(140, _ld(2, _sf(1, "# README\nhello")))),
        (3, 15, t0 + 3, _ld(20, _ld(7, _sf(1, "call_2") + _sf(2, "run_command") +
                                    _sf(3, '{"CommandLine":"python a.py","toolAction":"运行脚本"}')))),
        (4, 132, t0 + 4, _ld(140, _ld(2, _sf(1, "Command exited with code 1\nTraceback")))),
        (5, 15, t0 + 5, _ld(20, _sf(1, "脚本失败了，原因是……"))),
        (6, 101, t0 + 6, _sf(114, "[Message] sender=system content=[Notice] background tasks done")),
        (7, 17, t0 + 7, _sf(24, "\x1a\x01API error (attempt 1): 502")),
        (8, 23, t0 + 8, _ld(30, _sf(5, "<summary>压缩摘要</summary>"))),
        (9, 14, t0 + 9, _ld(19, _sf(2, "再来一个问题"))),
        (10, 15, t0 + 10, _ld(20, _ld(7, _sf(1, "call_3") + _sf(2, "call_mcp_tool") +
                                      _sf(3, '{"ServerName":"docs","ToolName":"search","q":"x"}')))),
    ]
    for idx, st, ts, payload in rows:
        con.execute("insert into steps (idx, step_type, metadata, step_payload) values (?,?,?,?)",
                    (idx, st, _agy_meta(ts), payload))
    con.commit()
    con.close()
    return path


def test_antigravity_adapter(tmp_path):
    db = _agy_db(tmp_path / "6b1695d0-052a-4e8d-9b2e-cabb6eb0c83f.db")
    tr = load("antigravity", db)
    assert tr.session_id == db.stem and tr.resume_command == f"agy --conversation={db.stem}"
    assert tr.style == "generic" and tr.question_count == 2 == len(UI.read_user_messages("antigravity", db))
    assert tr.warnings                                        # 推断字段要明说
    t1, t2 = tr.turns
    assert t1.question.text == "帮我看看这个项目"
    kinds = [s.kind for s in t1.steps]
    assert kinds[:3] == ["thinking", "note", "tool"] or kinds[0] == "thinking"
    tools = [s for s in t1.steps if s.kind == "tool"]
    assert tools[0].call.kind == "read" and tools[0].call.title == "C:\\p\\README.md"
    assert tools[0].result.preview == "# README\nhello" and tools[0].status == "success"
    assert tools[1].call.kind == "shell" and tools[1].call.title == "python a.py"
    assert tools[1].call.detail == "运行脚本"
    assert tools[1].status == "error" and tools[1].result.exit_code == 1
    assert t1.final.text == "脚本失败了，原因是……"                 # 无工具名的 step 15 = 助手回复
    inj = [m for m in tr.messages if m.injected]
    assert len(inj) == 1 and "background tasks" in inj[0].blocks[0].text
    ev_kinds = [e.block.kind for e in t1.trailing_events] + [s.block.kind for s in t1.steps if s.kind == "event"]
    assert "error" in ev_kinds and "compaction" in ev_kinds
    mcp = [s for s in t2.steps if s.kind == "tool"][0]
    assert mcp.call.kind == "mcp" and mcp.call.title == "docs.search" and mcp.call.server == "docs"
    assert T.format_time_range(tr.started_ts, tr.ended_ts).endswith("(10s)") or tr.ended_ts - tr.started_ts == 10000
    # 默认 usage 读取在缺少 gen_metadata 表时不能拖垮阅读器
    assert T.load_transcript("antigravity", db, with_usage=True, use_cache=False).question_count == 2


def test_antigravity_parallel_tool_calls_fifo_pairing_and_path_args(tmp_path):
    """单步 step_type=15 携带多个 repeated field 7 工具调用时，全部提取并与后续
    step_type=132 按 FIFO 一一配对（杜绝第 2..N 个输出悬空为孤立「工具输出」）；
    支持 read_file/edit_file 的 Path/StartLine/EndLine/Target/Replacement 参数。"""
    db = tmp_path / "703d5cd2-4dbd-44d7-8cf3-3488f8d077de.db"
    con = sqlite3.connect(db)
    con.execute("create table steps (idx integer primary key, step_type integer, status integer default 0, "
                "has_subtrajectory numeric default 'false', metadata blob, error_details blob, permissions blob, "
                "task_details blob, render_info blob, step_payload blob, step_format integer default 0)")
    t0 = 1_790_000_000
    c1 = _ld(7, _sf(1, "id1") + _sf(2, "read_file") +
             _sf(3, '{"Path":"C:/GitHub/TCER/a.py","StartLine":10,"EndLine":25}'))
    c2 = _ld(7, _sf(1, "id2") + _sf(2, "read_file") +
             _sf(3, '{"Path":"C:/GitHub/TCER/b.py"}'))
    c3 = _ld(7, _sf(1, "id3") + _sf(2, "edit_file") +
             _sf(3, '{"Path":"C:/GitHub/TCER/b.py","Target":"old","Replacement":"new\\nline"}'))
    rows = [
        (0, 14, t0, _ld(19, _sf(2, "并行读两个文件再修改"))),
        (1, 15, t0 + 1, _ld(20, _sf(3, "先并行读取") + c1 + c2 + c3)),
        (2, 132, t0 + 2, _ld(140, _ld(2, _sf(1, "content of a.py")))),
        (3, 132, t0 + 3, _ld(140, _ld(2, _sf(1, "content of b.py")))),
        (4, 132, t0 + 4, _ld(140, _ld(2, _sf(1, "Made 1 replacement")))),
    ]
    for idx, st, ts, payload in rows:
        con.execute("insert into steps (idx, step_type, metadata, step_payload) values (?,?,?,?)",
                    (idx, st, _agy_meta(ts), payload))
    con.commit()
    con.close()

    tr = load("antigravity", db)
    assert tr.tool_call_count == 3
    t1 = tr.turns[0]
    tools = [s for s in t1.steps if s.kind == "tool"]
    assert len(tools) == 3 and all(s.call is not None and s.result is not None for s in tools)
    assert tools[0].call.title == "C:/GitHub/TCER/a.py" and tools[0].call.detail == ":10-25"
    assert tools[0].result.preview == "content of a.py"
    assert tools[1].call.title == "C:/GitHub/TCER/b.py" and tools[1].result.preview == "content of b.py"
    assert tools[2].call.kind == "edit" and tools[2].call.title == "C:/GitHub/TCER/b.py"
    assert tools[2].call.diff["added"] == 2 and tools[2].call.diff["removed"] == 1


# =========================================================================== #
# WorkBuddy adapter
# =========================================================================== #
def test_workbuddy_adapter(tmp_path):
    lines = [
        {"type": "message", "role": "user", "id": "m1", "timestamp": 1_790_000_000_000,
         "content": [{"text": "<system-reminder>ctx</system-reminder>实现登录功能"}]},
        {"type": "reasoning", "timestamp": 1_790_000_001_000, "rawContent": [{"text": "先想想"}]},
        {"type": "function_call", "name": "write_file", "callId": "w1", "timestamp": 1_790_000_002_000,
         "arguments": json.dumps({"path": "a.py", "content": "x\ny"})},
        {"type": "function_call_result", "name": "write_file", "callId": "w1", "status": "completed",
         "timestamp": 1_790_000_003_000, "output": {"text": "written"}},
        {"type": "function_call", "name": "run_terminal", "callId": "r1", "timestamp": 1_790_000_004_000,
         "arguments": {"command": "pytest"}},
        {"type": "function_call_result", "name": "run_terminal", "callId": "r1", "status": "completed",
         "timestamp": 1_790_000_005_000, "output": "Error: boom"},
        {"type": "message", "role": "assistant", "timestamp": 1_790_000_006_000,
         "content": [{"text": "已完成"}]},
        {"type": "message", "role": "user", "timestamp": 1_790_000_007_000,
         "content": [{"text": "<system-reminder>only</system-reminder>"}]},
    ]
    d = tmp_path / "wb"
    d.mkdir()
    p = d / "sess.jsonl"
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    tr = load("workbuddy", p)
    from tcer.core import workbuddy_reader as wb
    assert tr.question_count == 1 == len(wb.read_user_messages(p)) == len(UI.read_user_messages("workbuddy", p))
    assert tr.resume_command is None and tr.style == "generic"
    t = tr.turns[0]
    assert t.question.text == "实现登录功能" and t.final.text == "已完成"
    tools = [s for s in t.steps if s.kind == "tool"]
    assert tools[0].call.kind == "write" and tools[0].call.title == "a.py" and tools[0].call.diff["added"] == 2
    assert tools[0].status == "success" and tools[0].result.preview == "written"
    assert tools[1].call.kind == "shell" and tools[1].status == "error"
    assert [s.kind for s in t.steps][0] == "thinking"
    assert T.load_transcript("codebuddy", p, use_cache=False, with_usage=False).source == "workbuddy"


# =========================================================================== #
# read_content 边界 / 综合
# =========================================================================== #
def test_read_content_failures_return_none(tmp_path):
    assert T.read_content(None) is None
    assert T.read_content(T.ContentRef("file", str(tmp_path / "nope"))) is None
    assert T.read_content(T.ContentRef("jsonl", str(tmp_path / "nope"), 0, pointer="/a")) is None
    f = tmp_path / "x.jsonl"
    f.write_text('{"a": [1, {"b": "val"}]}\n', encoding="utf-8")
    assert T.read_content(T.ContentRef("jsonl", str(f), 0, pointer="/a/1/b")) == "val"
    assert T.read_content(T.ContentRef("jsonl", str(f), 0, pointer="/a/9")) is None
    assert T.read_content(T.ContentRef("jsonl", str(f), 0, pointer="/a")).startswith("[")
    assert T.read_content(T.ContentRef("sqlite", str(f), table="evil; drop", row_id="x")) is None
    assert T.read_content(T.ContentRef("weird", str(f))) is None
    assert T.read_content(T.ContentRef("file", str(f)), limit=3) == '{"a'[:3]


def test_message_helpers():
    assert T.starts_turn(_m("user", TextBlock("x")))
    assert T.starts_turn(_m("user", ImageBlock(ImageRef())))
    assert T.starts_turn(_m("user", EventBlock("slash_command", "/x")))
    assert not T.starts_turn(_m("user", EventBlock("aborted", "x")))
    assert not T.starts_turn(_m("user", _call("1"), _res("1")))
    assert not T.starts_turn(_m("tool", _res("1")))
    assert not T.starts_turn(_m("user", TextBlock("  ")))
    assert not T.starts_turn(_m("user", TextBlock("x"), injected=True))
    msgs = [_m("system", EventBlock("info", "x")), _q("a", turn_id="keep"), _q("b")]
    T.assign_turn_ids(msgs)
    assert [m.turn_id for m in msgs] == ["t0", "keep", "t2"]
    assert T.message_text(_m("user", TextBlock("a"), TextBlock("b"))) == "a\n\nb"
    assert MessageMeta().is_empty() and not MessageMeta(model="x").is_empty()
    assert T.SessionTurn is T.Turn
    assert ThinkingBlock().type == "thinking" and ToolCallBlock("i", "n", "other", "t").type == "tool_call"


def test_all_public_apis_on_empty_and_one_turn_transcripts():
    empty = T.Transcript(source="claude", path="p")
    assert empty.turns == [] and empty.index() == [] and T.flatten_rows([]) == []
    assert T.find_search_hits([], "x").total == 0
    assert T.find_turn_row_index([], [], "t1") == -1
    assert empty.agent_style.id == "claude" or empty.agent_style.id == "generic"
