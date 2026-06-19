"""Tests for reader.py — ported-logic correctness + usage aggregation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tcer import reader

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


def _assistant(usage, model="claude-opus-4-8", ts="2026-03-06T10:00:00Z") -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "x"}], "usage": usage}}


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
        _assistant(_usage(0, 0, 0, 0)),  # all-zero → skipped + counted
    ])
    u = reader.aggregate_usage(p)
    assert u.assistant_msgs == 1
    assert u.empty_usage_skipped == 1
    assert u.input_tokens == 10 and u.output_tokens == 5


def test_aggregate_fixture_matches_expected():
    u = reader.aggregate_usage(FIXTURE)
    # two real turns (a1: 2/43447/0/1021, a2: 2/1069/43447/1473); a3 all-zero skipped
    assert u.input_tokens == 4
    assert u.cache_creation_input_tokens == 43447 + 1069
    assert u.cache_read_input_tokens == 43447
    assert u.output_tokens == 1021 + 1473
    assert u.assistant_msgs == 2
    assert u.empty_usage_skipped == 1
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
