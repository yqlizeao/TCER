from __future__ import annotations

import json
from pathlib import Path

from tcer.core import analyze, codex_reader


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return path


def _codex_lines(cwd: str = r"C:\repo\app") -> list[dict]:
    return [
        {
            "timestamp": "2026-06-29T01:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": "sid-1",
                "cwd": cwd,
                "source": "vscode",
                "thread_source": "user",
                "originator": "codex_vscode",
                "cli_version": "0.142.3",
                "model_provider": "openai",
                "git": {
                    "branch": "main",
                    "commit_hash": "abcdef1234567890",
                    "repository_url": "https://example.test/repo",
                },
            },
        },
        {
            "timestamp": "2026-06-29T01:00:01Z",
            "type": "turn_context",
            "payload": {
                "model": "gpt-5.2-codex",
                "cwd": cwd,
                "model_context_window": 1000,
                "approval_policy": "never",
                "sandbox_policy": {"mode": "workspace-write"},
                "permission_profile": {"name": "trusted"},
                "collaboration_mode": "default",
                "effort": "high",
            },
        },
        {
            "timestamp": "2026-06-29T01:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "实现 Codex 支持",
                "images": ["https://example.test/a.png"],
                "local_images": [r"C:\tmp\b.png"],
            },
        },
        {
            "timestamp": "2026-06-29T01:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": json.dumps({"cmd": "rg --files", "workdir": cwd}),
            },
        },
        {
            "timestamp": "2026-06-29T01:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Process exited with code 1",
            },
        },
        {
            "timestamp": "2026-06-29T01:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "model_context_window": 1000,
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    }
                },
                "rate_limits": {"limit_id": "codex", "limit_name": "Codex"},
            },
        },
        {
            "timestamp": "2026-06-29T01:00:06Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 50,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 7,
                        "total_tokens": 230,
                    }
                },
                "rate_limits": {
                    "limit_id": "codex",
                    "rate_limit_reached_type": "rate_limit_reached",
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "context_compacted"}},
        {"type": "response_item", "payload": {"type": "web_search_call", "id": "ws1"}},
        {"type": "event_msg", "payload": {"type": "web_search_end", "call_id": "ws1"}},
    ]


def test_discover_sessions_recurses_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    p = _write_jsonl(tmp_path / "sessions" / "2026" / "06" / "29" / "s.jsonl", [])
    assert codex_reader.discover_sessions() == [p]


def test_aggregate_usage_maps_codex_tokens_and_tools(tmp_path):
    p = _write_jsonl(tmp_path / "s.jsonl", _codex_lines())
    u = codex_reader.aggregate_usage(p)

    assert u.assistant_msgs == 2
    assert u.input_tokens == (100 - 40) + (200 - 50)
    assert u.cache_read_input_tokens == 40 + 50
    assert u.cache_creation_input_tokens == 0
    assert u.output_tokens == 20 + 30
    assert u.reasoning_output_tokens == 5 + 7
    assert u.model_context_window == 1000
    assert u.reasoning_output_tokens == 12
    assert u.rate_limit_snapshots == 2
    assert u.rate_limit_reached_count == 1
    assert u.rate_limit_names == {"Codex", "codex"}
    assert u.compaction_count == 1
    assert u.web_search_count == 1
    assert u.web_search_end_count == 1
    assert u.image_count == 1
    assert u.local_image_count == 1
    assert u.tool_calls["Grep"] == 1
    assert u.tool_errors == 1
    assert u.tool_errors_by_tool["Grep"] == 1
    assert "gpt-5.2-codex" in u.models
    assert u.per_model["gpt-5.2-codex"].output_tokens == 50
    assert u.user_msgs == 1
    assert u.user_message_texts == []
    assert codex_reader.read_user_messages(p) == ["实现 Codex 支持"]


def test_read_conversation_maps_codex_turns(tmp_path):
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"timestamp": "2026-06-29T01:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "系统提示"}]}},
        {"timestamp": "2026-06-29T01:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "实现功能"}]}},
        {"timestamp": "2026-06-29T01:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "好的，我来做"}]}},
        {"timestamp": "2026-06-29T01:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "c1",
                     "arguments": json.dumps({"cmd": "rg foo"})}},
        {"timestamp": "2026-06-29T01:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": "Process exited with code 1"}},
        # agent_message duplicates response_item.message — must NOT be emitted.
        {"timestamp": "2026-06-29T01:00:05Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "好的，我来做"}},
    ])
    convo = codex_reader.read_conversation(p)
    kinds = [(b["role"], b["type"]) for b in convo]
    # developer message skipped; agent_message not double-counted.
    assert kinds == [
        ("user", "text"),
        ("assistant", "text"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
    ]
    assert convo[0]["text"] == "实现功能"
    assert convo[2]["name"] == "Grep"
    assert convo[2]["input"] == {"cmd": "rg foo"}
    assert convo[3]["is_error"] is True


def test_codex_task_complete_duration_is_used(tmp_path):
    lines = _codex_lines() + [
        {
            "timestamp": "2026-06-29T01:00:10Z",
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "started_at": 1782718054,
            },
        },
        {
            "timestamp": "2026-06-29T01:00:20Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "completed_at": 1782718055,
                "duration_ms": 1234,
                "time_to_first_token_ms": 456,
            },
        }
    ]
    p = _write_jsonl(tmp_path / "s.jsonl", lines)
    u = codex_reader.aggregate_usage(p)
    assert u.session_duration_ms == 1234
    assert u.time_to_first_token_ms == 456
    assert u.tool_calls["Task"] == 1
    assert u.task_count == 1
    assert u.completed_task_count == 1


def test_project_refs_group_by_cwd_and_index_title(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_jsonl(tmp_path / "session_index.jsonl", [
        {"id": "sid-1", "thread_name": "线程标题"},
    ])
    p = _write_jsonl(tmp_path / "sessions" / "2026" / "06" / "29" / "s.jsonl", _codex_lines())

    refs = codex_reader.list_project_refs()
    assert len(refs) == 1
    assert refs[0].source == "codex"
    assert refs[0].session_paths == (p,)
    assert refs[0].display_name == "app"
    meta = codex_reader.read_session_meta(p)
    assert meta.title == "线程标题"
    assert meta.cli_version == "0.142.3"
    assert meta.model_provider == "openai"
    assert meta.thread_source == "user"
    assert meta.git_branch == "main"
    assert meta.git_commit == "abcdef1234567890"
    assert meta.git_repository == "https://example.test/repo"
    assert meta.approval_policy == "never"
    assert meta.sandbox_policy == "workspace-write"
    assert meta.permission_profile == "trusted"
    assert meta.collaboration_mode == "default"
    assert meta.reasoning_effort == "high"


def test_apply_patch_loc_counts_only_patch_hunks(tmp_path):
    patch = """*** Begin Patch
*** Update File: app.py
@@
-old
+new
+line
*** Add File: README.md
+hello
*** End Patch
"""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "arguments": json.dumps({"patch": patch}),
            },
        }
    ])
    sloc = codex_reader.session_loc_full(p)
    assert codex_reader.has_loc_signal(p) is True
    assert sloc.added == 3
    assert sloc.deleted == 1
    assert sloc.file_edit_counts == {"app.py": 1, "README.md": 1}


def test_analyze_codex_project_without_loc_keeps_token_metrics(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_jsonl(
        tmp_path / "sessions" / "2026" / "06" / "29" / "s.jsonl",
        _codex_lines(cwd=str(tmp_path / "repo")),
    )
    ref = codex_reader.list_project_refs()[0]

    result = analyze.analyze_project(ref.key, source="codex", project_ref=ref, no_loc=False)

    assert result.source == "codex"
    assert result.n_sessions == 1
    assert result.aggregate.usage.total > 0
    assert result.aggregate.net_loc is None
    assert result.reports[0].tcer is None
