"""估算输出速度 est_output_tps + 首字延迟中位数：各源采集口径与门槛。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tcer.core import codex_reader, metrics, omp_reader, opencode_reader, reader
from tcer.core.models import SessionMeta, TokenUsage


def _w(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- Claude

def _line(kind, uuid, parent, ts, **kw):
    return {"type": kind, "uuid": uuid, "parentUuid": parent, "timestamp": ts, **kw}


def _asst(uuid, parent, ts, mid, block, out, stop="tool_use"):
    row = _line("assistant", uuid, parent, ts, apiBlockIndex=block, message={
        "id": mid, "role": "assistant", "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "x"}], "stop_reason": stop,
        "usage": {"input_tokens": 1, "output_tokens": out}})
    if stop is None:
        row["message"].pop("stop_reason")
    return row


def _user(uuid, parent, ts, **kw):
    return _line("user", uuid, parent, ts, message={"role": "user", "content": "hi"}, **kw)


def test_claude_est_speed_basic_chain_and_attachment_skip(tmp_path):
    rows = [
        _user("u1", None, "2026-10-01T10:00:00.000Z"),
        # attachment 时间戳不可靠，必须跳过，起点取 u1
        _line("attachment", "a1", "u1", "2026-10-01T10:00:09.000Z", attachment={"type": "x"}),
        _asst("r1", "a1", "2026-10-01T10:00:04.000Z", "m1", 0, 400),
        _asst("r2", "a1", "2026-10-01T10:00:10.000Z", "m1", 1, 400),  # 同 id 后块 → 终点取最晚
    ]
    u, _ = reader.scan_session(_w(tmp_path / "s.jsonl", rows), use_cache=False)
    assert (u.est_gen_output_tokens, u.est_gen_duration_ms) == (400, 10_000)
    r = metrics.est_output_tps(u)
    assert abs(r - 40.0) < 1e-9


def test_claude_est_speed_rewind_replay_counts_once(tmp_path):
    base = [
        _user("u1", None, "2026-10-01T10:00:00.000Z"),
        _asst("r1", "u1", "2026-10-01T10:00:05.000Z", "m1", 0, 500),
    ]
    u, _ = reader.scan_session(_w(tmp_path / "s.jsonl", base + base), use_cache=False)
    assert (u.est_gen_output_tokens, u.est_gen_duration_ms) == (500, 5_000)


def test_claude_est_speed_gates(tmp_path):
    rows = [
        _user("u1", None, "2026-10-01T10:00:00.000Z"),
        # 输出不足 200
        _asst("r1", "u1", "2026-10-01T10:00:05.000Z", "m1", 0, 199),
        _user("u2", "r1", "2026-10-01T10:00:10.000Z"),
        # 无 stop_reason
        _asst("r2", "u2", "2026-10-01T10:00:15.000Z", "m2", 0, 900, stop=None),
        _user("u3", "r2", "2026-10-01T10:00:20.000Z"),
        # 见到的最早块不是首块（apiBlockIndex=1）
        _asst("r3", "u3", "2026-10-01T10:00:25.000Z", "m3", 1, 900),
        _user("u4", "r3", "2026-10-01T10:00:30.000Z"),
        # 耗时 < 1s
        _asst("r4", "u4", "2026-10-01T10:00:30.500Z", "m4", 0, 900),
        _user("u5", "r4", "2026-10-01T10:00:31.000Z"),
        # 父链断（父行不存在）
        _asst("r5", "ghost", "2026-10-01T10:00:36.000Z", "m5", 0, 900),
        _user("u6", "r5", "2026-10-02T10:00:00.000Z"),
        # 耗时 > 1h
        _asst("r6", "u6", "2026-10-02T12:00:00.000Z", "m6", 0, 900),
    ]
    u, _ = reader.scan_session(_w(tmp_path / "s.jsonl", rows), use_cache=False)
    assert (u.est_gen_output_tokens, u.est_gen_duration_ms) == (0, 0)
    assert metrics.est_output_tps(u) is None


def test_claude_est_speed_ismeta_parent_keeps_chain(tmp_path):
    rows = [
        _user("u1", None, "2026-10-01T10:00:00.000Z", isMeta=True),
        _asst("r1", "u1", "2026-10-01T10:00:04.000Z", "m1", 0, 400),
    ]
    u, _ = reader.scan_session(_w(tmp_path / "s.jsonl", rows), use_cache=False)
    assert (u.est_gen_output_tokens, u.est_gen_duration_ms) == (400, 4_000)
    assert u.assistant_msgs == 1


def test_est_samples_merge_and_pooled_ratio():
    a = TokenUsage(est_gen_output_tokens=400, est_gen_duration_ms=10_000)
    b = TokenUsage(est_gen_output_tokens=600, est_gen_duration_ms=10_000)
    m = a.merge(b)
    assert (m.est_gen_output_tokens, m.est_gen_duration_ms) == (1000, 20_000)
    assert metrics.est_output_tps(m) == 50.0  # Σ/Σ，不是 (40+60)/2 的逐条平均巧合


# ---------------------------------------------------------------- Codex

def _cx(ts, typ, payload):
    return {"timestamp": ts, "type": typ, "payload": payload}


def _tc(ts, total_out, last_out):
    return _cx(ts, "event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 100, "output_tokens": total_out},
        "last_token_usage": {"input_tokens": 100, "output_tokens": last_out}}})


def test_codex_est_speed_state_machine(tmp_path):
    rows = [
        _cx("2026-10-01T10:00:00.000Z", "event_msg", {"type": "task_started"}),
        _cx("2026-10-01T10:00:00.000Z", "turn_context", {"model": "gpt-5.2"}),
        _cx("2026-10-01T10:00:01.000Z", "response_item",
            {"type": "message", "role": "user", "content": []}),
        _cx("2026-10-01T10:00:03.000Z", "response_item", {"type": "reasoning"}),
        _cx("2026-10-01T10:00:11.000Z", "response_item",
            {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"}),
        # 工具结果先于 token_count 写出 → 终点取最后一个输出项（10:00:11）
        _cx("2026-10-01T10:00:15.000Z", "response_item",
            {"type": "function_call_output", "call_id": "c1", "output": "ok"}),
        _tc("2026-10-01T10:00:15.050Z", 500, 500),
        # 同一 last_token_usage 重复投递：total 不变 → 不是新请求
        _tc("2026-10-01T10:00:15.060Z", 500, 500),
        # 第二个请求：起点 = 上次 token_count/工具结果(10:00:15.050)；
        # 输出项 10:00:20 → token_count 10:00:25（无工具结果夹在中间 → 取 token_count）
        _cx("2026-10-01T10:00:20.000Z", "response_item",
            {"type": "message", "role": "assistant", "content": []}),
        _tc("2026-10-01T10:00:25.050Z", 1100, 600),
    ]
    u = codex_reader.aggregate_usage(_w(tmp_path / "rollout-2026-10-01T10-00-00-z.jsonl", rows))
    # 请求1: 1s(user msg 边界) → 11s = 10s，输出 500；请求2: 15.05 → 25.05 = 10s，输出 600
    assert u.est_gen_output_tokens == 1100
    assert u.est_gen_duration_ms == 20_000


def test_codex_ttft_samples_present_and_median(tmp_path):
    rows = [
        _cx("2026-10-01T10:00:01Z", "event_msg",
            {"type": "task_complete", "duration_ms": 10, "time_to_first_token_ms": ms})
        for ms in (100, 200, 9000)
    ]
    u = codex_reader.aggregate_usage(_w(tmp_path / "rollout-2026-10-01T10-00-00-y.jsonl", rows))
    assert u.time_to_first_token_ms == 100          # 字段语义仍是 min
    assert sorted(u.ttft_ms_samples) == [100, 200, 9000]
    assert metrics.ttft_primary_sec(u) == 0.2       # 显示中位数


def test_ttft_primary_falls_back_to_min_without_samples():
    u = TokenUsage(time_to_first_token_ms=1500)
    assert metrics.ttft_primary_sec(u) == 1.5
    assert metrics.ttft_primary_sec(TokenUsage()) is None


def test_compute_report_exposes_est_and_median_ttft():
    u = TokenUsage(input_tokens=10, output_tokens=1000, est_gen_output_tokens=1000,
                   est_gen_duration_ms=20_000, time_to_first_token_ms=100,
                   ttft_ms_samples=[100, 300, 500])
    r = metrics.compute(SessionMeta(session_id="s", cwd=None, title=None,
                                    path=Path("x"), is_subagent=False, source="codex"), u, net_loc=None)
    assert r.est_output_tps == 50.0
    assert r.time_to_first_token_sec == 0.3
    assert r.output_tps is None  # Codex 无逐补全耗时：output_tps 口径不变


# ---------------------------------------------------------------- OpenCode

def test_opencode_est_speed_streamed_then_completed(tmp_path, monkeypatch):
    from test_opencode_reader import _make_db
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    db = _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))
    con = sqlite3.connect(db)

    def add(mid, t, out, reasoning=0):
        con.execute("insert into message values (?, ?, ?, ?)", (mid, "ses-1", t, json.dumps({
            "role": "assistant", "providerID": "openai", "modelID": "gpt-5.2",
            "tokens": {"input": 10, "output": out, "reasoning": reasoning,
                       "cache": {"read": 0, "write": 0}},
            "time": {"created": t, **_times[mid]}})))
    _times = {
        "a1": {"completed": 1782720012000, "streamed": 1782720006000},  # streamed 优先 → 4s
        "a2": {"completed": 1782720030000},                              # 无 streamed → completed，5s
        "a3": {"streamed": 1782720040000},                                # 无 completed（未完成）→ 不收
    }
    add("a1", 1782720002000, 300)          # 300 / 4s
    add("a2", 1782720025000, 100, 100)     # (100 + 推理 100) / 5s
    add("a3", 1782720035000, 900)
    con.commit()
    con.close()
    u = opencode_reader.aggregate_usage(db, "ses-1")
    assert u.est_gen_output_tokens == 500
    assert u.est_gen_duration_ms == 9_000


# ---------------------------------------------------------------- omp / Pi

def test_omp_est_speed_uses_per_response_duration(tmp_path):
    def asst(ts, out, dur):
        return {"type": "message", "id": f"m{ts}", "timestamp": ts, "message": {
            "role": "assistant", "model": "claude-opus-4-8", "content": [],
            "usage": {"input": 5, "output": out, "cacheRead": 0, "cacheWrite": 0},
            "duration": dur, "stopReason": "stop"}}
    rows = [
        {"type": "session", "id": "s1", "cwd": "C:/x", "timestamp": "2026-10-01T10:00:00Z"},
        asst("2026-10-01T10:00:10Z", 600, 10_000),
        asst("2026-10-01T10:00:20Z", 100, 2_000),     # 输出 < 200 → 不收
        asst("2026-10-01T10:00:30Z", 300, 500),       # 耗时 < 1s → 不收
    ]
    u = omp_reader.aggregate_usage(_w(tmp_path / "s.jsonl", rows))
    assert (u.est_gen_output_tokens, u.est_gen_duration_ms) == (600, 10_000)
