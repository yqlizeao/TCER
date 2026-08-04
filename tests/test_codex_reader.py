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
    # Peak single-turn input: max of (100, 200) raw input_tokens (includes cache)
    assert u.peak_input_tokens == 200
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
    # task_started/complete are turn lifecycle events, not tool calls.
    assert "Task" not in u.tool_calls
    assert u.task_count == 1
    assert u.completed_task_count == 1


def _rollout_with_session_id(dir_: Path, name: str, sid: str, cwd: str, mtime_ns: int) -> Path:
    """Minimal Codex rollout file carrying only a session_meta header."""
    import os
    p = _write_jsonl(dir_ / name, [
        {"timestamp": "2026-07-29T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": sid, "cwd": cwd}},
    ])
    os.utime(p, ns=(mtime_ns, mtime_ns))
    return p


def test_codex_resume_same_session_id_collapses_to_latest(tmp_path, monkeypatch):
    """Codex resume writes a new rollout file per resume reusing the same
    session_id; only the most recent (most-complete) snapshot should be
    listed — never one row per file."""
    import time
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sess_dir = tmp_path / "sessions" / "2026" / "07" / "29"
    sess_dir.mkdir(parents=True)
    cwd = str(tmp_path / "repo")
    base = (int(time.time()) - 10_000) * 1_000_000_000

    # Three rollout files for the SAME session (resume) at increasing mtimes,
    # plus one unrelated session sharing the same cwd.
    p_old = _rollout_with_session_id(sess_dir, "rollout-a-SAME.jsonl", "SAME", cwd, base)
    p_mid = _rollout_with_session_id(sess_dir, "rollout-b-SAME.jsonl", "SAME", cwd, base + 1_000_000_000)
    p_new = _rollout_with_session_id(sess_dir, "rollout-c-SAME.jsonl", "SAME", cwd, base + 2_000_000_000)
    p_other = _rollout_with_session_id(sess_dir, "rollout-d-OTHER.jsonl", "OTHER", cwd, base + 3_000_000_000)

    refs = codex_reader.list_project_refs()
    assert len(refs) == 1                       # one project (shared cwd)
    paths = set(refs[0].session_paths)
    assert paths == {p_new, p_other}            # SAME→latest only; OTHER distinct
    assert p_old not in paths and p_mid not in paths

    result = analyze.analyze_project(refs[0].key, source="codex", project_ref=refs[0])
    assert result.n_sessions == 2               # not 4


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
    # First touch of app.py deletes pre-session "old" — not self-rework.
    assert sloc.rework_deleted == 0


def test_planner_text_files_count_as_output(tmp_path):
    """Planner text files (.txt/.rst/.csv) count toward net output and doc LOC;
    binary Office (.docx/.xlsx) does not — the edit model can't line-count them."""
    from tcer.core.loc import _is_code, _is_doc_file
    # Gate: planner text now counts as productive output (was excluded before).
    for fp in ("spec.txt", "plan.rst", "notes.org", "data.csv", "doc.adoc"):
        assert _is_code(fp), f"{fp} should count as output"
    # Prose text is categorized as 文档; .csv is output but data (not doc).
    assert _is_doc_file("spec.txt") and _is_doc_file("plan.rst")
    assert not _is_doc_file("data.csv")
    # Binary Office never counts (no text lines to count).
    assert not _is_code("report.docx") and not _is_code("sheet.xlsx")

    patch = """*** Begin Patch
*** Add File: 设计说明.txt
+需求一
+需求二
*** End Patch
"""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "apply_patch",
            "arguments": json.dumps({"patch": patch}),
        }}
    ])
    sloc = codex_reader.session_loc_full(p)
    assert sloc.added == 2                 # .txt now counts toward net output
    assert sloc.doc_added == 2             # and is recognized as 文档行


def test_apply_patch_self_rework_across_patches(tmp_path):
    """Lines this session added then removed in a later patch count as rework."""
    add = """*** Begin Patch
*** Add File: a.py
+1
+2
+3
+4
+5
*** End Patch
"""
    trim = """*** Begin Patch
*** Update File: a.py
@@
-1
-2
-3
+one
*** End Patch
"""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": add,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": trim,
            },
        },
    ])
    sloc = codex_reader.session_loc_full(p)
    assert sloc.added == 6   # 5 add + 1 new
    assert sloc.deleted == 3
    assert sloc.rework_deleted == 3  # all deleted lines were session-authored


def test_custom_tool_call_apply_patch_counts_as_edit(tmp_path):
    """Live Codex often records apply_patch as custom_tool_call, not function_call."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": {"patch": "*** Begin Patch\n*** End Patch\n"},
            },
        }
    ])
    u = codex_reader.aggregate_usage(p)
    assert u.tool_calls.get("Edit", 0) == 1
    assert "apply_patch" not in u.tool_calls


def test_custom_tool_call_apply_patch_feeds_loc(tmp_path):
    """LOC must parse custom_tool_call.input raw patch text (live Codex shape)."""
    patch = """*** Begin Patch
*** Update File: app.py
@@
-old
+new
+line
*** End Patch
"""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": patch,
            },
        }
    ])
    assert codex_reader.has_loc_signal(p) is True
    sloc = codex_reader.session_loc_full(p)
    assert sloc.added == 2
    assert sloc.deleted == 1
    assert sloc.file_edit_counts.get("app.py") == 1


def test_apply_patch_tool_op_path_from_patch_body(tmp_path):
    """ToolOp.path must surface the first file in the patch (files_touched / R/W ratio)."""
    patch = """*** Begin Patch
*** Update File: src/main.py
@@
-old
+new
*** Add File: extra.py
+x
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
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": patch,
            },
        },
    ])
    u = codex_reader.aggregate_usage(p)
    assert u.tool_calls.get("Edit") == 2
    edit_ops = [op for op in u.tool_ops if op.tool == "Edit"]
    assert len(edit_ops) == 2
    assert all(op.path == "src/main.py" for op in edit_ops)


def test_shell_workdir_not_recorded_as_file_path(tmp_path):
    """A shell command's ``workdir`` is a directory, not a touched file — it
    must not leak into ``ToolOp.path`` (it polluted 涉及文件 with the project
    root, no extension, once per call)."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "c1",
            "arguments": json.dumps({"cmd": "npm test", "workdir": r"C:\repo\app"}),
        }},
    ])
    u = codex_reader.aggregate_usage(p)
    bash_ops = [op for op in u.tool_ops if op.tool == "Bash"]
    assert len(bash_ops) == 1
    assert bash_ops[0].path == ""               # workdir must NOT surface here
    assert all("repo" not in (op.path or "") for op in u.tool_ops)


def test_shell_mentioning_apply_patch_not_misclassified_as_edit(tmp_path):
    """A shell command that merely *mentions* apply_patch (e.g. Select-String
    grepping logs for the tool name) must NOT classify as Edit — only a real
    apply_patch invocation (starts with it, or carries *** Begin Patch) does."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "c1",
            "arguments": json.dumps({"cmd": "Select-String -Pattern 'apply_patch|exec_command' *.jsonl"}),
        }},
    ])
    u = codex_reader.aggregate_usage(p)
    assert u.tool_calls.get("Edit", 0) == 0       # not an edit
    assert "Grep" in u.tool_calls                 # Select-String → search, not edit


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
    # No apply_patch → known zero LOC (not unknown); aggregate stays summable.
    assert result.aggregate.net_loc == 0
    assert result.reports[0].net_loc == 0
    assert result.reports[0].tcer == 0.0


def test_custom_tool_output_exit_code_and_prefix_errors(tmp_path):
    """custom_tool_call_output 的 "Exit code: N" 与无码 "execution error" 均计错；
    正常输出提到 error 一词不再误报。"""
    events = [
        {"timestamp": "2026-07-01T10:00:00Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 5}}}},
        {"timestamp": "2026-07-01T10:00:01Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "call_id": "c1", "input": "*** Begin Patch\n*** End Patch"}},
        # Exit code: 1 → 错误
        {"timestamp": "2026-07-01T10:00:02Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                     "output": "Exit code: 1\nstderr: boom"}},
        # Exit code: 0 且正文含 "error" 词 → 不误报
        {"timestamp": "2026-07-01T10:00:03Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                     "output": "Exit code: 0\nfixed the error handling docs"}},
        # 无码显式失败前缀 → 计错
        {"timestamp": "2026-07-01T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": "execution error: Io(NotFound)"}},
        # 无码、正文含 failed 一词 → 不误报
        {"timestamp": "2026-07-01T10:00:05Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1",
                     "output": "3 tests passed, previously failed cases now green"}},
    ]
    p = tmp_path / "rollout-2026-07-01T10-00-00-x.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    u = codex_reader.aggregate_usage(p)
    assert u.tool_errors == 2
    # 错误按 call_id 归因到映射的工具名
    assert sum(u.tool_errors_by_tool.values()) == 2


def test_task_started_drives_turn_grouping(tmp_path):
    """有 task_started 时,同回合的工具与 token 步共享回合号;跨回合递增。"""
    events = [
        {"timestamp": "2026-07-01T10:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t-1",
                     "started_at": "2026-07-01T10:00:00Z"}},
        {"timestamp": "2026-07-01T10:00:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "c1",
                     "arguments": "{\"command\": [\"ls\"]}"}},
        {"timestamp": "2026-07-01T10:00:02Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}},
        {"timestamp": "2026-07-01T10:00:03Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t-2",
                     "started_at": "2026-07-01T10:00:03Z"}},
        {"timestamp": "2026-07-01T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "call_id": "c2",
                     "arguments": "{\"command\": [\"pwd\"]}"}},
        {"timestamp": "2026-07-01T10:00:05Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 250, "output_tokens": 30}}}},
    ]
    p = tmp_path / "rollout-2026-07-01T10-00-00-y.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    u = codex_reader.aggregate_usage(p)
    turns = [op.turn for op in u.tool_ops]
    assert turns == [0, 1]
    assert [t.turn for t in u.turn_stats] == [0, 1]


def test_rate_limit_peak_ttft_samples_abort_reason(tmp_path):
    events = [
        {"timestamp": "2026-07-01T10:00:00Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "rate_limits": {"limit_name": "weekly",
                                     "primary": {"used_percent": 42.5},
                                     "secondary": {"used_percent": 61.0}},
                     "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}},
        {"timestamp": "2026-07-01T10:00:01Z", "type": "event_msg",
         "payload": {"type": "task_complete", "duration_ms": 1000,
                     "time_to_first_token_ms": 800}},
        {"timestamp": "2026-07-01T10:00:02Z", "type": "event_msg",
         "payload": {"type": "task_complete", "duration_ms": 1000,
                     "time_to_first_token_ms": 300}},
        {"timestamp": "2026-07-01T10:00:03Z", "type": "event_msg",
         "payload": {"type": "turn_aborted", "reason": "interrupted",
                     "duration_ms": 50}},
    ]
    p = tmp_path / "rollout-2026-07-01T10-00-00-z.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    u = codex_reader.aggregate_usage(p)
    assert u.rate_limit_peak_used == 0.61
    assert u.time_to_first_token_ms == 300      # 保持 min 语义
    assert sorted(u.ttft_ms_samples) == [300, 800]
    assert u.abort_reasons == {"interrupted": 1}


# --- Codex Desktop (cli 0.146+) new format ---------------------------------
# File edits appear ONLY as event_msg / patch_apply_end.changes (no apply_patch
# response_item), and every tool routes through a custom_tool_call name="exec"
# JS harness wrapping tools.shell_command({command:"..."}).

def test_patch_apply_end_changes_feed_loc(tmp_path):
    """New-format edits (patch_apply_end.changes) must populate LOC."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "event_msg", "payload": {
            "type": "patch_apply_end", "success": True,
            "changes": {
                "app.py": {"type": "update",
                           "unified_diff": "@@ -1,2 +1,3 @@\n old\n-drop\n+new\n+more\n"},
                "new_mod.py": {"type": "add", "content": "line1\nline2\nline3\n"},
                "gone.py": {"type": "delete", "content": "x\ny\n"},
            },
        }},
    ])
    assert codex_reader.has_loc_signal(p) is True
    sloc = codex_reader.session_loc_full(p)
    assert sloc.added == 5              # update +2, add +3
    assert sloc.deleted == 3           # update -1, delete -2
    assert sloc.file_edit_counts == {"app.py": 1, "new_mod.py": 1, "gone.py": 1}


def test_patch_apply_end_failed_is_ignored(tmp_path):
    """A failed patch_apply_end must not count toward LOC."""
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "event_msg", "payload": {
            "type": "patch_apply_end", "success": False,
            "changes": {"app.py": {"type": "add", "content": "a\nb\n"}},
        }},
    ])
    assert codex_reader.has_loc_signal(p) is False
    assert codex_reader.session_loc_full(p).added == 0


def test_apply_patch_response_item_not_double_counted_with_patch_end(tmp_path):
    """Old rollouts carry BOTH an apply_patch response_item AND a
    patch_apply_end result event — only the response_item source counts."""
    patch = ("*** Begin Patch\n*** Update File: app.py\n@@\n-old\n+new\n+line\n"
             "*** End Patch\n")
    p = _write_jsonl(tmp_path / "s.jsonl", [
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "apply_patch", "input": patch}},
        {"type": "event_msg", "payload": {
            "type": "patch_apply_end", "success": True,
            "changes": {"app.py": {"type": "update",
                                   "unified_diff": "@@ -1 +1,2 @@\n-old\n+new\n+line\n"}}}},
    ])
    sloc = codex_reader.session_loc_full(p)
    assert sloc.added == 2             # counted once (response_item), not 4
    assert sloc.deleted == 1
    assert sloc.file_edit_counts.get("app.py") == 1


def test_exec_harness_classifies_shell_command(tmp_path):
    """custom_tool_call name=exec wraps tools.shell_command — the inner command
    drives Grep/Read/Bash classification, not a single opaque 'exec' bucket."""
    def _exec(cmd_js: str) -> dict:
        return {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "input": cmd_js}}

    p = _write_jsonl(tmp_path / "s.jsonl", [
        _exec('const r = await tools.shell_command({command:"rg -n foo .\\\\src"}); text(r)'),
        _exec('const r = await tools.shell_command({command:"Get-Content -LiteralPath a.txt"}); text(r)'),
        _exec('const r = await tools.shell_command({command:"python build.py"}); text(r)'),
    ])
    u = codex_reader.aggregate_usage(p)
    assert u.tool_calls.get("Grep") == 1
    assert u.tool_calls.get("Read") == 1
    assert u.tool_calls.get("Bash") == 1
    assert "exec" not in u.tool_calls


def test_exec_harness_utf8_command_not_mojibaked(tmp_path):
    """UTF-8 (Chinese) command bodies must survive extraction — the classifier
    only needs the leading token, but corruption would break future path hints."""
    js = ('const r = await tools.shell_command({command:"rg -n \\"性格|标签\\" .\\\\project"}); text(r)')
    cmd = codex_reader._shell_command_from_exec(js)
    assert "性格|标签" in cmd
    assert cmd.startswith("rg -n")
