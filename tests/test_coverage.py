"""覆盖与守恒审计（core/coverage.py）的反向测试：每类问题都必须被点名。

审计只在真实数据上跑出 PASS 不足以说明它有用——这里逐类构造漂移/破损数据，
断言它们落进「未知 / 取值异常 / 失败检查」，并断言干净数据确实 PASS。
"""
from __future__ import annotations

import json
from pathlib import Path

from tcer.core import coverage, reader


def _asst(mid: str, *, usage: dict | None = None, content: list | None = None, **extra) -> dict:
    u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0, "service_tier": "standard"}
    if usage:
        u.update(usage)
    return {"type": "assistant", "version": "2.1.300",
            "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                        "content": content or [{"type": "text", "text": "ok"}], "usage": u},
            **extra}


def _tool_use(tid: str, name: str = "Bash") -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": {"command": "ls"}}


def _result(tid: str) -> dict:
    return {"type": "user", "version": "2.1.300",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def _clean_rows() -> list[dict]:
    return [
        {"type": "user", "version": "2.1.300",
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        _asst("m1", content=[_tool_use("t1")]),
        _result("t1"),
        _asst("m2"),
    ]


def _run(files: list[Path]) -> coverage.CoverageReport:
    return coverage.claude_coverage(files)


def _issue_keys(rep) -> set[tuple[str, str, str]]:
    return {(e.dim, e.value, e.status) for e in rep.issues}


def _check(rep, name: str) -> coverage.CoverageCheck:
    return next(c for c in rep.checks if c.name == name)


def test_clean_data_passes(tmp_path: Path):
    rep = _run([_write(tmp_path / "p" / "s.jsonl", _clean_rows())])
    assert rep.ok, coverage.format_report(rep)
    assert rep.issues == []
    assert all(c.ok for c in rep.checks if c.level == "warn")


def test_unknown_record_type_block_and_usage_key_are_flagged(tmp_path: Path):
    rows = _clean_rows() + [
        {"type": "brand-new-event", "version": "2.1.400"},
        {"type": "system", "subtype": "new_subtype", "version": "2.1.400"},
        _asst("m3", usage={"novel_tokens": 7}, content=[{"type": "hologram"}]),
    ]
    rep = _run([_write(tmp_path / "p" / "s.jsonl", rows)])
    keys = _issue_keys(rep)
    assert ("type", "brand-new-event", coverage.UNKNOWN) in keys
    assert ("system.subtype", "new_subtype", coverage.UNKNOWN) in keys
    assert ("usage", "novel_tokens", coverage.UNKNOWN) in keys
    assert ("block.assistant", "hologram", coverage.UNKNOWN) in keys
    assert not rep.ok
    # 版本超出普查范围也要点名（info，不单独判失败）
    assert rep.newer_than_census
    assert not _check(rep, "version_within_census").ok
    # 未知项带首见位置，便于人工定位
    e = next(e for e in rep.issues if e.value == "brand-new-event")
    assert e.example.endswith(":5") and e.min_version == "2.1.400"


def test_pricing_relevant_values_outside_allowlist_are_flagged(tmp_path: Path):
    rows = _clean_rows() + [
        _asst("m3", usage={"speed": "fast", "service_tier": "priority",
                           "inference_geo": "us"}),
    ]
    rep = _run([_write(tmp_path / "p" / "s.jsonl", rows)])
    keys = _issue_keys(rep)
    assert ("usage.speed=", '"fast"', coverage.UNEXPECTED) in keys
    assert ("usage.service_tier=", '"priority"', coverage.UNEXPECTED) in keys
    assert ("usage.inference_geo=", '"us"', coverage.UNEXPECTED) in keys
    # 白名单内的值不报
    assert not any(v == '"standard"' for _, v, _ in keys)


def test_conservation_failures_are_reported(tmp_path: Path):
    rows = [
        _result("ghost"),                                           # 无调用的结果
        _asst("", usage={"input_tokens": 50}),                      # 有用量无 id
        _asst("m1", usage={"input_tokens": 100}),
        _asst("m1", usage={"input_tokens": 40, "output_tokens": 9}),  # 快照输入变小
        _asst("m2", usage={"iterations": [                          # 多轮 iterations
            {"input_tokens": 5, "output_tokens": 2}, {"input_tokens": 5, "output_tokens": 3}]}),
        _asst("m4", content=[_tool_use("t9")]),                     # 无结果的调用
    ]
    main = _write(tmp_path / "p" / "s.jsonl", rows)
    orphan_sub = _write(tmp_path / "p" / "gone" / "subagents" / "agent-x.jsonl", _clean_rows())
    rep = _run([main, orphan_sub])
    assert not _check(rep, "tool_results_have_calls").ok
    assert not _check(rep, "usage_lines_have_id").ok
    assert not _check(rep, "iterations_consistent").ok
    assert not _check(rep, "subagent_parent_exists").ok
    assert {c.name for c in rep.failed_checks} >= {
        "tool_results_have_calls", "usage_lines_have_id", "iterations_consistent",
        "subagent_parent_exists"}
    # info 级：如实列出但不判失败
    shrink = _check(rep, "snapshot_input_not_shrinking")
    assert not shrink.ok and shrink.level == "info"
    pend = _check(rep, "calls_have_results")
    assert not pend.ok and pend.level == "info"
    assert not rep.ok


def test_requests_without_responses_are_counted(tmp_path: Path):
    rows = _clean_rows() + [
        {"type": "api-request", "querySource": "repl_main_thread", "version": "2.1.300"},
        {"type": "api-request", "querySource": "repl_main_thread", "version": "2.1.300"},
        {"type": "api-request", "querySource": "repl_main_thread", "version": "2.1.300"},
    ]
    rep = _run([_write(tmp_path / "p" / "s.jsonl", rows)])
    c = _check(rep, "requests_vs_responses")
    assert not c.ok and c.level == "info"
    assert "请求 3 / 有用量的回复 2" in c.detail


def test_reader_cross_check_catches_duplicate_server_tool_use(tmp_path: Path):
    """rewind 重写历史会重复同一 server_tool_use id：reader 必须只计一次，交叉核对必须一致。"""
    stu = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
    rows = _clean_rows() + [
        _asst("m3", content=[stu]),
        _asst("m3", content=[stu]),  # 重发
    ]
    path = _write(tmp_path / "p" / "s.jsonl", rows)
    u = reader.aggregate_usage(path)
    assert u.tool_calls.get("web_search") == 1
    rep = _run([path])
    assert _check(rep, "reader_tool_calls_match").ok


def test_registry_has_no_empty_notes():
    """登记表每一项都必须写明处理方式或忽略理由。"""
    for dim, reg in coverage.CLAUDE_REGISTRY.items():
        for value, (status, note) in reg.items():
            assert status in (coverage.HANDLED, coverage.IGNORED), (dim, value)
            assert note.strip(), (dim, value)


# --------------------------------------------------------------------------- #
# Antigravity
# --------------------------------------------------------------------------- #
def _agy_root(tmp_path: Path, steps, gens=None, *, version: str = "2.32.0",
              name: str = "antigravity") -> Path:
    from test_antigravity_reader import _build_db, _gen_row
    root = tmp_path / name
    _build_db(root / "conversations" / "c1.db",
              gens if gens is not None else [_gen_row("gemini-4-argon", 10, 5, 0, "r1")], steps)
    (root / "log").mkdir()
    (root / "log" / "cli-1.log").write_text(
        f"I1115 server.go:1] Language server version: {version}\n", encoding="utf-8")
    return root


def _agy_clean_steps():
    from test_antigravity_reader import _make_user_step_bytes, _multi_call_step, _result_step
    return [
        (14, 3, _make_user_step_bytes("帮我改一下")),
        (15, 3, _multi_call_step([("c1", "edit_file", {"Path": "a.py"}),
                                  ("c2", "mcp_chrome_devtools_click", {})])),
        (132, 3, _result_step("c1", "edit_file", {"Path": "a.py"})),
        (132, 3, _result_step("c2", "mcp_chrome_devtools_click", {})),
    ]


def test_antigravity_clean_data_passes(tmp_path):
    rep = coverage.antigravity_coverage([_agy_root(tmp_path, _agy_clean_steps())])
    assert rep.ok, coverage.format_report(rep)
    assert rep.files == 1 and rep.max_version == "antigravity 2.32.0"
    assert _check(rep, "reader_tool_calls_match").ok


def test_antigravity_drift_is_named(tmp_path):
    from test_antigravity_reader import (_make_proto_field, _multi_call_step, _result_step,
                                         _gen_row)
    uuid = "a8e7d3d1-86da-416e-bb71-82e9c7a753a8"
    steps = _agy_clean_steps() + [
        (999, 3, b""),                                                    # 新 step 类型
        (15, 3, _multi_call_step([("c3", "teleport_file", {}),            # 新工具
                                  ("c4", "mcp_mystery_srv_go", {})])),    # MCP 边界未知
        (132, 9, _result_step("c3", "teleport_file", {})),               # 新状态值
        (14, 3, _make_proto_field(7, 2, _make_proto_field(1, 2, uuid))),  # 兜底不应出 UUID
    ]
    gen = _gen_row("gemini-4-argon", 10, 5, 0, "r1")
    # usage 新字段 42 + 输出 ≠ 思考 + 回复
    bad = _make_proto_field(1, 2, _make_proto_field(4, 2, (
        _make_proto_field(2, 0, 1) + _make_proto_field(3, 0, 9) + _make_proto_field(9, 0, 2)
        + _make_proto_field(10, 0, 3) + _make_proto_field(7, 2, "r2")
        + _make_proto_field(42, 0, 1))))
    rep = coverage.antigravity_coverage(
        [_agy_root(tmp_path, steps, [gen, bad], version="2.40.0")])
    keys = {(e.dim, e.value, e.status) for e in rep.entries}
    assert ("step_type", "999", coverage.UNKNOWN) in keys
    assert ("tool", "teleport_file", coverage.UNKNOWN) in keys
    assert ("tool", "mcp_mystery_srv_go", coverage.UNKNOWN) in keys
    assert ("tool", "mcp_chrome_devtools_click", coverage.HANDLED) in keys
    assert ("status.132", "9", coverage.UNKNOWN) in keys
    assert ("gen.usage", "42", coverage.UNKNOWN) in keys
    assert not _check(rep, "output_equals_thinking_plus_reply").ok
    assert _check(rep, "prompt_text_not_metadata").ok        # 第 1 项修复后不再出 UUID
    assert not _check(rep, "version_within_census").ok       # 2.40.0 > 普查 2.32.0
    assert not rep.ok
    text = coverage.format_report(rep)
    assert "说明：未登记" in text


def test_antigravity_background_status_registered(tmp_path):
    from test_antigravity_reader import _multi_call_step, _result_step
    steps = _agy_clean_steps() + [
        (15, 3, _multi_call_step([("c5", "run_command", {"CommandLine": "npm run dev"})])),
        (132, 6, _result_step("c5", "run_command", {"CommandLine": "npm run dev"})),
    ]
    rep = coverage.antigravity_coverage([_agy_root(tmp_path, steps)])
    keys = {(e.dim, e.value, e.status) for e in rep.entries}
    assert ("status.132", "6", coverage.HANDLED) in keys
    assert rep.ok, coverage.format_report(rep)


def test_antigravity_reader_mismatch_detected(tmp_path, monkeypatch):
    from tcer.core import antigravity_reader as ar
    root = _agy_root(tmp_path, _agy_clean_steps())
    real = ar._aggregate_single

    def fake(path, *a, **k):
        u = real(path, *a, **k)
        u.tool_calls["Phantom"] = 1
        return u
    monkeypatch.setattr(ar, "_aggregate_single", fake)
    rep = coverage.antigravity_coverage([root])
    assert not _check(rep, "reader_tool_calls_match").ok


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def _cx_rows() -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": "thr-1", "cwd": "/repo/app",
                                             "cli_version": "0.163.0"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.3"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "shell_command",
            "call_id": "c1", "arguments": "{}"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1",
            "output": "exit 0\nok"}},
        {"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 100,
                                     "cached_input_tokens": 40,
                                     "output_tokens": 20},
                "total_token_usage": {"input_tokens": 100,
                                      "cached_input_tokens": 40,
                                      "output_tokens": 20}}}},
    ]


def test_codex_clean_data_passes(tmp_path: Path):
    rep = coverage.codex_coverage([_write(tmp_path / "r.jsonl", _cx_rows())])
    assert rep.ok, coverage.format_report(rep)


def test_codex_drift_is_named(tmp_path: Path):
    rows = _cx_rows() + [
        {"type": "futuristic_line", "payload": {"type": "x"}},
        {"type": "event_msg", "payload": {"type": "quantum_tick"}},
        {"type": "response_item", "payload": {"type": "holo_item"}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "write_file", "call_id": "c9",
            "arguments": "{}"}},
        {"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 200, "output_tokens": 30,
                                      "brain_tokens": 5}}}},
    ]
    rep = coverage.codex_coverage([_write(tmp_path / "r.jsonl", rows)])
    keys = _issue_keys(rep)
    assert ("type", "futuristic_line", coverage.UNKNOWN) in keys
    assert ("event", "quantum_tick", coverage.UNKNOWN) in keys
    assert ("item", "holo_item", coverage.UNKNOWN) in keys
    assert ("usage", "brain_tokens", coverage.UNKNOWN) in keys
    assert ("tool", "write_file", coverage.UNKNOWN) in keys
    assert not rep.ok


def test_codex_orphan_result_and_reader_mismatch(tmp_path: Path):
    rows = _cx_rows() + [
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "ghost", "output": "x"}},
    ]
    rep = coverage.codex_coverage([_write(tmp_path / "r.jsonl", rows)])
    assert not _check(rep, "tool_results_have_calls").ok


def test_codex_total_reset_is_info(tmp_path: Path):
    rows = _cx_rows() + [
        {"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 10, "output_tokens": 3},
                "last_token_usage": {"input_tokens": 10, "output_tokens": 3}}}},
    ]
    rep = coverage.codex_coverage([_write(tmp_path / "r.jsonl", rows)])
    assert not _check(rep, "total_token_usage_monotonic").ok
    # total 回退时独立重放走 last 兜底，与 reader 口径一致 → 不报 token 缺口
    assert _check(rep, "reader_tokens_match").ok


# --------------------------------------------------------------------------- #
# omp / Pi（同族 JSONL，共用登记表）
# --------------------------------------------------------------------------- #
def _omp_rows() -> list[dict]:
    return [
        {"type": "session", "id": "s1", "cwd": "/repo/app",
         "timestamp": "2026-07-29T06:18:21.927Z"},
        {"type": "model_change", "model": "anthropic/claude-opus-4-6"},
        {"type": "message", "timestamp": "2026-07-29T06:18:30.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message", "timestamp": "2026-07-29T06:18:35.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-6",
                     "content": [{"type": "text", "text": "ok"},
                                 {"type": "toolCall", "id": "t1",
                                  "name": "read", "arguments": {}}],
                     "usage": {"input": 100, "output": 20, "cacheRead": 30,
                               "cacheWrite": 10, "totalTokens": 160,
                               "cost": {"total": 0.01}},
                     "stopReason": "toolUse"}},
        {"type": "message", "timestamp": "2026-07-29T06:18:40.000Z",
         "message": {"role": "toolResult", "toolCallId": "t1",
                     "toolName": "read", "content": "ok", "isError": False}},
    ]


def test_omp_clean_data_passes(tmp_path: Path):
    f = _write(tmp_path / "c--repo-app" / "s1.jsonl", _omp_rows())
    rep = coverage.omp_coverage([f])
    assert rep.ok, coverage.format_report(rep)


def test_pi_clean_data_passes(tmp_path: Path):
    rows = _omp_rows()  # Pi 多 reasoning 字段
    rows[3]["message"]["usage"]["reasoning"] = 5
    f = _write(tmp_path / "c--repo-app" / "s1.jsonl", rows)
    rep = coverage.pi_coverage([f])
    assert rep.ok, coverage.format_report(rep)


def test_omp_drift_is_named(tmp_path: Path):
    rows = _omp_rows() + [
        {"type": "warp_event", "data": {}},
        {"type": "message", "message": {"role": "auditor"}},
        {"type": "message", "message": {"role": "assistant",
                                        "content": [{"type": "holo"}],
                                        "stopReason": "vaporized",
                                        "usage": {"flux": 9}}},
        {"type": "message", "message": {"role": "assistant",
                                        "content": [{"type": "toolCall",
                                                     "id": "t9", "name": "rm_rf",
                                                     "arguments": {}}]}},
    ]
    rep = coverage.omp_coverage([_write(tmp_path / "d" / "s.jsonl", rows)])
    keys = _issue_keys(rep)
    assert ("type", "warp_event", coverage.UNKNOWN) in keys
    assert ("role", "auditor", coverage.UNKNOWN) in keys
    assert ("block.assistant", "holo", coverage.UNKNOWN) in keys
    assert ("stop_reason", "vaporized", coverage.UNKNOWN) in keys
    assert ("usage", "flux", coverage.UNKNOWN) in keys
    assert ("tool", "rm_rf", coverage.UNKNOWN) in keys
    assert not rep.ok


def test_omp_total_consistency_violation_flagged(tmp_path: Path):
    rows = _omp_rows()
    rows[3]["message"]["usage"]["totalTokens"] = 999  # 分量和对不上
    rep = coverage.omp_coverage([_write(tmp_path / "d" / "s.jsonl", rows)])
    assert not _check(rep, "usage_total_consistency").ok
    assert not rep.ok


def test_omp_orphan_result_and_missing_parent(tmp_path: Path):
    rows = _omp_rows() + [
        {"type": "message", "message": {"role": "toolResult",
                                        "toolCallId": "ghost", "content": "x"}},
    ]
    rep = coverage.omp_coverage([_write(tmp_path / "d" / "s.jsonl", rows)])
    assert not _check(rep, "tool_results_have_calls").ok
    # <stem>/ 里的子代理：主文件在盘上但普查漏收 → 点名
    _write(tmp_path / "d2" / "2026_x_s9.jsonl", _omp_rows())
    sub = _write(tmp_path / "d2" / "2026_x_s9" / "sub.jsonl", _omp_rows())
    rep2 = coverage.omp_coverage([sub])
    assert not _check(rep2, "subagent_parent_exists").ok


def test_omp_reasoning_over_output_is_info(tmp_path: Path):
    rows = _omp_rows()
    rows[3]["message"]["usage"]["reasoning"] = 999  # 已知代理路由口径异常
    rows[3]["message"]["usage"]["totalTokens"] = 160  # 分量守恒不变 → 只触发 info
    rep = coverage.omp_coverage([_write(tmp_path / "d" / "s.jsonl", rows)])
    assert not _check(rep, "reasoning_within_output").ok
    assert rep.ok  # info 级不判 FAIL


# --------------------------------------------------------------------------- #
# OpenCode（SQLite）
# --------------------------------------------------------------------------- #
def _oc_db(path: Path, *, with_msg_tokens: bool = True) -> Path:
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        create table project (id text primary key, name text, worktree text);
        create table session (id text primary key, parent_id text, project_id text,
          directory text, title text, time_created integer, time_updated integer,
          model text, permission text, tokens_input integer, tokens_output integer,
          tokens_reasoning integer, tokens_cache_read integer,
          tokens_cache_write integer, cost integer);
        create table message (id text primary key, session_id text,
          time_created integer, data text);
        create table part (id text primary key, session_id text, message_id text,
          time_created integer, data text);
    """)
    jd = lambda o: json.dumps(o, ensure_ascii=False)
    con.execute("insert into project values (?,?,?)", ("prj", "app", "/repo/app"))
    con.execute("insert into session values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ses", None, "prj", "/repo/app", "t", 1, 2, jd({"modelID": "m"}),
                 "{}", 100, 20, 5, 30, 10, 0))
    msg_tokens = ({"tokens": {"input": 100, "output": 20, "reasoning": 5,
                              "cache": {"read": 30, "write": 10}}}
                  if with_msg_tokens else {})
    con.execute("insert into message values (?,?,?,?)",
                ("mu", "ses", 1, jd({"role": "user"})))
    con.execute("insert into message values (?,?,?,?)",
                ("ma", "ses", 2, jd({"role": "assistant", "modelID": "m",
                                     **msg_tokens})))
    con.executemany("insert into part values (?,?,?,?,?)", [
        ("pu", "ses", "mu", 1, jd({"type": "text", "text": "hi"})),
        ("pt", "ses", "ma", 2, jd({"type": "tool", "tool": "read", "callID": "c1",
                                   "state": {"status": "completed"}})),
        ("pr", "ses", "ma", 3, jd({"type": "tool-result", "callID": "c1"})),
    ])
    con.commit()
    con.close()
    return path


def test_opencode_clean_data_passes(tmp_path: Path):
    rep = coverage.opencode_coverage([_oc_db(tmp_path / "opencode.db")])
    assert rep.ok, coverage.format_report(rep)


def test_opencode_drift_is_named(tmp_path: Path):
    import sqlite3
    db = _oc_db(tmp_path / "opencode.db")
    con = sqlite3.connect(db)
    jd = lambda o: json.dumps(o, ensure_ascii=False)
    con.execute("create table quantum (id text)")
    con.execute("insert into message values (?,?,?,?)",
                ("mx", "ses", 3, jd({"role": "oracle"})))
    con.execute("insert into part values (?,?,?,?,?)",
                ("px", "ses", "ma", 4, jd({"type": "vortex"})))
    con.execute("insert into part values (?,?,?,?,?)",
                ("px2", "ses", "ma", 5, jd({"type": "tool", "tool": "rm_rf",
                                            "callID": "c9"})))
    con.commit()
    con.close()
    rep = coverage.opencode_coverage([db])
    keys = _issue_keys(rep)
    assert ("db.table", "quantum", coverage.UNKNOWN) in keys
    assert ("role", "oracle", coverage.UNKNOWN) in keys
    assert ("part", "vortex", coverage.UNKNOWN) in keys
    assert ("tool", "rm_rf", coverage.UNKNOWN) in keys
    assert not rep.ok


def test_opencode_token_gap_flagged_only_when_messages_carry_tokens(tmp_path: Path):
    import sqlite3
    # 消息带 tokens 但和会话列对不上 → warn
    db = _oc_db(tmp_path / "a.db")
    con = sqlite3.connect(db)
    con.execute("update session set tokens_input=9999 where id='ses'")
    con.commit()
    con.close()
    rep = coverage.opencode_coverage([db])
    assert not _check(rep, "session_tokens_equal_message_sum").ok
    # 消息无 tokens（旧格式）→ 不算缺口
    db2 = _oc_db(tmp_path / "b.db", with_msg_tokens=False)
    rep2 = coverage.opencode_coverage([db2])
    assert _check(rep2, "session_tokens_equal_message_sum").ok
