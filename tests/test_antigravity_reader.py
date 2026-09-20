from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from tcer.core import analyze, antigravity_reader, loc
from tcer.core.antigravity_reader import (
    _decode_gen_metadata,
    _decode_timestamp,
    _extract_tool_call,
    _extract_tool_result,
    _extract_user_text,
    _is_correction,
    _is_slash_command,
    _normalize_workspace_uri,
    _parse_proto_fields,
)
from tcer.core.models import ProjectRef


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _decode_varint(data: bytes, pos: int = 0) -> tuple[int, int]:
    val = 0
    shift = 0
    start = pos
    while pos < len(data):
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
    return val, pos - start


def _make_proto_field(field_num: int, wire_type: int, val: int | bytes | str) -> bytes:
    tag = (field_num << 3) | wire_type
    out = bytearray(_encode_varint(tag))
    if wire_type == 0:
        out.extend(_encode_varint(int(val)))
    elif wire_type == 2:
        b = val.encode("utf-8") if isinstance(val, str) else val
        out.extend(_encode_varint(len(b)))
        out.extend(b)
    return bytes(out)


def _make_gen_metadata_bytes(
    model: str = "gemini-3.8-flash",
    uncached_in: int = 100,
    total_out: int = 50,
    cached_in: int = 200,
    reasoning: int = 25,
) -> bytes:
    # Field 4: token usage message
    f4_sub = bytearray()
    f4_sub.extend(_make_proto_field(2, 0, uncached_in))
    f4_sub.extend(_make_proto_field(3, 0, total_out))
    f4_sub.extend(_make_proto_field(5, 0, cached_in))
    f4_sub.extend(_make_proto_field(9, 0, reasoning))

    # Field 1: nested parent message
    f1_sub = bytearray()
    f1_sub.extend(_make_proto_field(4, 2, bytes(f4_sub)))
    f1_sub.extend(_make_proto_field(19, 2, model.encode("utf-8")))

    root = bytearray()
    root.extend(_make_proto_field(1, 2, bytes(f1_sub)))
    return bytes(root)


def _make_user_step_bytes(text: str) -> bytes:
    # stype = 14: field 19 -> subfield 2 -> text
    f19_sub = bytearray()
    f19_sub.extend(_make_proto_field(2, 2, text.encode("utf-8")))

    root = bytearray()
    root.extend(_make_proto_field(19, 2, bytes(f19_sub)))
    return bytes(root)


def _make_tool_call_step_bytes(call_id: str, tool_name: str, args_dict: dict) -> bytes:
    # stype = 15: field 20 -> subfield 7 (subfield 1=id, 2=name, 3=args_json)
    f7_sub = bytearray()
    f7_sub.extend(_make_proto_field(1, 2, call_id.encode("utf-8")))
    f7_sub.extend(_make_proto_field(2, 2, tool_name.encode("utf-8")))
    f7_sub.extend(_make_proto_field(3, 2, json.dumps(args_dict).encode("utf-8")))

    f20_sub = bytearray()
    f20_sub.extend(_make_proto_field(7, 2, bytes(f7_sub)))

    root = bytearray()
    root.extend(_make_proto_field(20, 2, bytes(f20_sub)))
    return bytes(root)


def _make_tool_result_step_bytes(output_text: str) -> bytes:
    # stype = 132: field 140 -> subfield 2 -> subfield 1 -> output_text
    f2_sub = bytearray()
    f2_sub.extend(_make_proto_field(1, 2, output_text.encode("utf-8")))

    f140_sub = bytearray()
    f140_sub.extend(_make_proto_field(2, 2, bytes(f2_sub)))

    root = bytearray()
    root.extend(_make_proto_field(140, 2, bytes(f140_sub)))
    return bytes(root)


def _make_timestamp_meta(seconds: int, nanos: int = 0) -> bytes:
    # field 1 -> subfield 1=seconds, subfield 2=nanos
    f1_sub = bytearray()
    f1_sub.extend(_make_proto_field(1, 0, seconds))
    f1_sub.extend(_make_proto_field(2, 0, nanos))

    root = bytearray()
    root.extend(_make_proto_field(1, 2, bytes(f1_sub)))
    return bytes(root)


def _create_mock_session_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
    cur.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    cur.execute(
        "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
        "status INTEGER, metadata BLOB, step_payload BLOB)"
    )

    # Add trajectory_metadata_blob for cwd
    cur.execute(
        "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
        (b"something file:///C:/test/app something",),
    )

    # Add gen_metadata
    cur.execute(
        "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
        (0, _make_gen_metadata_bytes("gemini-3.8-flash", 100, 50, 200, 25)),
    )
    cur.execute(
        "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
        (1, _make_gen_metadata_bytes("gemini-3.8-flash", 150, 60, 300, 30)),
    )

    # Step 0: User input
    ts1 = _make_timestamp_meta(1700000000)
    cur.execute(
        "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
        (0, 14, 0, ts1, _make_user_step_bytes("Please create a new file")),
    )

    # Step 1: Tool call - write_to_file
    ts2 = _make_timestamp_meta(1700000005)
    write_args = {"TargetFile": r"C:\test\hello.py", "CodeContent": "print('hello')\nprint('world')\n"}
    cur.execute(
        "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
        (1, 15, 0, ts2, _make_tool_call_step_bytes("call-1", "write_to_file", write_args)),
    )

    # Step 2: Tool result - success
    ts3 = _make_timestamp_meta(1700000006)
    cur.execute(
        "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
        (2, 132, 0, ts3, _make_tool_result_step_bytes("File written successfully")),
    )

    # Step 3: Tool call - replace_file_content
    ts4 = _make_timestamp_meta(1700000010)
    edit_args = {
        "TargetFile": r"C:\test\hello.py",
        "TargetContent": "print('world')\n",
        "ReplacementContent": "print('antigravity')\nprint('super')\n",
    }
    cur.execute(
        "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
        (3, 15, 0, ts4, _make_tool_call_step_bytes("call-2", "replace_file_content", edit_args)),
    )

    # Step 4: Tool result - failed exit code
    ts5 = _make_timestamp_meta(1700000012)
    cur.execute(
        "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
        (4, 132, 0, ts5, _make_tool_result_step_bytes("exited with code 1\nCommand failed")),
    )

    conn.commit()
    conn.close()
    return path


def test_protobuf_decoding():
    # Test varint
    assert _decode_varint(_encode_varint(300), 0) == (300, 2)
    assert _decode_varint(_encode_varint(0), 0) == (0, 1)

    # Test nested gen_metadata decoding
    data = _make_gen_metadata_bytes("gemini-2.5-pro", 500, 100, 1000, 80)
    model, uncached_in, total_out, cached_in, reasoning = _decode_gen_metadata(data)
    assert model == "gemini-2.5-pro"
    assert uncached_in == 500
    assert total_out == 100
    assert cached_in == 1000
    assert reasoning == 80


def test_timestamp_decoding():
    meta = _make_timestamp_meta(1700000000)
    ts = _decode_timestamp(meta)
    assert ts == 1700000000.0


def test_user_signals():
    # Test slash commands
    assert _is_slash_command("/help me please")
    assert _is_slash_command("/goal finish task")
    assert not _is_slash_command("normal message")

    # Test correction
    assert _is_correction("不对，你的代码写错了")
    assert _is_correction("that is incorrect, please revert")
    assert not _is_correction("好的，谢谢")


def test_workspace_uri_normalization():
    assert _normalize_workspace_uri("file:///C:/GitHub/TCER") == r"C:\GitHub\TCER"
    norm = _normalize_workspace_uri("file:///C:/repo/testapp")
    assert norm == r"C:\repo\testapp"
    assert _normalize_workspace_uri("C:\\GitHub\\TCER") == r"C:\GitHub\TCER"


def test_aggregate_usage_and_loc(tmp_path: Path):
    db_path = _create_mock_session_db(tmp_path / "conversations" / "test-session.db")

    usage = antigravity_reader.aggregate_usage(db_path)
    assert usage.input_tokens == (100 + 200) + (150 + 300)  # 750
    assert usage.cache_read_input_tokens == 200 + 300  # 500
    assert usage.output_tokens == 50 + 60  # 110
    assert usage.reasoning_output_tokens == 25 + 30  # 55
    assert usage.peak_input_tokens == 450
    assert usage.user_msgs == 1
    assert usage.assistant_msgs == 2
    assert usage.tool_calls.get("Write") == 1
    assert usage.tool_calls.get("Edit") == 1
    assert usage.tool_errors == 1
    assert len(usage.turn_stats) == 2
    assert usage.turn_stats[0].tool_calls == 1
    assert usage.turn_stats[1].errors == 1
    assert usage.started_at == 1700000000 * 1000
    assert usage.ended_at == 1700000012 * 1000
    assert usage.session_duration_ms == 12 * 1000

    # LOC replay test
    sloc, has_sig = antigravity_reader.session_loc_full(db_path)
    assert has_sig is True
    # Write added 2 lines; Edit replaced 1 line with 2 lines (net +1)
    assert sloc.added == 3
    assert sloc.deleted == 0


def test_meta_and_user_messages(tmp_path: Path):
    db_path = _create_mock_session_db(tmp_path / "conversations" / "test-session.db")

    meta = antigravity_reader.read_session_meta(db_path)
    assert meta.session_id == "test-session"
    assert meta.source == "antigravity"
    assert "Please create a new file" in (meta.title or "")

    user_msgs = antigravity_reader.read_user_messages(db_path)
    assert len(user_msgs) == 1
    assert user_msgs[0] == "Please create a new file"


def test_list_and_resolve_projects(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path))
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True)
    db_path = _create_mock_session_db(conv_dir / "sess-123.db")

    # Create conversation_summaries.db
    sum_db = tmp_path / "conversation_summaries.db"
    conn = sqlite3.connect(sum_db)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, "
        "workspace_uris TEXT, last_modified_time INTEGER)"
    )
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
        ("sess-123", json.dumps(["file:///C:/GitHub/MyProject"]), 1700000000),
    )
    conn.commit()
    conn.close()

    refs = antigravity_reader.list_project_refs()
    assert len(refs) == 1
    assert refs[0].key == "MyProject"
    assert refs[0].source == "antigravity"
    assert refs[0].cwd == r"C:\GitHub\MyProject"

    resolved = antigravity_reader.resolve_project("MyProject")
    assert resolved is not None
    assert resolved.key == "MyProject"

    sessions = antigravity_reader.discover_sessions(resolved)
    assert len(sessions) == 1
    assert sessions[0].stem == "sess-123"


def test_analyze_antigravity_project_integration(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path))
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True)
    _create_mock_session_db(conv_dir / "sess-1.db")

    sum_db = tmp_path / "conversation_summaries.db"
    conn = sqlite3.connect(sum_db)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, "
        "workspace_uris TEXT, last_modified_time INTEGER)"
    )
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
        ("sess-1", json.dumps(["file:///C:/repo/testapp"]), 1700000000),
    )
    conn.commit()
    conn.close()

    pa = analyze.analyze_project("testapp", source="antigravity")
    assert pa.project_hash == "testapp"
    assert pa.source == "antigravity"
    assert pa.n_sessions == 1
    assert pa.aggregate.usage.input_tokens == 750
    assert pa.aggregate.usage.output_tokens == 110
    assert pa.aggregate.cost > 0
    assert pa.aggregate.tier is not None
