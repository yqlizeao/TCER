"""Tests for reader.py — ported-logic correctness + usage aggregation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcer.core import reader

FIXTURE = Path(__file__).parent / "fixtures" / "sample.jsonl"


def write_session(tmp_path: Path, lines: list[dict]) -> Path:
    p = tmp_path / "s.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return p


def _usage(i=0, cw=0, cr=0, o=0) -> dict:
    return {"input_tokens": i, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "output_tokens": o}


def _assistant(usage, model="claude-opus-4-8", ts="2026-03-06T10:00:00Z", msg_id=None) -> dict:
    """Create an assistant message dict.

    Args:
        msg_id: None = omit field, "" = empty string edge case, "msg_X" = real id
    """
    msg = {"role": "assistant", "model": model,
           "content": [{"type": "text", "text": "x"}], "usage": usage}
    if msg_id is not None:
        msg["id"] = msg_id
    return {"type": "assistant", "timestamp": ts, "message": msg}


def test_parent_session_id_main_and_subagent():
    main = Path("/c/.claude/projects/hash/SID-123.jsonl")
    sub = Path("/c/.claude/projects/hash/SID-123/subagents/agent-abc.jsonl")
    assert reader.parent_session_id(main) == "SID-123"          # main → own stem
    assert reader.parent_session_id(sub) == "SID-123"           # subagent → parent dir
    assert reader.is_subagent(sub) and not reader.is_subagent(main)


def test_session_artifacts_from_main_and_subagent():
    main = Path("/c/.claude/projects/hash/SID-123.jsonl")
    sub = Path("/c/.claude/projects/hash/SID-123/subagents/agent-abc.jsonl")
    # Both the main file and a subagent file resolve to the same identity/targets.
    for p in (main, sub):
        sid, main_jsonl, session_dir = reader.session_artifacts(p)
        assert sid == "SID-123"
        assert main_jsonl == Path("/c/.claude/projects/hash/SID-123.jsonl")
        assert session_dir == Path("/c/.claude/projects/hash/SID-123")


def _make_session_on_disk(proj: Path, sid: str) -> Path:
    """Lay out a main file + subagents/ + tool-results/ like Claude Code does."""
    proj.mkdir(parents=True, exist_ok=True)
    main = proj / f"{sid}.jsonl"
    main.write_text('{"type":"assistant"}\n', encoding="utf-8")
    sub_dir = proj / sid / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-x.jsonl").write_text("{}\n", encoding="utf-8")
    (sub_dir / "agent-x.meta.json").write_text("{}\n", encoding="utf-8")
    tr_dir = proj / sid / "tool-results"
    tr_dir.mkdir(parents=True)
    (tr_dir / "call_1.json").write_text("{}\n", encoding="utf-8")
    return main


def test_delete_session_removes_main_and_subagent_tree(tmp_path: Path):
    proj = tmp_path / "hash"
    main = _make_session_on_disk(proj, "SID-A")
    # A second session must survive untouched.
    other = _make_session_on_disk(proj, "SID-B")

    removed = reader.delete_session(main)

    assert not main.exists()
    assert not (proj / "SID-A").exists()       # subagents + tool-results gone
    assert set(removed) == {main, proj / "SID-A"}
    # Neighbour session is intact.
    assert other.exists()
    assert (proj / "SID-B" / "subagents" / "agent-x.jsonl").exists()


def test_delete_session_via_subagent_path(tmp_path: Path):
    proj = tmp_path / "hash"
    _make_session_on_disk(proj, "SID-A")
    sub = proj / "SID-A" / "subagents" / "agent-x.jsonl"

    removed = reader.delete_session(sub)

    assert not (proj / "SID-A.jsonl").exists()
    assert not (proj / "SID-A").exists()
    assert len(removed) == 2


def test_delete_session_orphan_subagent_only(tmp_path: Path):
    """No main file (orphan subagents): only the directory is removed, no error."""
    proj = tmp_path / "hash"
    sub_dir = proj / "SID-A" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-x.jsonl").write_text("{}\n", encoding="utf-8")

    removed = reader.delete_session(sub_dir / "agent-x.jsonl")

    assert not (proj / "SID-A").exists()
    assert removed == [proj / "SID-A"]



def test_aggregate_dedupes_by_message_id(tmp_path):
    """One API response split across content-block lines (same message.id + usage)
    must be counted ONCE, not per line (observed up to 6× on Bedrock sessions)."""
    u = _usage(i=2, cw=157721, cr=0, o=722)
    lines = [
        {"type": "assistant", "timestamp": "2026-03-06T10:00:00Z",
         "message": {"role": "assistant", "id": "msg_X", "model": "m",
                     "content": [{"type": "thinking", "thinking": "…"}], "usage": u}},
        {"type": "assistant", "timestamp": "2026-03-06T10:00:01Z",
         "message": {"role": "assistant", "id": "msg_X", "model": "m",
                     "content": [{"type": "text", "text": "hi"}], "usage": u}},
        {"type": "assistant", "timestamp": "2026-03-06T10:00:02Z",
         "message": {"role": "assistant", "id": "msg_X", "model": "m",
                     "content": [{"type": "tool_use", "name": "Edit"}], "usage": u}},
        # a genuinely different response
        {"type": "assistant", "timestamp": "2026-03-06T10:01:00Z",
         "message": {"role": "assistant", "id": "msg_Y", "model": "m",
                     "content": [{"type": "text", "text": "bye"}], "usage": _usage(i=10, o=20)}},
    ]
    agg = reader.aggregate_usage(write_session(tmp_path, lines))
    assert agg.assistant_msgs == 2                       # not 4
    assert agg.output_tokens == 722 + 20                 # each response once
    assert agg.cache_creation_input_tokens == 157721     # not ×3


def test_aggregate_tool_use_on_continuation_line_counted(tmp_path):
    """A tool_use block on a continuation line (same message.id as the text line)
    must still be counted. Claude Code writes one content block per JSONL line,
    so the tool_use almost always lives on a *later* line than the first —
    deduping it away loses ~78% of tool calls in real sessions.
    """
    u = _usage(i=2, o=722)
    lines = [
        # response 1: text on line 1, Edit on line 2 (same id) + Bash on line 3
        {"type": "assistant", "timestamp": "2026-03-06T10:00:00Z",
         "message": {"role": "assistant", "id": "msg_X", "model": "m",
                     "content": [{"type": "text", "text": "hi"}], "usage": u}},
        {"type": "assistant", "timestamp": "2026-03-06T10:00:01Z",
         "message": {"role": "assistant", "id": "msg_X", "model": "m",
                     "content": [{"type": "tool_use", "name": "Edit",
                                  "id": "c1", "input": {"file_path": "/a.py"}}],
                     "usage": u}},
        {"type": "assistant", "timestamp": "2026-03-06T10:00:02Z",
         "message": {"role": "assistant", "id": "msg_X", "model": "m",
                     "content": [{"type": "tool_use", "name": "Bash",
                                  "id": "c2", "input": {}}], "usage": u}},
    ]
    agg = reader.aggregate_usage(write_session(tmp_path, lines))

    # tokens counted once (dedup), but BOTH tool calls captured
    assert agg.assistant_msgs == 1
    assert agg.output_tokens == 722
    assert agg.tool_calls == {"Edit": 1, "Bash": 1}
    assert len(agg.tool_ops) == 2
    assert [(op.tool, op.path) for op in agg.tool_ops] == [("Edit", "/a.py"), ("Bash", "")]
    # both tool_ops share response 1's turn number (frozen per response)
    assert {op.turn for op in agg.tool_ops} == {0}


def test_aggregate_tool_use_turn_frozen_per_response(tmp_path):
    """Tool ops from different responses get distinct, sequential turns; all
    blocks within one response share its turn. Guards the search→edit window
    (metrics uses strict op.turn < edit_turn <= op.turn+WINDOW).
    """
    u = _usage(i=1, o=1)
    lines = [
        # response 1 (msg_A): Grep on continuation line
        {"type": "assistant", "timestamp": "2026-03-06T10:00:00Z",
         "message": {"role": "assistant", "id": "msg_A", "model": "m",
                     "content": [{"type": "text", "text": "x"}], "usage": u}},
        {"type": "assistant", "timestamp": "2026-03-06T10:00:01Z",
         "message": {"role": "assistant", "id": "msg_A", "model": "m",
                     "content": [{"type": "tool_use", "name": "Grep",
                                  "id": "c1", "input": {}}], "usage": u}},
        # response 2 (msg_B): Edit on continuation line
        {"type": "assistant", "timestamp": "2026-03-06T10:01:00Z",
         "message": {"role": "assistant", "id": "msg_B", "model": "m",
                     "content": [{"type": "text", "text": "y"}], "usage": u}},
        {"type": "assistant", "timestamp": "2026-03-06T10:01:01Z",
         "message": {"role": "assistant", "id": "msg_B", "model": "m",
                     "content": [{"type": "tool_use", "name": "Edit",
                                  "id": "c2", "input": {"file_path": "/x.py"}}],
                     "usage": u}},
    ]
    agg = reader.aggregate_usage(write_session(tmp_path, lines))
    turns = [op.turn for op in agg.tool_ops]
    assert turns == [0, 1]                       # Grep=turn0 (resp1), Edit=turn1 (resp2)


# --------------------------------------------------------------------------- #
# message.id edge-case boundary tests
# --------------------------------------------------------------------------- #
def test_aggregate_empty_string_id_treated_as_unique(tmp_path):
    """Empty string id should be treated as 'no id' and counted individually.

    Risk: If empty string is added to `seen` set, subsequent empty-id messages
    would be incorrectly skipped.
    """
    u = _usage(i=10, cw=0, cr=0, o=5)
    lines = [
        _assistant(u, msg_id=""),
        _assistant(u, msg_id=""),  # Same empty id
        _assistant(_usage(i=20, o=10), msg_id="msg_real"),  # Different real id
    ]
    agg = reader.aggregate_usage(write_session(tmp_path, lines))

    # Both empty-id messages should be counted (not deduped)
    assert agg.assistant_msgs == 3
    assert agg.input_tokens == 10 + 10 + 20  # All three counted
    assert agg.output_tokens == 5 + 5 + 10


def test_aggregate_none_id_fallback_to_individual(tmp_path):
    """Messages without message.id should be counted individually (backward compat)."""
    u = _usage(i=10, o=5)
    lines = [
        _assistant(u),  # No msg_id field (omitted)
        _assistant(u),  # No msg_id field (omitted)
        _assistant(u, msg_id="msg_real"),  # Has id
    ]
    agg = reader.aggregate_usage(write_session(tmp_path, lines))

    # All three should be counted (no dedup for missing ids)
    assert agg.assistant_msgs == 3
    assert agg.input_tokens == 30


def test_aggregate_mixed_id_and_no_id(tmp_path):
    """Mixed scenario: messages with id dedup, without id counted individually."""
    u = _usage(i=10, o=5)
    lines = [
        # Group A: Same id (should dedup)
        _assistant(u, msg_id="msg_A"),
        _assistant(u, msg_id="msg_A"),  # Duplicate, should skip
        # Group B: No id (should count individually)
        _assistant(u),  # No id
        _assistant(u),  # No id, but no dedup (counted)
        # Group C: Different id
        _assistant(u, msg_id="msg_B"),
    ]
    agg = reader.aggregate_usage(write_session(tmp_path, lines))

    # Expected: msg_A(1) + no_id(2) + msg_B(1) = 4
    assert agg.assistant_msgs == 4
    assert agg.input_tokens == 40  # 4 × 10


# --------------------------------------------------------------------------- #
# aggregate_usage
# --------------------------------------------------------------------------- #
def test_aggregate_sums_four_token_fields(tmp_path):
    p = write_session(tmp_path, [
        _assistant(_usage(2, 43447, 0, 1021)),
        _assistant(_usage(2, 1069, 43447, 1473), model="claude-sonnet-4-6"),
    ])
    u = reader.aggregate_usage(p)
    assert u.input_tokens == 4
    assert u.cache_creation_input_tokens == 43447 + 1069
    assert u.cache_read_input_tokens == 43447
    assert u.output_tokens == 1021 + 1473
    assert u.assistant_msgs == 2
    assert u.models == {"claude-opus-4-8", "claude-sonnet-4-6"}


def test_aggregate_skips_meta_and_empty_usage(tmp_path):
    p = write_session(tmp_path, [
        _assistant(_usage(10, 0, 0, 5)),
        {"isMeta": True, "type": "summary", "summary": "skip me"},
        _assistant(_usage(0, 0, 0, 0)),  # all-zero → skipped, not counted in assistant_msgs
    ])
    u = reader.aggregate_usage(p)
    assert u.assistant_msgs == 1          # only real-usage turns
    assert u.empty_usage_skipped == 1
    assert u.effective_turns == 1         # == assistant_msgs
    assert u.input_tokens == 10 and u.output_tokens == 5


def test_aggregate_fixture_matches_expected():
    u = reader.aggregate_usage(FIXTURE)
    # three assistant turns (a1, a2, a3); a3 has all-zero usage
    assert u.input_tokens == 4
    assert u.cache_creation_input_tokens == 43447 + 1069
    assert u.cache_read_input_tokens == 43447
    assert u.output_tokens == 1021 + 1473
    assert u.assistant_msgs == 2           # only real-usage turns (a1, a2)
    assert u.empty_usage_skipped == 1
    assert u.effective_turns == 2          # == assistant_msgs
    assert u.models == {"claude-opus-4-8", "claude-sonnet-4-6"}


# --------------------------------------------------------------------------- #
# parse_timestamp_ms — three formats
# --------------------------------------------------------------------------- #
def test_timestamp_ms_int_seconds_vs_millis():
    assert reader.parse_timestamp_ms(1781061650) == 1781061650000      # seconds
    assert reader.parse_timestamp_ms(1781061650000) == 1781061650000   # already ms


def test_timestamp_rfc3339_string():
    assert reader.parse_timestamp_ms("1970-01-01T00:00:01Z") == 1000


def test_timestamp_garbage_returns_none():
    assert reader.parse_timestamp_ms(None) is None
    assert reader.parse_timestamp_ms("not a date") is None
    assert reader.parse_timestamp_ms(True) is None


# --------------------------------------------------------------------------- #
# extract_text — ported from cc-switch
# --------------------------------------------------------------------------- #
def test_extract_text_tool_use_shows_name():
    assert reader.extract_text([{"type": "tool_use", "name": "Bash"}]) == "[Tool: Bash]"


def test_extract_text_string_and_array():
    assert reader.extract_text("hello") == "hello"
    assert reader.extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"


# --------------------------------------------------------------------------- #
# read_session_meta + is_subagent
# --------------------------------------------------------------------------- #
def test_session_meta_extracts_id_cwd_title():
    meta = reader.read_session_meta(FIXTURE)
    assert meta.session_id == "sess-001"
    assert meta.cwd == "/tmp/project"
    assert meta.title == "请帮我重构这个函数"
    assert meta.is_subagent is False


def test_session_meta_tail_title_beats_stale_head_title(tmp_path):
    """A newer ai-title in the tail must win over an older one in the head.

    Claude Code rewrites the title as the conversation grows; the freshest one
    lives furthest down the file. Regression guard for the head-overwrites-tail bug.
    """
    lines: list[dict] = [
        {"type": "user", "sessionId": "sess-x", "cwd": "/tmp/p",
         "message": {"role": "user", "content": "原始问题"}},
        {"type": "ai-title", "sessionId": "sess-x", "aiTitle": "旧标题"},
    ]
    # Pad past the head window so head (first 20) and tail (last 30) don't overlap.
    for _ in range(50):
        lines.append(_assistant(_usage(1, 0, 0, 1)))
    lines.append({"type": "ai-title", "sessionId": "sess-x", "aiTitle": "新标题"})
    meta = reader.read_session_meta(write_session(tmp_path, lines))
    assert meta.title == "新标题"
    assert meta.session_id == "sess-x"


def test_session_meta_falls_back_to_user_message_without_ai_title(tmp_path):
    """No ai-title anywhere → first real user message is used as the title."""
    lines = [
        {"type": "user", "sessionId": "sess-y", "cwd": "/tmp/p",
         "message": {"role": "user", "content": "重构这个函数"}},
        _assistant(_usage(1, 0, 0, 1)),
    ]
    meta = reader.read_session_meta(write_session(tmp_path, lines))
    assert meta.title == "重构这个函数"


def test_is_subagent_detection():
    assert reader.is_subagent(Path("/home/u/.claude/projects/p/subagents/ag.jsonl")) is True
    assert reader.is_subagent(Path("/home/u/.claude/projects/p/main.jsonl")) is False


def test_aggregate_handles_garbage_lines(tmp_path):
    p = tmp_path / "broken.jsonl"
    p.write_text("not json at all\n"
                 + json.dumps(_assistant(_usage(1, 0, 0, 1))) + "\n"
                 + "\n", encoding="utf-8")
    u = reader.aggregate_usage(p)
    assert u.assistant_msgs == 1


def test_scan_parses_system_signals_and_server_tool_use(tmp_path):
    """system/turn_duration、api_error(429)、compact_boundary 与 server_tool_use。"""
    import json as _json
    lines = [
        {"type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
         "message": {"role": "assistant", "id": "m1", "model": "claude-opus-4-8",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0,
                               "server_tool_use": {"web_search_requests": 2,
                                                   "web_fetch_requests": 1}}}},
        {"type": "system", "subtype": "turn_duration",
         "durationMs": 4321, "messageCount": 3},
        {"type": "system", "subtype": "api_error",
         "error": {"status": 429, "message": "rate limited"},
         "retryAttempt": 1},
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 150000}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(_json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u = reader.aggregate_usage(p)
    assert u.web_search_count == 3
    assert u.rate_limit_reached_count == 1
    assert "api-429" in u.rate_limit_names
    assert u.compaction_count == 1
    # 真实回合耗时回填到 turn_stats
    assert len(u.turn_stats) == 1
    assert u.turn_stats[0].duration_ms == 4321
    assert u.turn_stats[0].output_tokens == 5


def test_session_meta_line_metadata(tmp_path):
    """行级 version/gitBranch/effort/permissionMode → SessionMeta。"""
    import json as _json
    line = {"type": "user", "sessionId": "sid-1", "cwd": "/tmp/x",
            "version": "2.1.220", "gitBranch": "main", "effort": "high",
            "permissionMode": "bypassPermissions",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": "hello"}]}}
    p = tmp_path / "s.jsonl"
    p.write_text(_json.dumps(line) + "\n", encoding="utf-8")
    meta = reader.read_session_meta(p)
    assert meta.cli_version == "2.1.220"
    assert meta.git_branch == "main"
    assert meta.reasoning_effort == "high"
    assert meta.permission_profile == "bypassPermissions"


def test_structured_patch_diff_counters(tmp_path):
    """structuredPatch 的 +/- 行独立累计(仅代码文件),作回放 LOC 交叉校验。"""
    import json as _json
    lines = [
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}},
        {"type": "user",
         "toolUseResult": {"filePath": "a.py", "structuredPatch": [
             {"lines": ["+new1", "+new2", "-old1", " ctx"]},
             {"lines": ["+new3"]},
         ]},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
        # 非代码后缀不计
        {"type": "user",
         "toolUseResult": {"filePath": "img.png", "structuredPatch": [
             {"lines": ["+x", "-y"]}]},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t2"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(_json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u = reader.aggregate_usage(p)
    assert (u.patch_diff_added, u.patch_diff_deleted) == (3, 1)


def test_claude_third_batch_signals(tmp_path):
    """stop_hook_summary / queue-operation / MCP 归因。"""
    import json as _json
    lines = [
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}},
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "system", "subtype": "stop_hook_summary",
         "hookCount": 2, "hookErrors": ["boom"],
         "hookInfos": [{"durationMs": 1200}, {"durationMs": 300}]},
        {"type": "user", "attributionMcpServer": "monolith",
         "attributionMcpTool": "editor_query",
         "toolUseResult": {"stdout": "ok"},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(_json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u = reader.aggregate_usage(p)
    assert u.queued_input_count == 2
    assert (u.hook_run_count, u.hook_error_count, u.hook_duration_ms_total) == (2, 1, 1500)
    assert u.mcp_calls_by_attr == {"monolith/editor_query": 1}


def test_prompt_behavior_signals(tmp_path):
    """slash 命令 / 纠正措辞 / 首条消息长度(只计数,不存正文)。"""
    import json as _json

    def _user(text):
        return {"type": "user",
                "message": {"role": "user",
                            "content": [{"type": "text", "text": text}]}}
    lines = [
        _user("帮我实现一个解析器,要求如下:支持 JSONL"),   # 首条(19 字)
        _user("/compact"),                                  # slash
        _user("<command-name>/model</command-name>"),        # 命令面板
        _user("不对,重来,应该用差分"),                       # 纠正
        _user("好的继续"),                                   # 普通
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(_json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u = reader.aggregate_usage(p)
    assert u.slash_command_count == 2
    assert u.correction_msg_count == 1
    assert u.first_prompt_chars == len("帮我实现一个解析器,要求如下:支持 JSONL")
    assert u.user_msgs == 4  # command-name 是注入，不计入 user_msgs（slash 仍计 2）


def test_read_user_messages_filters_cc_injections(tmp_path):
    """Claude-Code-injected user-role texts must NOT appear in the popup.

    Before the fix ``_strip_tags`` ran BEFORE the ``<…>`` prefix check, which
    erased the tags first and let ~17% of popup rows be injections
    (task-notification, ide_opened_file, local-command-stdout, compact
    continuation, ESC marker, system-reminder…). The check must run on the
    RAW text. Guards read_user_messages, user_message_texts, and
    first_prompt_chars together.
    """
    import json as _json

    def _user(text):
        return {"type": "user",
                "message": {"role": "user",
                            "content": [{"type": "text", "text": text}]}}

    lines = [
        _user("<task-notification>a095\ncall_1\nC:\\Temp\\t</task-notification>"),
        _user("<ide_opened_file>The user opened the file Untitled-1 in the IDE.</ide_opened_file>"),
        _user("<local-command-stdout>Set model to Fable 5</local-command-stdout>"),
        _user("<command-name>/model</command-name>"),
        _user("[Request interrupted by user]"),
        _user("This session is being continued from a previous conversation that ran out of context."),
        _user("<system-reminder>Only use artifacts when explicitly asked.</system-reminder>"),
        _user("请帮我修复这个 bug"),   # the one real human message
        _assistant(_usage(10, 0, 0, 5)),
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(_json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    # Popup path: only the real message survives.
    assert reader.read_user_messages(p) == ["请帮我修复这个 bug"]

    # Cached path (aggregate_usage w/ texts) agrees.
    u = reader.aggregate_usage(p)
    assert u.user_message_texts == ["请帮我修复这个 bug"]
    # first_prompt_chars skips injections → the real message's length.
    assert u.first_prompt_chars == len("请帮我修复这个 bug")
    # user_msgs 也排除注入（task-notification 等非真人输入不计）→ 只剩 1 条真实。
    assert u.user_msgs == 1


def test_scan_skips_subagent_user_prompts(tmp_path):
    """子代理文件的 user 消息（Task 派发 prompt）不计 user_msgs / first_prompt。

    子代理是 Task 工具派生产物，其 user 消息是主代理下发的指令（非真人），
    并入父会话时不应算作用户消息；token/LOC 等真实成本仍照常累计。
    """
    import json as _json
    sub = tmp_path / "SID" / "subagents" / "agent.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(_json.dumps({"type": "user", "message": {"role": "user",
        "content": [{"type": "text", "text": "You are researching the local repo…"}]}}) + "\n",
        encoding="utf-8")
    u, _ = reader.scan_session(sub, with_loc=False)
    assert reader.is_subagent(sub) is True
    assert u.user_msgs == 0
    assert u.first_prompt_chars == 0


def test_scan_records_turn_net_locs_and_compaction_turns(tmp_path):
    """逐回合 LOC 流水 + 压缩回合位置（分段 TCER / 衰减分析的地基）。"""
    import json

    from tcer.core import reader as R

    def _asst(i, cid, tool, inp):
        return {"type": "assistant", "timestamp": 1_770_000_000_000 + i * 1000,
                "message": {"role": "assistant", "id": f"m{i}",
                            "usage": {"input_tokens": 10, "output_tokens": 5,
                                      "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0},
                            "content": [{"type": "tool_use", "id": cid, "name": tool,
                                         "input": inp}]}}

    def _res(cid):
        return {"type": "user",
                "message": {"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": cid}]}}

    lines = [
        _asst(0, "t1", "Write", {"file_path": "a.py", "content": "x\ny\nz"}),
        _res("t1"),
        {"type": "system", "subtype": "compact_boundary"},
        _asst(1, "t2", "Edit", {"file_path": "a.py", "old_string": "x",
                                "new_string": "x\np"}),
        _res("t2"),
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u, _ = R.scan_session(p, with_loc=True, include_user_texts=False)
    assert u.turn_net_locs == [(0, 3, 0), (1, 1, 0)]
    assert u.compaction_turns == [1]  # 边界之后的首个回合
    assert u.compaction_count == 1


def test_f1_correction_writes_back_to_turn_records(tmp_path):
    """originalFile F1 修正的行差回写进逐回合流水（Σ turn_net_locs = 净 LOC）。"""
    import json

    from tcer.core import reader as R

    lines = [
        {"type": "assistant",
         "message": {"role": "assistant", "id": "m1",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": "a.py",
                                            "content": "\n".join(f"l{i}" for i in range(10))}}]}},
        # 结果行：原文件其实有 8 行（覆写，净 +2 而非 +10）
        {"type": "user", "toolUseResult": {"filePath": "a.py",
                                           "originalFile": "\n".join(f"o{i}" for i in range(8))},
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
    ]
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    u, sloc = R.scan_session(p, with_loc=True, include_user_texts=False)
    assert sloc.added == 2
    net_by_turn = {}
    for turn, a, d in u.turn_net_locs:
        pa, pd = net_by_turn.get(turn, (0, 0))
        net_by_turn[turn] = (pa + a, pd + d)
    assert sum(a - d for a, d in net_by_turn.values()) == 2  # 流水与总量一致
