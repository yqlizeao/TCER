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
