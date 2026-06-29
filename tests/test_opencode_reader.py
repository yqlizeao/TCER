from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tcer.core import analyze, opencode_reader


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _make_db(path: Path, cwd: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table project (
          id text primary key,
          name text,
          worktree text
        );
        create table session (
          id text primary key,
          parent_id text,
          project_id text,
          directory text,
          title text,
          time_created integer,
          time_updated integer,
          model text,
          permission text,
          tokens_input integer,
          tokens_output integer,
          tokens_reasoning integer,
          tokens_cache_read integer,
          tokens_cache_write integer,
          cost integer,
          summary_additions integer,
          summary_deletions integer,
          summary_files integer,
          summary_diffs text
        );
        create table message (
          id text primary key,
          session_id text,
          time_created integer,
          data text
        );
        create table part (
          id text primary key,
          session_id text,
          message_id text,
          time_created integer,
          data text
        );
        """
    )
    con.execute("insert into project values (?, ?, ?)", ("proj-1", "TCER", cwd))
    con.execute(
        """
        insert into session values (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            "ses-1",
            None,
            "proj-1",
            cwd,
            "支持 OpenCode",
            1782720000000,
            1782720060000,
            _j({"providerID": "openai", "modelID": "gpt-5.2"}),
            _j({"mode": "ask"}),
            1000,
            200,
            50,
            300,
            40,
            123456,
            12,
            3,
            2,
            _j([{"path": "app.py"}, {"path": "tests/test_app.py"}]),
        ),
    )
    con.execute(
        "insert into message values (?, ?, ?, ?)",
        ("msg-user", "ses-1", 1782720001000, _j({"role": "user"})),
    )
    con.execute(
        "insert into message values (?, ?, ?, ?)",
        (
            "msg-assistant",
            "ses-1",
            1782720002000,
            _j({"role": "assistant", "providerID": "openai", "modelID": "gpt-5.2"}),
        ),
    )
    con.executemany(
        "insert into part values (?, ?, ?, ?, ?)",
        [
            ("part-text", "ses-1", "msg-user", 1782720001001, _j({"type": "text", "text": "实现 OpenCode 支持"})),
            ("part-reason", "ses-1", "msg-assistant", 1782720002001, _j({"type": "reasoning"})),
            ("part-read", "ses-1", "msg-assistant", 1782720002002, _j({"type": "tool", "tool": "read", "path": "app.py", "callID": "c1"})),
            ("part-edit", "ses-1", "msg-assistant", 1782720002003, _j({"type": "tool", "tool": "edit", "path": "app.py", "callID": "c2"})),
            ("part-bash", "ses-1", "msg-assistant", 1782720002004, _j({"type": "tool", "tool": "bash", "state": "error", "callID": "c3"})),
            ("part-img", "ses-1", "msg-user", 1782720002005, _j({"type": "file", "mime": "image/png"})),
        ],
    )
    con.commit()
    con.close()
    return path


def test_opencode_sqlite_project_refs_and_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    db = _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))

    refs = opencode_reader.list_project_refs()

    assert len(refs) == 1
    assert refs[0].source == "opencode"
    assert refs[0].path == db
    assert refs[0].display_name == "TCER"
    assert opencode_reader.sessions_for_project(refs[0]) == ["ses-1"]

    meta = opencode_reader.read_session_meta(db, "ses-1")
    assert meta.source == "opencode"
    assert meta.title == "支持 OpenCode"
    assert meta.cwd == str(tmp_path / "repo")
    assert meta.model_provider == "openai"
    assert meta.approval_policy == "ask"


def test_opencode_sqlite_usage_messages_and_loc(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    db = _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))

    u = opencode_reader.aggregate_usage(db, "ses-1")

    assert u.input_tokens == 1000
    assert u.output_tokens == 200
    assert u.reasoning_output_tokens == 50
    assert u.cache_read_input_tokens == 300
    assert u.cache_creation_input_tokens == 40
    assert u.assistant_msgs == 1
    assert u.user_msgs == 1
    assert u.thinking_count == 1
    assert u.image_count == 1
    assert u.tool_calls["Read"] == 1
    assert u.tool_calls["Edit"] == 1
    assert u.tool_calls["Bash"] == 1
    assert u.tool_errors == 1
    assert "gpt-5.2" in u.models
    assert opencode_reader.read_user_messages(db, "ses-1") == ["实现 OpenCode 支持"]

    sloc = opencode_reader.session_loc_full(db, "ses-1")
    assert opencode_reader.has_loc_signal(db, "ses-1") is True
    assert sloc.added == 12
    assert sloc.deleted == 3
    assert sloc.file_edit_counts == {"app.py": 1, "tests/test_app.py": 1}


def test_analyze_opencode_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))
    ref = opencode_reader.list_project_refs()[0]

    result = analyze.analyze_project(ref.key, source="opencode", project_ref=ref)

    assert result.source == "opencode"
    assert result.n_sessions == 1
    assert result.aggregate.usage.total == 1540
    assert result.reports[0].net_loc == 9
    assert result.aggregate.net_loc == 9


def test_opencode_sqlite_without_project_table_groups_by_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    cwd = str(tmp_path / "repo")
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table session (
          id text primary key,
          parent_id text,
          project_id text,
          directory text,
          title text,
          time_created integer,
          time_updated integer,
          model text,
          permission text,
          tokens_input integer,
          tokens_output integer,
          tokens_reasoning integer,
          tokens_cache_read integer,
          tokens_cache_write integer,
          cost integer,
          summary_additions integer,
          summary_deletions integer,
          summary_files integer,
          summary_diffs text
        );
        """
    )
    con.execute(
        "insert into session values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses-no-project",
            None,
            None,
            cwd,
            "无 project 表",
            1782720000000,
            1782720001000,
            _j({"modelID": "gpt-5.2"}),
            "{}",
            1,
            2,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            "[]",
        ),
    )
    con.commit()
    con.close()

    ref = opencode_reader.list_project_refs()[0]

    assert ref.cwd == cwd
    assert opencode_reader.sessions_for_project(ref) == ["ses-no-project"]
    meta = opencode_reader.read_session_meta(db, "ses-no-project")
    assert meta.title == "无 project 表"
