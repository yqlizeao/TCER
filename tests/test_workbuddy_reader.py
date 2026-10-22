from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from tcer.core import analyze, loc, metrics, workbuddy_reader
from tcer.core.models import ProjectRef
from tcer.core.paths import encode_hash

_BACKEND = Path(__file__).resolve().parents[1] / "server" / "backend"
sys.path.insert(0, str(_BACKEND))

_SAMPLE_FILES = [
    Path(r"C:\Users\Administrator\Downloads\c3eb82a1-6400-44c5-ad03-3925b7e54d65.jsonl"),
    Path(r"C:\Users\Administrator\Downloads\7c328a12-9546-420c-95eb-dfa9c058d11c.jsonl"),
    Path(r"C:\Users\Administrator\Downloads\3618ea5d-4730-4b37-84ef-62caa63ef2d7.jsonl"),
]


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """A fresh server db module pointed at an isolated sqlite file."""
    monkeypatch.setenv("TCER_SERVER_DB", str(tmp_path / "workbuddy_test.db"))
    mod = importlib.import_module("db")
    mod = importlib.reload(mod)
    mod.init_db()
    return mod


def test_clean_user_text():
    # 1. System reminder stripping with attributes
    raw1 = (
        '<system-reminder data-role="user-context">\n'
        '<user_info>\nOS Version: win32\n</user_info>\n'
        '</system-reminder>\n'
        '<user_query>Hello world</user_query>'
    )
    assert workbuddy_reader.clean_user_text(raw1) == "Hello world"

    # 2. Quote unwrapping
    raw2 = (
        '<user_query><message_quote type="text" source_message_id="123">\n'
        '<original_text><![CDATA[some quoted code]]></original_text>\n'
        '</message_quote>\nPlease review this code</user_query>'
    )
    cleaned2 = workbuddy_reader.clean_user_text(raw2)
    assert "> some quoted code" in cleaned2
    assert "Please review this code" in cleaned2

    # 3. Plain text
    assert workbuddy_reader.clean_user_text("Plain prompt") == "Plain prompt"


def test_project_root_detection_and_aggregation(tmp_path):
    # Create two synthetic sessions with different timestamped sandbox cwds,
    # but operating on files in the same business repository (E:\my_game).
    sess1 = tmp_path / "sess1.jsonl"
    sess2 = tmp_path / "sess2.jsonl"

    sess1_lines = [
        {"type": "session-meta", "sessionId": "s-1", "timestamp": 1700000000000},
        {"type": "ai-title", "aiTitle": "任务1", "sessionId": "s-1", "cwd": r"C:\Users\User\WorkBuddy\2026-10-16-10-00-00"},
        {
            "id": "m1",
            "type": "message",
            "role": "user",
            "timestamp": 1700000001000,
            "content": [{"type": "input_text", "text": "增加玩家模块"}],
        },
        {
            "id": "f1",
            "type": "function_call",
            "name": "Write",
            "callId": "call-1",
            "timestamp": 1700000002000,
            "arguments": json.dumps({"file_path": "E:/my_game/Source/Player.cpp", "content": "class Player {};\n"}),
            "providerData": {
                "messageId": "msg-step-1",
                "model": "gemini-3.8-flash",
                "rawUsage": {"prompt_tokens": 1000, "completion_tokens": 50},
            },
        },
    ]

    sess2_lines = [
        {"type": "session-meta", "sessionId": "s-2", "timestamp": 1700000010000},
        {"type": "ai-title", "aiTitle": "任务2", "sessionId": "s-2", "cwd": r"C:\Users\User\WorkBuddy\2026-10-16-14-30-00"},
        {
            "id": "m2",
            "type": "message",
            "role": "user",
            "timestamp": 1700000011000,
            "content": [{"type": "input_text", "text": "编写配置表"}],
        },
        {
            "id": "f2",
            "type": "function_call",
            "name": "Edit",
            "callId": "call-2",
            "timestamp": 1700000012000,
            "arguments": json.dumps({
                "file_path": "E:/my_game/Config/settings.json",
                "old_string": "",
                "new_string": "{\n  \"max_hp\": 100\n}\n",
            }),
            "providerData": {
                "messageId": "msg-step-2",
                "model": "gemini-3.8-flash",
                "rawUsage": {"prompt_tokens": 1200, "completion_tokens": 40},
            },
        },
    ]

    with sess1.open("w", encoding="utf-8") as f:
        for l in sess1_lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")
    with sess2.open("w", encoding="utf-8") as f:
        for l in sess2_lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")

    # 1. Project directory extraction
    pdir1 = workbuddy_reader.extract_project_dir_from_session(sess1)
    pdir2 = workbuddy_reader.extract_project_dir_from_session(sess2)
    assert pdir1 is not None and "my_game" in pdir1
    assert pdir2 is not None and "my_game" in pdir2
    assert pdir1 == pdir2

    # 2. list_project_refs must group both sessions under ONE project ref
    refs = workbuddy_reader.list_project_refs(tmp_path)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.source == "workbuddy"
    assert ref.display_name == "my_game"
    assert set(ref.session_paths) == {sess1, sess2}

    # 3. Analyze the aggregated project
    analysis = analyze.analyze_workbuddy_project(ref)
    assert analysis.n_sessions == 2
    assert len(analysis.reports) == 2
    assert analysis.aggregate.net_loc == 4  # 1 line in Player.cpp + 3 lines in settings.json
    assert analysis.aggregate.usage.total == 2290  # (1000+50) + (1200+40)


def test_synthetic_session(tmp_path):
    p = tmp_path / "test_session.jsonl"
    lines = [
        {"type": "session-meta", "sessionId": "sess-001", "timestamp": 1700000000000},
        {"type": "ai-title", "aiTitle": "测试小兵换装", "sessionId": "sess-001", "cwd": "c:\\slg"},
        {
            "id": "m1",
            "type": "message",
            "role": "user",
            "timestamp": 1700000001000,
            "content": [
                {"type": "input_text", "text": '<system-reminder data-role="ctx">junk</system-reminder><user_query>帮我重构代码</user_query>'}
            ],
        },
        {
            "id": "r1",
            "type": "reasoning",
            "timestamp": 1700000002000,
            "rawContent": [{"type": "reasoning_text", "text": "思考中：重构代码方案..."}],
        },
        {
            "id": "f1",
            "type": "function_call",
            "name": "Write",
            "callId": "call-1",
            "timestamp": 1700000003000,
            "arguments": json.dumps({"file_path": "c:/slg/src/main.py", "content": "print('hello')\nprint('world')\n"}),
            "providerData": {
                "messageId": "msg-step-1",
                "model": "gemini-3.8-flash",
                "rawUsage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 800},
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            },
        },
        {
            "id": "fr1",
            "type": "function_call_result",
            "name": "Write",
            "callId": "call-1",
            "status": "completed",
            "timestamp": 1700000004000,
            "output": {"type": "text", "text": "File written successfully"},
        },
        {
            "id": "m2",
            "type": "message",
            "role": "assistant",
            "timestamp": 1700000005000,
            "content": [{"type": "output_text", "text": "已成功重构代码。"}],
            "providerData": {
                "messageId": "msg-step-2",
                "model": "gemini-3.8-flash",
                "rawUsage": {
                    "prompt_tokens": 1100,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 900},
                },
            },
        },
    ]
    with p.open("w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")

    # Meta
    meta = workbuddy_reader.read_session_meta(p)
    assert meta.session_id == "sess-001"
    assert meta.title == "测试小兵换装"
    assert meta.cwd.lower() == "c:\\slg"
    assert meta.source == "workbuddy"

    # Usage
    u = workbuddy_reader.aggregate_usage(p)
    assert u.input_tokens == 2100
    assert u.output_tokens == 90
    assert u.cache_read_input_tokens == 1700
    assert u.reasoning_output_tokens == 30
    assert u.peak_input_tokens == 1100
    assert u.user_msgs == 1
    assert u.tool_calls.get("Write") == 1
    assert "gemini-3.8-flash" in u.models

    # LOC
    sloc = workbuddy_reader.session_loc_full(p)
    assert sloc.added == 2
    assert sloc.deleted == 0
    assert workbuddy_reader.has_loc_signal(p) is True

    # User messages
    msgs = workbuddy_reader.read_user_messages(p)
    assert msgs == ["帮我重构代码"]

    # Conversation
    conv = workbuddy_reader.read_conversation(p)
    assert len(conv) == 5
    assert conv[0]["role"] == "user" and conv[0]["text"] == "帮我重构代码"
    assert conv[1]["role"] == "assistant" and conv[1]["type"] == "thinking"
    assert conv[2]["role"] == "assistant" and conv[2]["type"] == "tool_use" and conv[2]["name"] == "Write"
    assert conv[3]["role"] == "tool" and conv[3]["type"] == "tool_result" and conv[3]["is_error"] is False
    assert conv[4]["role"] == "assistant" and conv[4]["type"] == "text" and conv[4]["text"] == "已成功重构代码。"

    # Full session parse
    row = workbuddy_reader.parse_session_file(p)
    assert row["session_id"] == "sess-001"
    assert row["project"] == "slg"
    assert row["source"] == "workbuddy"
    assert row["net_loc"] == 2
    assert len(row["conversation"]) == 5


def test_real_downloads_files_if_exist(test_db):
    for idx, p in enumerate(_SAMPLE_FILES, start=1):
        if not p.is_file():
            continue
        row = workbuddy_reader.parse_session_file(p)
        assert row["source"] == "workbuddy"
        assert row["session_id"] == p.stem
        assert len(row["conversation"]) > 0

        # Test inserting into server db
        test_db.insert_records(
            uploaded_by="admin",
            person="admin",
            project=row["project"],
            aggregate=None,
            sessions=[row],
            generated_at=1700000000,
            semantic_consent=True,
        )

        detail = test_db.session_detail(idx, viewer="admin")
        assert detail is not None
        assert detail["source"] == "workbuddy"
        assert detail["has_transcript"] is True
        assert len(detail["raw"]["conversation"]) == len(row["conversation"])


def test_real_workbuddy_ai_projects_discovery_and_dialog_grouping():
    roots = workbuddy_reader.workbuddy_roots()
    if not any(".workbuddy-ai" in str(r) for r in roots):
        pytest.skip("本机无 ~/.workbuddy-ai，跳过依赖本机环境的测试")

    refs = workbuddy_reader.list_project_refs()
    if not refs:
        pytest.skip("~/.workbuddy-ai 下无会话")

    # 1. 普通会话合并为 WorkBuddy
    wb_ref = next((r for r in refs if r.display_name == "WorkBuddy"), None)
    assert wb_ref is not None
    assert wb_ref.key == "workbuddy"

    # 2. 其他会话缩减到目录名，如 cicd 和 vxtest
    names = {r.display_name for r in refs}
    assert "WorkBuddy" in names
    assert "cicd" in names or "vxtest" in names

    # 3. 验证真实项目上传弹窗分组聚合：显示名缩减为目录名
    grouped = analyze.real_projects(refs)
    group_displays = {g.display for g in grouped}
    assert "WorkBuddy" in group_displays
    for g in grouped:
        assert not any(k in g.display for k in ("c-Users-Administrator", "WorkBuddy AI-2026"))
