"""性能优化的等价性护栏：快路径必须与慢路径（独立实现）逐字段一致。

- omp/pi 单遍扫描 ``scan_file`` / ``scan_folded`` == 独立的 ``read_session_meta`` 未缓存实现 /
  ``aggregate_usage`` / ``_loc_scan``（audit 用后两者交叉验证）。
- ``parse_timestamp_ms`` 对各种输入形态与旧实现逐值一致。
- ``file_cache`` 键在 mtime/size 变化时失效。
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

import pytest

from tcer.core import file_cache, omp_reader
from tcer.core.models import TokenUsage, ToolOp, TurnStat
from tcer.core.reader import parse_timestamp_ms


def _jl(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _entries(tag: str) -> list[dict]:
    ts = "2026-05-01T10:00:00.000Z"
    return [
        {"type": "session", "id": f"sid-{tag}", "cwd": "C:\\repo\\app", "title": "t", "timestamp": ts},
        {"type": "model_change", "model": "anthropic/claude-opus-4-8"},
        {"type": "message", "timestamp": ts,
         "message": {"role": "user", "content": "please fix the bug"}},
        {"type": "message", "timestamp": "2026-05-01T10:00:05.000Z", "message": {
            "role": "assistant", "model": "claude-opus-4-8", "duration": 1200,
            "content": [
                {"type": "toolCall", "id": f"c1{tag}", "name": "write",
                 "arguments": {"path": "a.py", "content": "x = 1\ny = 2\n"}},
                {"type": "toolCall", "id": f"c2{tag}", "name": "read", "arguments": {"path": "a.py"}},
            ],
            "usage": {"input": 10, "output": 20, "cacheRead": 5, "cacheWrite": 3,
                      "cost": {"total": 0.01}}}},
        {"type": "message", "timestamp": "2026-05-01T10:00:06.000Z", "message": {
            "role": "toolResult", "toolCallId": f"c1{tag}", "toolName": "write",
            "details": {"resolvedPath": "a.py"}, "content": []}},
        {"type": "message", "timestamp": "2026-05-01T10:00:07.000Z", "message": {
            "role": "toolResult", "toolCallId": f"c2{tag}", "toolName": "read",
            "isError": True, "content": []}},
        {"type": "message", "timestamp": "2026-05-01T10:01:00.000Z",
         "message": {"role": "user", "content": "no, that's wrong, redo it"}},
        {"type": "message", "timestamp": "2026-05-01T10:01:09.000Z", "message": {
            "role": "assistant", "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input": 7, "output": 9}}},
    ]


def _dump(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: _dump(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if isinstance(o, dict):
        return {k: _dump(v) for k, v in o.items()}
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, (list, tuple)):
        return [_dump(x) for x in o]
    return o


def test_omp_scan_folded_matches_independent_paths(tmp_path):
    file_cache.clear()
    main = _jl(tmp_path / "s" / "2026_u1.jsonl", _entries("m"))
    _jl(tmp_path / "s" / "2026_u1" / "SubAgent.jsonl", _entries("s"))

    u_fast, (sloc_fast, sig_fast) = omp_reader.scan_folded(main)
    u_slow = omp_reader.aggregate_usage(main)
    sloc_slow, sig_slow = omp_reader._loc_scan(main)
    assert _dump(u_fast) == _dump(u_slow)
    assert _dump(sloc_fast) == _dump(sloc_slow)
    assert sig_fast == sig_slow is True

    meta_fast = omp_reader.read_session_meta(main)
    meta_slow = omp_reader._read_session_meta_uncached(main)
    assert _dump(meta_fast) == _dump(meta_slow)


def test_omp_scan_file_subagent_flag_is_separate_cache_entry(tmp_path):
    file_cache.clear()
    p = _jl(tmp_path / "x.jsonl", _entries("m"))
    main_u = omp_reader.scan_file(p)[1]
    sub_u = omp_reader.scan_file(p, is_subagent=True)[1]
    assert main_u.slash_command_count + main_u.correction_msg_count >= 1
    assert main_u.correction_msg_count == 1
    assert sub_u.correction_msg_count == 0 and sub_u.first_prompt_chars == 0
    assert _dump(sub_u) == _dump(omp_reader._aggregate_single(p, is_subagent=True))


def test_omp_scan_invalidates_on_file_change(tmp_path):
    file_cache.clear()
    p = _jl(tmp_path / "x.jsonl", _entries("m"))
    before = omp_reader.scan_file(p)[1].output_tokens
    _jl(p, _entries("m") + [{"type": "message", "timestamp": "2026-05-01T10:02:00.000Z", "message": {
        "role": "assistant", "model": "claude-opus-4-8",
        "content": [], "usage": {"input": 1, "output": 100}}}])
    assert omp_reader.scan_file(p)[1].output_tokens == before + 100


def _old_parse(value):
    def ms(n):
        return n if n > 1_000_000_000_000 else n * 1000
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return ms(int(value))
    if isinstance(value, str):
        s = value.strip()
        try:
            return ms(int(s))
        except ValueError:
            pass
        try:
            return ms(int(float(s)))
        except ValueError:
            pass
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, OSError, OverflowError):
            return None
    return None


@pytest.mark.parametrize("v", [
    None, True, False, 0, 1, 1.5, 1_700_000_000, 1_700_000_000_123, "", " ", "12", " 12 ",
    "1e3", "1_000", "nan", "-inf", "+5", "1.7e12", "abc", "12:00", "[]",
    "2026-01-01", "2026-01-01T00:00:00", "2026-01-01T00:00:00Z", "2026-05-01T10:00:00.123+08:00",
    "2026-05-01 10:00:00", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z",
    "9999999999999999999999", [], {},
])
def test_parse_timestamp_ms_matches_legacy(v):
    for _ in range(2):  # 第二次走 lru_cache
        try:
            expect = ("ok", _old_parse(v))
        except BaseException as e:  # noqa: BLE001 - 比对异常类型
            expect = ("exc", type(e).__name__)
        try:
            got = ("ok", parse_timestamp_ms(v))
        except BaseException as e:  # noqa: BLE001
            got = ("exc", type(e).__name__)
        assert got == expect


def test_merge_rebases_turns_and_skips_empty_other():
    a = TokenUsage(user_msgs=2)
    a.tool_ops = [ToolOp(0, "Read", "x"), ToolOp(3, "Edit", "x")]
    a.turn_stats = [TurnStat(turn=0, ts=1), TurnStat(turn=5, ts=2)]
    b = TokenUsage(user_msgs=1)
    b.tool_ops = [ToolOp(0, "Grep", "")]
    b.turn_stats = [TurnStat(turn=0, ts=3, user_turn=1)]
    m = a.merge(b)
    assert [o.turn for o in m.tool_ops] == [0, 3, 4]
    assert [t.turn for t in m.turn_stats] == [0, 5, 6]
    assert m.turn_stats[-1].user_turn == 3
    # other 为空：不应触碰/重排 self 的列表内容
    e = a.merge(TokenUsage())
    assert [o.turn for o in e.tool_ops] == [0, 3]
    assert [t.turn for t in e.turn_stats] == [0, 5]
    # self 为空：other 回合号从 0 开始
    f = TokenUsage().merge(b)
    assert [o.turn for o in f.tool_ops] == [0]
    assert [t.turn for t in f.turn_stats] == [0]
