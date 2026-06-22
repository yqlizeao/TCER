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
