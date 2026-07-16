from __future__ import annotations

import json
from pathlib import Path

from tcer.core import analyze, grok_reader
from tcer.core.models import ToolOp

# Epoch seconds (Grok writes ``timestamp`` as seconds, not ms).
_T0 = 1783998890


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def _write_summary(session_dir: Path, data: dict) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    p = session_dir / "summary.json"
    with p.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return p


def _notif(ts: int, update: dict, params_meta: dict | None = None) -> dict:
    """Build one Grok ACP JSON-RPC notification (one updates.jsonl line)."""
    params: dict = {"sessionId": "sid-1", "update": update}
    if params_meta is not None:
        params["_meta"] = params_meta
    return {"timestamp": ts, "method": "session/update", "params": params}


def test_read_conversation_coalesces_grok_chunks(tmp_path):
    p = _write_jsonl(tmp_path / "updates.jsonl", [
        # Two user chunks coalesce into one block.
        _notif(_T0, {"sessionUpdate": "user_message_chunk",
                     "content": {"type": "text", "text": "实现 "}}),
        _notif(_T0, {"sessionUpdate": "user_message_chunk",
                     "content": {"type": "text", "text": "Grok"}}),
        _notif(_T0 + 1, {"sessionUpdate": "agent_thought_chunk",
                         "content": {"type": "text", "text": "思考"}}),
        _notif(_T0 + 2, {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": "好的"}}),
        _notif(_T0 + 3, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                         "rawInput": {"file_path": "a.py"},
                         "_meta": {"x.ai/tool": {"name": "read_file"}}}),
        _notif(_T0 + 4, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                         "status": "completed",
                         "rawOutput": {"exit_code": 0, "output": "内容"}}),
    ])
    convo = grok_reader.read_conversation(p)
    kinds = [(b["role"], b["type"]) for b in convo]
    assert kinds == [
        ("user", "text"),
        ("assistant", "thinking"),
        ("assistant", "text"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
    ]
    assert convo[0]["text"] == "实现 Grok"  # chunks joined
    assert convo[3]["name"] == "Read"       # canonical tool name
    assert convo[4]["is_error"] is False
    assert convo[4]["text"] == "内容"


def _grok_lines() -> list[dict]:
    """A realistic single-turn Grok session: msg → thought → read → edit → error → turn_completed."""
    return [
        _notif(_T0, {"sessionUpdate": "user_message_chunk",
                     "content": {"type": "text", "text": "实现 Grok 支持"}},
               params_meta={"modelId": "grok-4.5", "promptIndex": 0}),
        _notif(_T0 + 1, {"sessionUpdate": "agent_thought_chunk",
                         "content": {"type": "text", "text": "思考中"}}),
        _notif(_T0 + 2, {"sessionUpdate": "tool_call", "toolCallId": "call-1",
                         "title": "read_file", "rawInput": {"target_file": "app.py"},
                         "_meta": {"x.ai/tool": {"name": "read_file", "kind": "read",
                                                  "namespace": "grok_build", "label": "Read File",
                                                  "read_only": True}}}),
        _notif(_T0 + 3, {"sessionUpdate": "tool_call", "toolCallId": "call-2",
                         "title": "search_replace",
                         "rawInput": {"file_path": "app.py", "old_string": "old\n",
                                      "new_string": "new\nline\n"},
                         "_meta": {"x.ai/tool": {"name": "search_replace", "kind": "edit",
                                                  "namespace": "grok_build", "label": "Edit"}}}),
        _notif(_T0 + 4, {"sessionUpdate": "tool_call_update", "toolCallId": "call-2",
                         "status": "completed",
                         "rawOutput": {"type": "Edit", "exit_code": 1,
                                       "output_for_prompt": "err"}}),
        _notif(_T0 + 5, {"sessionUpdate": "turn_completed", "prompt_id": "p1",
                         "stop_reason": "end_turn",
                         "usage": {
                             "inputTokens": 30305, "outputTokens": 116, "totalTokens": 30421,
                             "cachedReadTokens": 26368, "reasoningTokens": 73,
                             "modelCalls": 1, "apiDurationMs": 3322,
                             "modelUsage": {"grok-4.5": {
                                 "inputTokens": 30305, "outputTokens": 116,
                                 "cachedReadTokens": 26368, "reasoningTokens": 73,
                                 "modelCalls": 1, "apiDurationMs": 3322}},
                             "numTurns": 1}}),
    ]


def test_discover_sessions_recurses_grok_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    p = _write_jsonl(tmp_path / "sessions" / "C%3A%5Crepo%5Capp" / "uuid-1" / "updates.jsonl", [])
    assert grok_reader.discover_sessions() == [p]


def test_aggregate_usage_maps_grok_tokens_and_tools(tmp_path):
    p = _write_jsonl(tmp_path / "updates.jsonl", _grok_lines())
    u = grok_reader.aggregate_usage(p)

    # Token usage: input excludes cached; per-model bucket matches.
    assert u.assistant_msgs == 1
    assert u.input_tokens == 30305 - 26368
    assert u.cache_read_input_tokens == 26368
    assert u.cache_creation_input_tokens == 0
    assert u.output_tokens == 116
    assert u.reasoning_output_tokens == 73
    assert "grok-4.5" in u.models
    assert u.per_model["grok-4.5"].input_tokens == 30305 - 26368
    assert u.per_model["grok-4.5"].cache_read_input_tokens == 26368
    assert u.per_model["grok-4.5"].output_tokens == 116

    # Tools classified to canonical names; error attributed to the failing Edit.
    assert u.tool_calls == {"Read": 1, "Edit": 1}
    assert u.tool_errors == 1
    assert u.tool_errors_by_tool == {"Edit": 1}
    assert u.tool_ops == [ToolOp(1, "Read", "app.py"), ToolOp(1, "Edit", "app.py")]

    # Counts and timing.
    assert u.user_msgs == 1
    assert u.thinking_count == 1
    assert u.session_duration_ms == 3322  # apiDurationMs of the turn
    assert u.started_at is not None and u.ended_at is not None

    assert grok_reader.read_user_messages(p) == ["实现 Grok 支持"]


def test_empty_usage_turn_is_skipped(tmp_path):
    """An errored turn emits turn_completed with null usage — must not count as a turn."""
    lines = [
        _notif(_T0, {"sessionUpdate": "retry_state"}),
        _notif(_T0 + 1, {"sessionUpdate": "user_message_chunk",
                         "content": {"type": "text", "text": "hi"}}),
        _notif(_T0 + 2, {"sessionUpdate": "turn_completed", "prompt_id": "p1",
                         "stop_reason": "error",
                         "usage": {"inputTokens": None, "outputTokens": None,
                                   "cachedReadTokens": None}}),
    ]
    p = _write_jsonl(tmp_path / "updates.jsonl", lines)
    u = grok_reader.aggregate_usage(p)
    assert u.assistant_msgs == 0
    assert u.empty_usage_skipped == 1
    assert u.input_tokens == 0 and u.output_tokens == 0


def test_multi_turn_usage_sums_across_turns(tmp_path):
    lines = [
        _notif(_T0, {"sessionUpdate": "user_message_chunk",
                     "content": {"type": "text", "text": "one"}}),
        _notif(_T0 + 1, {"sessionUpdate": "turn_completed", "usage": {
            "inputTokens": 15113, "outputTokens": 91, "cachedReadTokens": 11264,
            "reasoningTokens": 46, "modelUsage": {"grok-4.5": {
                "inputTokens": 15113, "outputTokens": 91, "cachedReadTokens": 11264,
                "reasoningTokens": 46}}}}),
        _notif(_T0 + 2, {"sessionUpdate": "user_message_chunk",
                         "content": {"type": "text", "text": "two"}}),
        _notif(_T0 + 3, {"sessionUpdate": "turn_completed", "usage": {
            "inputTokens": 51978, "outputTokens": 1276, "cachedReadTokens": 46720,
            "reasoningTokens": 100, "modelUsage": {"grok-4.5": {
                "inputTokens": 51978, "outputTokens": 1276, "cachedReadTokens": 46720,
                "reasoningTokens": 100}}}}),
    ]
    p = _write_jsonl(tmp_path / "updates.jsonl", lines)
    u = grok_reader.aggregate_usage(p)
    assert u.assistant_msgs == 2
    assert u.input_tokens == (15113 - 11264) + (51978 - 46720)
    assert u.cache_read_input_tokens == 11264 + 46720
    assert u.output_tokens == 91 + 1276
    assert u.user_msgs == 2


def test_search_replace_loc_counts_line_deltas(tmp_path):
    p = _write_jsonl(tmp_path / "updates.jsonl", _grok_lines())
    sloc = grok_reader.session_loc_full(p)
    assert grok_reader.has_loc_signal(p) is True
    # old_string "old\n" = 1 line; new_string "new\nline\n" = 2 lines.
    assert sloc.added == 2
    assert sloc.deleted == 1
    assert sloc.file_edit_counts == {"app.py": 1}


def test_write_counts_unseen_writes(tmp_path):
    lines = [
        _notif(_T0, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "write",
                     "rawInput": {"file_path": "new.py", "content": "a\nb\nc\n"},
                     "_meta": {"x.ai/tool": {"name": "write", "kind": "edit"}}}),
    ]
    p = _write_jsonl(tmp_path / "updates.jsonl", lines)
    sloc = grok_reader.session_loc_full(p)
    assert grok_reader.has_loc_signal(p) is True
    assert sloc.added == 3          # content lines counted as added (F1 exposure)
    assert sloc.unseen_writes == 1  # whole-file write to an unseen file
    assert sloc.deleted == 0


def test_decode_cwd_and_classify_tool():
    assert grok_reader._decode_cwd("C%3A%5Cplayground%5Clangfuse") == r"C:\playground\langfuse"
    assert grok_reader._decode_cwd(None) is None
    assert grok_reader._classify_grok_tool("read_file") == "Read"
    assert grok_reader._classify_grok_tool("search_replace") == "Edit"
    assert grok_reader._classify_grok_tool("run_terminal_command") == "Bash"
    assert grok_reader._classify_grok_tool("web_search") == "WebSearch"
    assert grok_reader._classify_grok_tool("custom_mcp_thing") == "custom_mcp_thing"
    assert grok_reader._classify_grok_tool(None) == "Tool"


def test_project_refs_group_by_cwd_and_read_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    cwd = r"C:\repo\app"
    sdir = tmp_path / "sessions" / "C%3A%5Crepo%5Capp" / "uuid-1"
    _write_summary(sdir, {
        "info": {"id": "uuid-1", "cwd": cwd},
        "generated_title": "标题",
        "current_model_id": "grok-4.5",
        "reasoning_effort": "high",
        "sandbox_profile": "off",
        "agent_name": "grok-build-plan",
    })
    p = _write_jsonl(sdir / "updates.jsonl", _grok_lines())

    refs = grok_reader.list_project_refs()
    assert len(refs) == 1
    assert refs[0].source == "grok"
    assert refs[0].session_paths == (p,)
    assert refs[0].display_name == "app"

    meta = grok_reader.read_session_meta(p)
    assert meta.source == "grok"
    assert meta.session_id == "uuid-1"
    assert meta.title == "标题"
    assert meta.entrypoint == "grok-build-plan"
    assert meta.reasoning_effort == "high"
    assert meta.sandbox_policy == "off"


def test_analyze_grok_project_without_loc_keeps_token_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    cwd = str(tmp_path / "repo")
    # Build a session with token usage but no edit tools.
    lines = [
        _notif(_T0, {"sessionUpdate": "user_message_chunk",
                     "content": {"type": "text", "text": "hi"}}, params_meta={"modelId": "grok-4.5"}),
        _notif(_T0 + 1, {"sessionUpdate": "turn_completed", "usage": {
            "inputTokens": 1000, "outputTokens": 50, "cachedReadTokens": 200,
            "modelUsage": {"grok-4.5": {"inputTokens": 1000, "outputTokens": 50,
                                        "cachedReadTokens": 200}}}}),
    ]
    # summary.info.cwd drives grouping (takes precedence over folder decoding).
    sdir = tmp_path / "sessions" / "proj" / "uuid-1"
    _write_summary(sdir, {"info": {"id": "uuid-1", "cwd": cwd}})
    _write_jsonl(sdir / "updates.jsonl", lines)

    ref = grok_reader.list_project_refs()[0]
    result = analyze.analyze_project(ref.key, source="grok", project_ref=ref, no_loc=False)

    assert result.source == "grok"
    assert result.n_sessions == 1
    assert result.aggregate.usage.total > 0
    assert result.aggregate.net_loc is None  # no edit tools → no LOC signal
    assert result.reports[0].tcer is None
