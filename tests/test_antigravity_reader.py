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
    # input_tokens 只含未命中缓存部分；缓存命中单列，total_input 才是全量输入
    assert usage.input_tokens == 100 + 150  # 250
    assert usage.cache_read_input_tokens == 200 + 300  # 500
    assert usage.total_input == 750
    assert usage.total == 750 + 110
    pm = next(iter(usage.per_model.values()))
    assert pm.input_tokens == 250 and pm.cache_read_input_tokens == 500
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
    assert pa.aggregate.usage.input_tokens == 250
    assert pa.aggregate.usage.total_input == 750
    assert pa.aggregate.usage.output_tokens == 110
    assert pa.aggregate.cost > 0
    assert pa.aggregate.tier is not None


def test_newer_tool_arg_names_and_mcp(tmp_path: Path):
    """write_file/edit_file 用 Path/Content/Target/Replacement；call_mcp_tool 映射 mcp__srv__tool。"""
    db_path = tmp_path / "conversations" / "variant.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
    cur.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    cur.execute(
        "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
        "status INTEGER, metadata BLOB, step_payload BLOB)"
    )
    calls = [
        ("write_file", {"Path": r"C:\t\a.py", "Content": "x=1\ny=2\n", "Overwrite": True}),
        ("edit_file", {"Path": r"C:\t\a.py", "Target": "y=2\n", "Replacement": "y=3\nz=4\n"}),
        ("read_file", {"Path": r"C:\t\a.py"}),
        ("call_mcp_tool", {"ServerName": "firecrawl", "ToolName": "scrape", "Arguments": {}}),
    ]
    for i, (name, args) in enumerate(calls):
        cur.execute(
            "INSERT INTO steps VALUES (?,?,?,?,?)",
            (2 * i, 15, 3, _make_timestamp_meta(1700000000 + i), _make_tool_call_step_bytes(f"c{i}", name, args)),
        )
        cur.execute(
            "INSERT INTO steps VALUES (?,?,?,?,?)",
            (2 * i + 1, 132, 3, _make_timestamp_meta(1700000000 + i), _make_tool_result_step_bytes("ok")),
        )
    conn.commit()
    conn.close()

    usage = antigravity_reader.aggregate_usage(db_path)
    assert usage.tool_calls.get("mcp__firecrawl__scrape") == 1
    assert "call_mcp_tool" not in usage.tool_calls
    assert [o.path for o in usage.tool_ops[:3]] == [r"C:\t\a.py"] * 3
    sloc, has_sig = antigravity_reader.session_loc_full(db_path)
    assert has_sig is True
    assert sloc.added == 2 + 1  # write 2 lines; edit 1 line -> 2 lines (net +1)


def _write_summaries(root: Path, rows: list[tuple[str, str, int]]) -> None:
    conn = sqlite3.connect(root / "conversation_summaries.db")
    conn.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, "
        "workspace_uris TEXT, last_modified_time INTEGER)"
    )
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
        [(cid, json.dumps([uri]), t) for cid, uri, t in rows],
    )
    conn.commit()
    conn.close()


def _setup_ccswitch_roots(home: Path) -> tuple[Path, Path]:
    """默认根 + cc-switch 隔离根：同工作区跨根、仅隔离根独有工作区、同会话 id 两根各一份。"""
    import os

    default = home / ".gemini" / "antigravity-cli"
    iso = home / ".cc-switch" / "agy" / "18ca37b1-prov" / "antigravity-cli"
    for root in (default, iso):
        (root / "conversations").mkdir(parents=True)
    _create_mock_session_db(default / "conversations" / "s-default.db")
    _create_mock_session_db(default / "conversations" / "s-dup.db")
    _write_summaries(default, [
        ("s-default", "file:///C:/repo/shared", 2),
        ("s-dup", "file:///C:/repo/shared", 1),
    ])
    _create_mock_session_db(iso / "conversations" / "s-iso.db")
    _create_mock_session_db(iso / "conversations" / "s-only.db")
    newer = _create_mock_session_db(iso / "conversations" / "s-dup.db")
    st = newer.stat()
    os.utime(newer, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _write_summaries(iso, [
        ("s-iso", "file:///C:/repo/shared", 3),
        ("s-only", "file:///C:/repo/isolated_only", 2),
        ("s-dup", "file:///C:/repo/shared", 1),
    ])
    return default, iso


def test_ccswitch_isolated_roots_merge_by_workspace(tmp_path: Path, monkeypatch):
    for env in ("ANTIGRAVITY_DIR", "ANTIGRAVITY_CLI_DIR", "GEMINI_CLI_DIR"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    default, iso = _setup_ccswitch_roots(tmp_path)

    from tcer.core.paths import antigravity_roots
    assert antigravity_roots() == [
        default,
        tmp_path / ".gemini" / "antigravity",
        tmp_path / ".gemini" / "antigravity-ide",
        tmp_path / ".config" / "antigravity",
        iso,
    ]

    refs = {r.key: r for r in antigravity_reader.list_project_refs()}
    assert set(refs) == {"shared", "isolated_only"}
    # 同工作区跨根合并成一张卡；同 id 两根各一份只计一次，取较新的文件
    shared = antigravity_reader.discover_sessions(refs["shared"])
    assert sorted(p.stem for p in shared) == ["s-default", "s-dup", "s-iso"]
    dup = next(p for p in shared if p.stem == "s-dup")
    assert dup.parent.parent == iso
    assert [p.stem for p in antigravity_reader.discover_sessions(refs["isolated_only"])] == ["s-only"]

    # 无 session_paths 的 ref 走同一套跨根分组
    bare = ProjectRef(source="antigravity", key="shared", display_name="shared", cwd=refs["shared"].cwd)
    assert sorted(antigravity_reader.discover_sessions(bare)) == sorted(shared)

    pa = analyze.analyze_project("shared", source="antigravity")
    assert pa.n_sessions == 3
    assert pa.aggregate.usage.input_tokens == 3 * 250


def test_ccswitch_scan_disabled_by_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    default, _iso = _setup_ccswitch_roots(tmp_path)
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(default))

    from tcer.core.paths import antigravity_roots
    assert antigravity_roots() == [default]
    refs = {r.key: r for r in antigravity_reader.list_project_refs()}
    assert set(refs) == {"shared"}
    assert sorted(p.stem for p in refs["shared"].session_paths) == ["s-default", "s-dup"]


# --------------------------------------------------------------------------- #
# 2026-10-27 实测格式：并行调用 / status 5 裁剪 / 失败编辑 / 压缩用量
# --------------------------------------------------------------------------- #

def _call_f7(call_id: str, name: str, args: dict) -> bytes:
    return (_make_proto_field(1, 2, call_id) + _make_proto_field(2, 2, name)
            + _make_proto_field(3, 2, json.dumps(args)))


def _multi_call_step(calls: list[tuple[str, str, dict]]) -> bytes:
    f20 = b"".join(_make_proto_field(7, 2, _call_f7(*c)) for c in calls)
    return _make_proto_field(20, 2, f20)


def _result_step(call_id: str, name: str, args: dict, out: str = "ok") -> bytes:
    """step 132：字段 5.4 回显原调用 + 字段 140 输出。"""
    f5 = _make_proto_field(4, 2, _call_f7(call_id, name, args))
    return _make_proto_field(5, 2, f5) + _make_tool_result_step_bytes(out)


def _usage_blob(unc: int, out: int, cached: int, rid: str) -> bytes:
    return (_make_proto_field(2, 0, unc) + _make_proto_field(3, 0, out)
            + _make_proto_field(5, 0, cached) + _make_proto_field(7, 2, rid))


def _gen_row(model: str, unc: int, out: int, cached: int, rid: str) -> bytes:
    f1 = _make_proto_field(4, 2, _usage_blob(unc, out, cached, rid)) + _make_proto_field(19, 2, model)
    return _make_proto_field(1, 2, f1)


def _usage_blob_no_id(unc: int, out: int, cached: int) -> bytes:
    """ccusage #2026 形态：Gemini 经 ACP/localharness 时 usage 不带
    response_id（字段 7）/provider_assigned_message_id/message_id。"""
    return (_make_proto_field(2, 0, unc) + _make_proto_field(3, 0, out)
            + _make_proto_field(5, 0, cached))


def _gen_row_unkeyed_mirror(model: str, unc: int, out: int, cached: int, step_idx: int) -> bytes:
    """ccusage #2026 实际行形态（Antigravity 1.4.1 实测解码）：同一 turn 的 usage
    在 gen_metadata 内出现两次——field 4 主 usage + field 17 retry_infos[0]
    镜像（仅 1 次尝试时也照写）——且 usage 无 id 字段；另带 step_indices（字段 2）、
    custom_metadata.request_id（字段 20）、model_display_name（字段 21）。"""
    usage = _usage_blob_no_id(unc, out, cached)
    retry = _make_proto_field(4, 2, usage)
    custom = _make_proto_field(1, 2, "request_id") + _make_proto_field(2, 2, "req-0")
    f1 = (_make_proto_field(4, 2, usage)
          + _make_proto_field(17, 2, retry)
          + _make_proto_field(19, 2, model)
          + _make_proto_field(20, 2, custom)
          + _make_proto_field(21, 2, model + "-medium"))
    return _make_proto_field(1, 2, f1) + _make_proto_field(2, 0, step_idx)


def _step_meta_with_model_usage(unc: int, out: int, cached: int, display_name: str, seconds: int) -> bytes:
    """ccusage #2026 的 steps 行形态：metadata 字段 9 model_usage 与 gen_metadata
    的 usage 逐值相同（第三份拷贝），generator_model_info（字段 24）带 display name。"""
    info = _make_proto_field(1, 2, display_name)
    return (_make_timestamp_meta(seconds)
            + _make_proto_field(9, 2, _usage_blob_no_id(unc, out, cached))
            + _make_proto_field(24, 2, info))


def _compaction_step(unc: int, out: int, cached: int, rid: str) -> bytes:
    f28 = _make_proto_field(2, 2, _usage_blob(unc, out, cached, rid))
    return _make_proto_field(5, 2, _make_proto_field(28, 2, f28))


def _pruned_user_step(text: str) -> bytes:
    """status 5 用户步骤：19.2 被清空，只剩 19.3.1 副本。"""
    f19 = _make_proto_field(3, 2, _make_proto_field(1, 2, text))
    return _make_proto_field(19, 2, f19)


def _build_db(path: Path, gens: list[bytes], steps: list[tuple[int, int, bytes]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
                 "status INTEGER, metadata BLOB, step_payload BLOB)")
    conn.executemany("INSERT INTO gen_metadata VALUES (?,?)", list(enumerate(gens)))
    conn.executemany(
        "INSERT INTO steps VALUES (?,?,?,?,?)",
        [(i, st, status, _make_timestamp_meta(1700000000 + i), pl)
         for i, (st, status, pl) in enumerate(steps)],
    )
    conn.commit()
    conn.close()
    return path


def test_parallel_calls_pruned_steps_failed_edits_and_compaction(tmp_path: Path):
    w = {"Path": r"C:\t\a.py", "Content": "a\nb\n"}
    e_ok = {"Path": r"C:\t\a.py", "Target": "b\n", "Replacement": "b\nc\n"}
    e_bad = {"Path": r"C:\t\a.py", "Target": "zzz\n", "Replacement": "1\n2\n3\n"}
    e_pruned = {"Path": r"C:\t\b.py", "Target": "", "Replacement": "x\ny\n"}
    db = _build_db(tmp_path / "conversations" / "s.db", [
        _gen_row("gemini-3.8-flash", 100, 10, 0, "rid-a"),
        _gen_row("gemini-4-argon", 200, 20, 1000, "rid-b"),
        _gen_row("gemini-4-argon", 300, 30, 2000, "rid-c"),
    ], [
        (14, 5, _pruned_user_step("帮我改一下")),                       # 0 裁剪的用户提问
        (15, 5, _make_proto_field(20, 2, b"")),                          # 1 裁剪的调用（turn 0）
        (132, 5, _result_step("p1", "edit_file", e_pruned)),             # 2 → 由结果补回
        (15, 3, _multi_call_step([("c1", "write_file", w),               # 3 并行三调用（turn 1）
                                  ("c2", "edit_file", e_ok),
                                  ("c3", "manage_task", {"Action": "status"})])),
        (132, 3, _result_step("c1", "write_file", w)),
        (132, 3, _result_step("c2", "edit_file", e_ok)),
        (132, 3, _result_step("c3", "manage_task", {"Action": "status"})),
        (23, 3, _compaction_step(5000, 700, 40000, "rid-z")),            # 7 压缩（不在 gen 里）
        (23, 3, _compaction_step(1, 1, 1, "rid-b")),                     # 8 已在 gen 里 → 不重复计
        (15, 3, _multi_call_step([("c4", "edit_file", e_bad)])),         # 9 turn 2
        (132, 7, _result_step("c4", "edit_file", e_bad, "Could not successfully apply")),
    ])

    u = antigravity_reader.aggregate_usage(db)
    # 工具：裁剪补回 1 + 并行 3 + 失败编辑 1；manage_task 不再冒充子代理 Task
    assert u.tool_calls == {"Edit": 3, "Write": 1, "manage_task": 1}
    assert "Task" not in u.tool_calls
    assert [t.tool_calls for t in u.turn_stats] == [1, 3, 1]
    # status 5 不是错误；status 7 才是
    assert u.tool_errors == 1
    # 压缩：rid-z 计入并按前一次生成（gemini-4-argon）计价；rid-b 已在 gen 里不重复
    assert u.compaction_count == 2
    assert u.compaction_turns == [2, 2]
    assert u.input_tokens == 100 + 200 + 300 + 5000
    assert u.output_tokens == 10 + 20 + 30 + 700
    assert u.cache_read_input_tokens == 1000 + 2000 + 40000
    argon = u.per_model["gemini-4-argon"]
    assert (argon.input_tokens, argon.output_tokens, argon.cache_read_input_tokens) == (5500, 750, 43000)
    # 裁剪的提问取 19.3.1 副本，不出乱码
    assert antigravity_reader.read_user_messages(db) == ["帮我改一下"]

    sloc, has_sig = antigravity_reader.session_loc_full(db)
    assert has_sig is True
    # 补回的裁剪编辑 +2（b.py）、write +2、成功 edit +1（b→b,c 行级差分）；失败编辑（+3）不回放
    assert sloc.added == 2 + 2 + 1
    assert sloc.deleted == 0
    assert sloc.file_edit_counts == {r"C:\t\b.py": 1, r"C:\t\a.py": 2}


def test_ccusage_2026_unkeyed_mirror_copies_count_once(tmp_path: Path):
    """ccusage #2026/#2027 三倍计数：TCER 未中招（合成复现验证）。

    ccusage 因同时读三处 usage（gen_metadata field 4 + 同行 retry_infos[0]
    镜像 + steps metadata field 9 model_usage），无 id 时去重失效而计 3x。
    TCER 只读 gen_metadata field 4（不解析 retry_infos；steps 只读 stype 23
    的压缩用量），所以同一 turn 的三份拷贝只计 1x。本测试按 #2026 实际行形态
    （issue 给出的 gen_metadata[0]/steps[1] 解码值）构造数据并锁定该行为：
    若将来 reader 开始读 steps model_usage 或 retry_infos，必须先补去重，
    否则本测试失败。
    """
    from tcer.core.antigravity_reader import _gen_response_id

    db_path = tmp_path / "conversations" / "s.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    gens = [
        _gen_row_unkeyed_mirror("gemini-4-argon", 15027, 49, 5000, 1),
        _gen_row_unkeyed_mirror("gemini-4-argon", 20000, 100, 7000, 3),
    ]
    # usage 确实无 id（复现前提）；若上游开始填 id，_gen_response_id 应返回它
    assert _gen_response_id(gens[0]) == ""
    assert _decode_gen_metadata(gens[0]) == ("gemini-4-argon", 15027, 49, 5000, 0)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
                 "status INTEGER, metadata BLOB, step_payload BLOB)")
    conn.executemany("INSERT INTO gen_metadata VALUES (?,?)", list(enumerate(gens)))
    conn.executemany(
        "INSERT INTO steps VALUES (?,?,?,?,?)",
        [
            (0, 14, 0, _make_timestamp_meta(1700000000), _make_user_step_bytes("帮我写个函数")),
            (1, 15, 0, _step_meta_with_model_usage(15027, 49, 5000, "gemini-4-argon-medium", 1700000001),
             _make_tool_call_step_bytes("c1", "read_file", {"Path": r"C:\t\a.py"})),
            (2, 14, 0, _make_timestamp_meta(1700000002), _make_user_step_bytes("再改一下")),
            (3, 15, 0, _step_meta_with_model_usage(20000, 100, 7000, "gemini-4-argon-medium", 1700000003),
             _make_tool_call_step_bytes("c2", "read_file", {"Path": r"C:\t\a.py"})),
        ],
    )
    conn.commit()
    conn.close()

    u = antigravity_reader.aggregate_usage(db_path)
    # 1x 而非 3x：两回合各计一次（15027+20000 / 49+100 / 5000+7000）
    assert u.input_tokens == 35027
    assert u.output_tokens == 149
    assert u.cache_read_input_tokens == 12000
    # steps 的 model_usage 拷贝不得另起 "gemini-4-argon-medium" 分桶
    assert set(u.per_model) == {"gemini-4-argon"}
    assert [(t.input_tokens, t.output_tokens, t.cache_read) for t in u.turn_stats] == [
        (15027, 49, 5000), (20000, 100, 7000),
    ]


def test_loc_waits_for_result_and_pairs_in_order_without_call_echo(tmp_path: Path):
    """结果步骤缺字段 5.4（旧格式）时按调用顺序配对；无结果的尾部调用不回放（#32）。"""
    w1 = {"Path": r"C:\t\a.py", "Content": "1\n"}
    w2 = {"Path": r"C:\t\b.py", "Content": "1\n2\n"}
    w3 = {"Path": r"C:\t\c.py", "Content": "1\n2\n3\n"}
    db = _build_db(tmp_path / "conversations" / "s.db", [], [
        (15, 3, _multi_call_step([("c1", "write_file", w1), ("c2", "write_file", w2)])),
        (132, 3, _make_tool_result_step_bytes("ok")),
        (132, 7, _make_tool_result_step_bytes("failed")),
        (15, 3, _multi_call_step([("c3", "write_file", w3)])),
    ])
    sloc, _ = antigravity_reader.session_loc_full(db)
    assert sloc.file_edit_counts == {r"C:\t\a.py": 1}
    assert sloc.added == 1


# --------------------------------------------------------------------------- #
# 2026-11-10 桌面 App + IDE 支持：多根发现、summaries 不全兜底、backup 排除
# --------------------------------------------------------------------------- #

def _setup_surface_roots(home: Path) -> dict[str, Path]:
    """CLI + App + IDE + config 四根 + backup 根（备份副本，必须排除）。"""
    roots: dict[str, Path] = {}
    for name, sub in (
        ("cli", ".gemini/antigravity-cli"),
        ("app", ".gemini/antigravity"),
        ("ide", ".gemini/antigravity-ide"),
        ("cfg", ".config/antigravity"),
        ("bak", ".gemini/antigravity-backup"),
    ):
        root = home / sub
        (root / "conversations").mkdir(parents=True)
        roots[name] = root
    return roots


def test_app_ide_config_roots_discovered_and_backup_excluded(tmp_path: Path, monkeypatch):
    for env in ("ANTIGRAVITY_DIR", "ANTIGRAVITY_CLI_DIR", "GEMINI_CLI_DIR"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    roots = _setup_surface_roots(tmp_path)
    _create_mock_session_db(roots["cli"] / "conversations" / "s-cli.db")
    _create_mock_session_db(roots["app"] / "conversations" / "s-app.db")
    _create_mock_session_db(roots["ide"] / "conversations" / "s-ide.db")
    _create_mock_session_db(roots["cfg"] / "conversations" / "s-cfg.db")
    _create_mock_session_db(roots["bak"] / "conversations" / "s-bak.db")
    _write_summaries(roots["cli"], [("s-cli", "file:///C:/repo/cli-proj", 5)])
    _write_summaries(roots["app"], [("s-app", "file:///C:/repo/app-proj", 5)])
    _write_summaries(roots["ide"], [("s-ide", "file:///C:/repo/ide-proj", 5)])
    _write_summaries(roots["cfg"], [("s-cfg", "file:///C:/repo/cfg-proj", 5)])
    _write_summaries(roots["bak"], [("s-bak", "file:///C:/repo/bak-proj", 5)])

    from tcer.core.paths import antigravity_roots
    assert antigravity_roots() == [
        roots["cli"], roots["app"], roots["ide"], roots["cfg"],
    ]

    refs = {r.key: r for r in antigravity_reader.list_project_refs()}
    assert set(refs) == {"cli-proj", "app-proj", "ide-proj", "cfg-proj"}
    assert "bak-proj" not in refs
    for key, stem in (("cli-proj", "s-cli"), ("app-proj", "s-app"),
                      ("ide-proj", "s-ide"), ("cfg-proj", "s-cfg")):
        assert [p.stem for p in antigravity_reader.discover_sessions(refs[key])] == [stem]

    pa = analyze.analyze_project("app-proj", source="antigravity")
    assert pa.n_sessions == 1
    assert pa.aggregate.usage.input_tokens == 250


def test_summaries_partial_rows_recovered_by_glob(tmp_path: Path, monkeypatch):
    """summaries 只剩部分行（GUI Language Server 刷库 bug）：漏掉的会话由 glob 从 blob 找回。"""
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path))
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True)
    _create_mock_session_db(conv_dir / "s-listed.db")
    _create_mock_session_db(conv_dir / "s-wiped.db")
    _write_summaries(tmp_path, [("s-listed", "file:///C:/repo/partial-proj", 5)])
    # s-wiped 不在 summaries 里，只能从 blob 的 file:///C:/test/app 找回

    refs = {r.key: r for r in antigravity_reader.list_project_refs()}
    # blob 是 b"something file:///C:/test/app something"，兜底正则把空格后的
    # "something" 也吞进工作区（既有行为）；关键是会话被找回，不是 key 的拼写
    assert set(refs) == {"partial-proj", "app something"}
    assert [p.stem for p in antigravity_reader.discover_sessions(refs["partial-proj"])] == ["s-listed"]
    assert [p.stem for p in antigravity_reader.discover_sessions(refs["app something"])] == ["s-wiped"]


def test_no_summaries_glob_only(tmp_path: Path, monkeypatch):
    """根目录没有 summaries 库：只靠 glob + blob 发现（App/IDE 未证实有自己的库）。"""
    monkeypatch.setenv("ANTIGRAVITY_DIR", str(tmp_path))
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True)
    _create_mock_session_db(conv_dir / "s-blob.db")

    refs = antigravity_reader.list_project_refs()
    # 同上：blob 兜底正则把 "something" 吞进工作区名（既有行为）
    assert [r.key for r in refs] == ["app something"]
    assert [p.stem for p in antigravity_reader.discover_sessions(refs[0])] == ["s-blob"]


def test_cross_surface_same_session_dedup(tmp_path: Path, monkeypatch):
    """同一会话 id 在 CLI 与 App 各一份：只计一次，取 mtime 较新的文件。"""
    import os

    for env in ("ANTIGRAVITY_DIR", "ANTIGRAVITY_CLI_DIR", "GEMINI_CLI_DIR"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cli = tmp_path / ".gemini" / "antigravity-cli"
    app = tmp_path / ".gemini" / "antigravity"
    for root in (cli, app):
        (root / "conversations").mkdir(parents=True)
    _create_mock_session_db(cli / "conversations" / "s-dup.db")
    newer = _create_mock_session_db(app / "conversations" / "s-dup.db")
    st = newer.stat()
    os.utime(newer, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _write_summaries(cli, [("s-dup", "file:///C:/repo/shared", 1)])
    _write_summaries(app, [("s-dup", "file:///C:/repo/shared", 2)])

    refs = {r.key: r for r in antigravity_reader.list_project_refs()}
    assert set(refs) == {"shared"}
    shared = antigravity_reader.discover_sessions(refs["shared"])
    assert [p.stem for p in shared] == ["s-dup"]
    assert shared[0].parent.parent == app

    pa = analyze.analyze_project("shared", source="antigravity")
    assert pa.n_sessions == 1
    assert pa.aggregate.usage.input_tokens == 250
