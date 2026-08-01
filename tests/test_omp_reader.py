from __future__ import annotations

import json
from pathlib import Path

from tcer.core import analyze, omp_reader
from tcer.core.models import ProjectRef

# omp writes ISO-8601 timestamps (not epoch ms); the reader parses them via
# parse_timestamp_ms -> datetime.fromisoformat.
_ISO = "2026-07-29T06:18:21.927Z"


def _write_omp(path: Path, entries: list[dict]) -> Path:
    """Write an omp session JSONL: fixed-width ``title`` slot + header + entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "title", "v": 1, "title": "",
                             "updatedAt": _ISO, "pad": ""}, ensure_ascii=False) + "\n")
        for obj in entries:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def _session(cwd=r"C:\repo\app", sid="019fa-1", title="t") -> dict:
    return {"type": "session", "version": 3, "id": sid, "timestamp": _ISO,
            "cwd": cwd, "title": title, "titleSource": "auto"}


def _model_change(model="claude-opus-4-8") -> dict:
    return {"type": "model_change", "id": "m1", "parentId": None,
            "timestamp": _ISO, "model": model}


def _thinking_level(level="high") -> dict:
    return {"type": "thinking_level_change", "id": "tl1", "parentId": None,
            "timestamp": _ISO, "thinkingLevel": level, "configured": None}


def _user(text="hi", ts=_ISO, imgs=0) -> dict:
    content = [{"type": "text", "text": text}]
    for _ in range(imgs):
        content.append({"type": "image", "mimeType": "image/png", "data": "iVBORw0="})
    return {"type": "message", "id": "u1", "parentId": None, "timestamp": ts,
            "message": {"role": "user", "content": content,
                        "attribution": "user", "timestamp": ts}}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _thinking() -> dict:
    return {"type": "thinking", "thinking": "...", "thinkingSignature": "sig"}


def _tool_call(name: str, cid: str, args: dict) -> dict:
    return {"type": "toolCall", "id": cid, "name": name, "arguments": args}


def _assistant(content, usage, *, model="claude-opus-4-8", provider="granola",
               stop="toolUse", ts=_ISO, snap=None, duration=None, ttft=None,
               resp_id="r1", err=None) -> dict:
    msg = {"role": "assistant", "content": content, "api": "anthropic-messages",
           "provider": provider, "model": model, "usage": usage,
           "stopReason": stop, "timestamp": ts, "responseId": resp_id}
    if err is not None:
        msg["errorMessage"] = err
    if duration is not None:
        msg["duration"] = duration
    if ttft is not None:
        msg["ttft"] = ttft
    if snap is not None:
        msg["contextSnapshot"] = snap
    return {"type": "message", "id": "a1", "parentId": "u1", "timestamp": ts,
            "message": msg}


def _tool_result(cid, tool_name, *, details=None, is_error=False,
                 content=None, ts=_ISO) -> dict:
    return {"type": "message", "id": "tr1", "parentId": "a1", "timestamp": ts,
            "message": {"role": "toolResult", "toolCallId": cid, "toolName": tool_name,
                        "content": content if content is not None else [{"type": "text", "text": "ok"}],
                        "details": details or {}, "isError": is_error, "timestamp": ts}}


def _usage(inp, out, cr=0, cw=0, cost=None) -> dict:
    u = {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw,
         "totalTokens": inp + cr + cw + out}
    if cost is not None:
        u["cost"] = {"total": cost}
    return u


def test_aggregate_usage_basic(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _model_change("claude-opus-4-8"),
        _user("实现 omp 支持"),
        _assistant([_thinking(), _text("好的"), _tool_call("read", "toolu_1", {"path": "a.py"})],
                   _usage(100, 20, cr=40, cw=10, cost=0.01),
                   snap={"promptTokens": 1500}, duration=900, ttft=200),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.input_tokens == 100
    assert u.output_tokens == 20
    assert u.cache_read_input_tokens == 40
    assert u.cache_creation_input_tokens == 10
    assert u.peak_input_tokens == 1500          # from contextSnapshot.promptTokens
    assert u.user_msgs == 1
    assert u.assistant_msgs == 1
    assert u.thinking_count == 1
    assert u.tool_calls == {"Read": 1}
    assert u.web_search_count == 0
    assert u.time_to_first_token_ms == 200
    assert u.session_duration_ms == 900
    assert abs(u.reported_cost_usd - 0.01) < 1e-9
    assert "claude-opus-4-8" in u.models
    assert len(u.per_model) == 1
    assert sum(mu.input_tokens for mu in u.per_model.values()) == 100
    # one TurnStat step per assistant response
    assert len(u.turn_stats) == 1
    t = u.turn_stats[0]
    assert (t.input_tokens, t.cache_read, t.output_tokens) == (100, 40, 20)
    assert t.duration_ms == 900
    assert t.model == "claude-opus-4-8"


def test_model_change_strips_provider_prefix(tmp_path):
    """model_change carries 'provider/model'; pricing.normalize drops the prefix."""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _model_change("granola/claude-opus-4-8"),
        _user("x"),
        _assistant([_text("hi")], _usage(10, 5)),
    ])
    u = omp_reader.aggregate_usage(p)
    assert "granola/claude-opus-4-8" not in u.models
    assert len(u.models) == 1
    assert "claude-opus-4-8" in u.models


def test_empty_usage_skipped(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("x"),
        _assistant([_text("hi")], _usage(0, 0)),   # all-zero -> skipped
        _assistant([_text("ok")], _usage(50, 10)),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.empty_usage_skipped == 1
    assert u.assistant_msgs == 1
    assert u.input_tokens == 50 and u.output_tokens == 10


def test_custom_entries_ignored(tmp_path):
    """custom / custom_message entries must not crash or count as messages."""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        {"type": "custom_message", "id": "c1", "parentId": None, "timestamp": _ISO,
         "customType": "x", "content": "n", "display": True},
        {"type": "custom", "id": "c2", "parentId": None, "timestamp": _ISO, "data": {}},
        _user("hi"),
        _assistant([_text("ok")], _usage(5, 2)),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.user_msgs == 1 and u.assistant_msgs == 1


def test_tool_mapping_and_ops(tmp_path):
    calls = [
        ("read", "c1", {"path": "a.py"}),
        ("grep", "c2", {"path": "."}),
        ("bash", "c3", {"workdir": "."}),
        ("web_search", "c4", {"query": "x"}),
        ("todo", "c5", {}),
        ("task", "c6", {}),
    ]
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("do things"),
        _assistant([_tool_call(n, cid, args) for n, cid, args in calls], _usage(10, 5)),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.tool_calls == {"Read": 1, "Grep": 1, "Bash": 1,
                            "WebSearch": 1, "TodoWrite": 1, "Task": 1}
    assert u.web_search_count == 1
    ops = {(op.tool, op.path) for op in u.tool_ops}
    assert ("Read", "a.py") in ops
    assert ("Grep", ".") in ops


def test_tool_errors_by_tool(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("x"),
        _assistant([_tool_call("bash", "c1", {})], _usage(10, 5)),
        _tool_result("c1", "bash", is_error=True),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.tool_errors == 1
    assert u.tool_errors_by_tool == {"Bash": 1}


def test_image_inputs(tmp_path):
    """Inline base64 image blocks on user messages count as image inputs."""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("look at these", imgs=2),
        _assistant([_text("ok")], _usage(10, 5)),
        _user("and this one", imgs=1),
        _assistant([_text("done")], _usage(8, 3), stop="stop"),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.image_count == 3
    assert u.user_msgs == 2


def test_prompt_behavior_signals(tmp_path):
    """slash 命令 / 纠正措辞 / 首条消息长度——与 Claude 同构，只计数不存正文。"""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("帮我实现一个解析器,要求如下:支持 JSONL"),   # 首条真实消息
        _user("/compact"),                                  # slash
        _user("<command-name>/model</command-name>"),        # 命令面板
        _user("不对,重来,应该用差分"),                       # 纠正
        _user("好的继续"),                                   # 普通
        _assistant([_text("done")], _usage(10, 5)),
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.slash_command_count == 2
    assert u.correction_msg_count == 1
    assert u.first_prompt_chars == len("帮我实现一个解析器,要求如下:支持 JSONL")
    assert u.user_msgs == 5


def test_prompt_signals_skip_subagent(tmp_path):
    """子代理的 user 消息是 Task 派发 prompt，非真人输入 → prompt 信号不计。"""
    p = _write_omp(tmp_path / "sub.jsonl", [
        _session(),
        _user("/deploy"),
        _user("不对重来"),
        _assistant([_text("x")], _usage(1, 1)),
    ])
    u = omp_reader._aggregate_single(p, is_subagent=True)
    assert u.slash_command_count == 0
    assert u.correction_msg_count == 0
    assert u.first_prompt_chars == 0
    assert u.user_msgs == 2  # 成本计数仍保留


def test_aborted_turns(tmp_path):
    """stopReason == 'aborted' turns feed aborted_task_count + abort_reasons
    (analogous to Codex turn_aborted); errorMessage keys the reason."""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("do a thing"),
        _assistant([_text("working")], _usage(10, 5), stop="aborted",
                   err="Interrupted by user"),
        _user("try again"),
        _assistant([_text("done")], _usage(8, 3), stop="stop"),
        _assistant([_text("nope")], _usage(0, 0), stop="aborted"),  # no errorMessage
    ])
    u = omp_reader.aggregate_usage(p)
    assert u.aborted_task_count == 2
    assert u.abort_reasons == {"Interrupted by user": 1, "aborted": 1}


def test_loc_write_unseen(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("create file"),
        _assistant([_tool_call("write", "c1", {"path": "new.py", "content": "a\nb\nc\n"})],
                   _usage(10, 5)),
        _tool_result("c1", "write", details={"resolvedPath": "new.py"}),
    ])
    sloc, has = omp_reader._loc_scan(p)
    assert has is True
    assert sloc.added == 3
    assert sloc.deleted == 0
    assert sloc.unseen_writes == 1


def test_loc_edit_delta(tmp_path):
    # edit grows a.py: 2 old lines -> 4 new lines (net +2)
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("edit"),
        _assistant([_tool_call("edit", "c1", {"path": "a.py"})], _usage(10, 5)),
        _tool_result("c1", "edit",
                     details={"path": "a.py", "oldText": "a\nb\n", "newText": "a\nb\nc\nd\n"}),
    ])
    sloc, has = omp_reader._loc_scan(p)
    assert has is True
    assert sloc.added == 2          # max(0, 4 - 2)
    assert sloc.deleted == 0


def test_loc_edit_self_rework(tmp_path):
    # write 5 lines, then edit-replace the first 3 with 1 -> rework of session-authored lines
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("write then rework"),
        _assistant([_tool_call("write", "c1", {"path": "a.py", "content": "1\n2\n3\n4\n5\n"})],
                   _usage(10, 5)),
        _tool_result("c1", "write", details={"resolvedPath": "a.py"}),
        _assistant([_tool_call("edit", "c2", {"path": "a.py"})], _usage(12, 6)),
        _tool_result("c2", "edit",
                     details={"path": "a.py", "oldText": "1\n2\n3\n", "newText": "one\n"}),
    ])
    sloc, _ = omp_reader._loc_scan(p)
    assert sloc.added == 5          # the write
    assert sloc.deleted == 2        # max(0, 3 - 1)
    assert sloc.rework_deleted == 2  # 2 of the 3 deleted were session-authored
    assert sloc.unseen_writes == 1


def test_loc_skips_pruned_edits(tmp_path):
    """edit details with snapshotsPruned (no oldText/newText) are skipped gracefully."""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("edit"),
        _assistant([_tool_call("edit", "c1", {"path": "a.py"})], _usage(10, 5)),
        _tool_result("c1", "edit", details={"path": "a.py", "snapshotsPruned": True}),
    ])
    sloc, has = omp_reader._loc_scan(p)
    assert has is False
    assert sloc.added == 0 and sloc.deleted == 0


def test_has_loc_signal_read_only(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("read only"),
        _assistant([_tool_call("read", "c1", {"path": "a.py"})], _usage(10, 5)),
        _tool_result("c1", "read", details={"resolvedPath": "a.py"}),
    ])
    assert omp_reader.has_loc_signal(p) is False
    assert omp_reader.session_loc_full(p).added == 0


def test_read_session_meta(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(cwd=r"C:\repo\app", sid="sid-xyz", title="my title"),
        _model_change(),
        _user("first message here"),
        _thinking_level("high"),
        _assistant([_text("ok")], _usage(5, 2), provider="granola"),
    ])
    meta = omp_reader.read_session_meta(p)
    assert meta.session_id == "sid-xyz"
    assert meta.cwd == r"C:\repo\app"
    assert meta.title == "my title"
    assert meta.source == "omp"
    assert meta.entrypoint == "omp"
    assert meta.cli_version == "session v3"
    assert meta.model_provider == "granola"
    assert meta.reasoning_effort == "high"
    # omp writes ISO-8601 timestamps (not epoch ms); they must parse to ~1e12 ms.
    u = omp_reader.aggregate_usage(p)
    assert u.started_at is not None and u.started_at >= 1_000_000_000_000


def test_reasoning_effort_last_wins(tmp_path):
    """Multiple thinking_level_change events: the last non-empty level wins."""
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _thinking_level("low"),
        _user("x"),
        _thinking_level("high"),
        _assistant([_text("ok")], _usage(5, 2)),
    ])
    assert omp_reader.read_session_meta(p).reasoning_effort == "high"


def test_read_conversation(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("hi"),
        _assistant([_thinking(), _text("sure"), _tool_call("read", "c1", {"path": "a.py"})],
                   _usage(5, 2)),
        _tool_result("c1", "read", is_error=False,
                     content=[{"type": "text", "text": "file"}]),
    ])
    convo = omp_reader.read_conversation(p)
    kinds = [(b["role"], b["type"]) for b in convo]
    assert kinds == [("user", "text"), ("assistant", "thinking"),
                     ("assistant", "text"), ("assistant", "tool_use"),
                     ("tool", "tool_result")]
    assert convo[3]["name"] == "Read"          # canonical
    assert convo[4]["is_error"] is False
    assert convo[4]["text"] == "file"


def test_read_user_messages(tmp_path):
    p = _write_omp(tmp_path / "s.jsonl", [
        _session(),
        _user("first"), _user("second"),
        _assistant([_text("ok")], _usage(5, 2)),
    ])
    assert omp_reader.read_user_messages(p) == ["first", "second"]


# --------------------------------------------------------------------------- subagent folding

_STEM = "2026-07-29T06-18-21.927Z_019fa-1"


def _seed_omp_project(root: Path, *, cwd=r"C:\repo\app", with_subagent=False) -> Path:
    """Write a main session (and optionally a folded subagent) under <root>/sessions."""
    proj = root / "sessions" / "C--repo-app"
    main = proj / f"{_STEM}.jsonl"
    _write_omp(main, [_session(cwd=cwd), _user("main user"),
                      _assistant([_text("main reply")], _usage(100, 10, cost=0.5))])
    if with_subagent:
        sub = proj / _STEM / "SubAgent.jsonl"
        _write_omp(sub, [_session(cwd=cwd), _user("sub task"),
                         _assistant([_text("sub reply")], _usage(200, 20, cost=0.2))])
    return main


def test_subagent_detection_and_folding(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    main = _seed_omp_project(tmp_path / "agent", with_subagent=True)
    sub = main.parent / _STEM / "SubAgent.jsonl"

    assert omp_reader._is_subagent_file(sub) is True
    assert omp_reader._is_subagent_file(main) is False
    assert omp_reader._subagent_files(main) == [sub]

    # aggregate folds the subagent into the parent (real cost preserved)
    u = omp_reader.aggregate_usage(main)
    assert u.input_tokens == 300          # 100 main + 200 sub
    assert u.output_tokens == 30
    assert abs(u.reported_cost_usd - 0.7) < 1e-9
    assert u.user_msgs == 2               # main + sub user msgs


def test_list_project_refs_excludes_subagents(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    _seed_omp_project(tmp_path / "agent", with_subagent=True)
    refs = omp_reader.list_project_refs()
    assert len(refs) == 1
    ref = refs[0]
    assert ref.source == "omp"
    # session_paths holds MAIN files only (subagent folded, not a separate session)
    assert len(ref.session_paths) == 1
    assert omp_reader.sessions_for_project(ref) == list(ref.session_paths)


def test_analyze_project_folds_subagents_and_counts(tmp_path, monkeypatch):
    from tcer.core import file_cache
    file_cache.clear()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    _seed_omp_project(tmp_path / "agent", with_subagent=True)
    refs = omp_reader.list_project_refs()
    a = analyze.analyze_project(refs[0].key, source="omp", project_ref=refs[0])
    assert a.n_sessions == 1
    assert a.n_subagents == 1              # folded subagent counted
    assert a.aggregate.usage.input_tokens == 300
    assert len(a.reports) == 1             # subagent is not a separate report
    file_cache.clear()


# --------------------------------------------------------------------------- env resolution

def test_omp_sessions_dir_env(tmp_path, monkeypatch):
    from tcer.core import paths
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    assert paths.omp_sessions_dir() == tmp_path / "agent" / "sessions"
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CONFIG_DIR", "myconf")
    assert paths.omp_sessions_dir() == Path.home() / "myconf" / "agent" / "sessions"
    monkeypatch.delenv("PI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("OMP_HOME", str(tmp_path / "legacy"))
    assert paths.omp_sessions_dir() == tmp_path / "legacy" / "agent" / "sessions"



