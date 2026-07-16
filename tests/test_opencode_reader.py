from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tcer.core import analyze, opencode_reader


@pytest.fixture(autouse=True)
def _clear_data_dirs_cache():
    """Clear _data_dirs() LRU cache so env/monkeypatch changes take effect."""
    opencode_reader._data_dirs.cache_clear()
    yield
    opencode_reader._data_dirs.cache_clear()


def test_classify_opencode_tool_aliases():
    assert opencode_reader._classify_tool("todowrite", {}) == ("TodoWrite", "")
    assert opencode_reader._classify_tool("websearch", {}) == ("WebSearch", "")
    assert opencode_reader._classify_tool("webfetch", {}) == ("WebFetch", "")
    assert opencode_reader._classify_tool("question", {}) == ("AskUserQuestion", "")
    assert opencode_reader._classify_tool("edit", {"path": "a.py"}) == ("Edit", "a.py")
    # Live OpenCode nests path under state.input.filePath
    assert opencode_reader._classify_tool(
        "edit",
        {"type": "tool", "tool": "edit", "state": {"input": {"filePath": "src/a.py"}}},
    ) == ("Edit", "src/a.py")


def test_path_hint_and_normalize_tool_input():
    data = {
        "type": "tool",
        "tool": "edit",
        "state": {
            "status": "completed",
            "input": {
                "filePath": "app.py",
                "oldString": "a\n",
                "newString": "a\nb\n",
            },
        },
    }
    assert opencode_reader._path_hint(data) == "app.py"
    inp = opencode_reader._normalize_tool_input(data)
    assert inp["file_path"] == "app.py"
    assert inp["old_string"] == "a\n"
    assert inp["new_string"] == "a\nb\n"
    assert opencode_reader._part_is_error(data) is False
    assert opencode_reader._part_is_error(
        {"state": {"status": "error", "error": "boom"}}
    ) is True


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
    # Reasoning is folded into output (OpenCode stores it separately).
    assert u.reasoning_output_tokens == 50
    assert u.output_tokens == 200 + 50
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
    # No step-finish parts in fixture → fall back to session total input as peak.
    assert u.peak_input_tokens == 1000 + 40 + 300
    assert opencode_reader.read_user_messages(db, "ses-1") == ["实现 OpenCode 支持"]


def test_opencode_reasoning_ratio_at_most_one(tmp_path, monkeypatch):
    """Folded reasoning keeps reasoning_output_ratio in [0, 1] (live OpenCode)."""
    from tcer.core import metrics
    from tcer.core.models import SessionMeta

    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    db = _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))
    u = opencode_reader.aggregate_usage(db, "ses-1")
    meta = SessionMeta(
        session_id="ses-1", cwd=str(tmp_path / "repo"), title=None,
        path=db, is_subagent=False, entrypoint="opencode", source="opencode",
    )
    r = metrics.compute(meta, u, net_loc=None)
    assert r.reasoning_output_ratio is not None
    assert 0.0 <= r.reasoning_output_ratio <= 1.0
    assert r.reasoning_output_ratio == pytest.approx(50 / 250)


def test_opencode_peak_input_from_step_finish(tmp_path, monkeypatch):
    """Peak must come from step-finish snapshots, not session-summed totals."""
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    cwd = str(tmp_path / "repo")
    db = _make_db(tmp_path / "opencode.db", cwd)
    con = sqlite3.connect(db)
    # Session totals are the SUM of steps (live OpenCode shape).
    con.execute(
        "update session set tokens_input=300, tokens_output=30, "
        "tokens_cache_read=1000, tokens_cache_write=0, tokens_reasoning=0 "
        "where id='ses-1'"
    )
    con.executemany(
        "insert into part values (?, ?, ?, ?, ?)",
        [
            (
                "part-step1", "ses-1", "msg-assistant", 1782720002010,
                _j({
                    "type": "step-finish",
                    "tokens": {
                        "input": 100, "output": 10, "reasoning": 0,
                        "cache": {"read": 200, "write": 0},
                    },
                }),
            ),
            (
                "part-step2", "ses-1", "msg-assistant", 1782720002020,
                _j({
                    "type": "step-finish",
                    "tokens": {
                        "input": 200, "output": 20, "reasoning": 0,
                        "cache": {"read": 800, "write": 0},
                    },
                }),
            ),
        ],
    )
    con.commit()
    con.close()

    u = opencode_reader.aggregate_usage(db, "ses-1")
    assert u.input_tokens == 300
    assert u.cache_read_input_tokens == 1000
    # Peak = max(100+200, 200+800) = 1000 — not session sum 1300.
    assert u.peak_input_tokens == 1000

    sloc = opencode_reader.session_loc_full(db, "ses-1")
    assert opencode_reader.has_loc_signal(db, "ses-1") is True
    assert sloc.added == 12
    assert sloc.deleted == 3
    assert sloc.file_edit_counts == {"app.py": 1, "tests/test_app.py": 1}
    # Nested state.input.filePath is recorded on tool_ops (live OpenCode shape).
    edit_ops = [op for op in u.tool_ops if op.tool == "Edit"]
    assert edit_ops and edit_ops[0].path == "app.py"


def test_opencode_loc_replays_tool_parts_when_summary_empty(tmp_path, monkeypatch):
    """Real OpenCode often leaves summary_* at 0; LOC must come from edit parts."""
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    cwd = str(tmp_path / "repo")
    db = _make_db(tmp_path / "opencode.db", cwd)
    con = sqlite3.connect(db)
    # Zero out summary counters — the live-data failure mode.
    con.execute(
        "update session set summary_additions=0, summary_deletions=0, "
        "summary_files=0, summary_diffs=null where id='ses-1'"
    )
    # Replace stub edit part with full live-shaped payload; add a Write.
    con.execute("delete from part where id in ('part-edit')")
    edit_part = {
        "type": "tool",
        "tool": "edit",
        "callID": "c2",
        "state": {
            "status": "completed",
            "input": {
                "filePath": "app.py",
                "oldString": "x = 1\n",
                "newString": "x = 1\ny = 2\n",
            },
        },
    }
    write_part = {
        "type": "tool",
        "tool": "write",
        "callID": "c4",
        "state": {
            "status": "completed",
            "input": {
                "filePath": "new_mod.py",
                "content": "def f():\n    return 1\n",
            },
        },
    }
    con.executemany(
        "insert into part values (?, ?, ?, ?, ?)",
        [
            ("part-edit", "ses-1", "msg-assistant", 1782720002003, _j(edit_part)),
            ("part-write", "ses-1", "msg-assistant", 1782720002006, _j(write_part)),
        ],
    )
    con.commit()
    con.close()

    assert opencode_reader.has_loc_signal(db, "ses-1") is True
    sloc = opencode_reader.session_loc_full(db, "ses-1")
    # Edit: +1 line; Write: +2 lines (prior 0 / unseen)
    assert sloc.added >= 1
    assert "app.py" in sloc.file_edit_counts
    assert "new_mod.py" in sloc.file_edit_counts
    assert sloc.added - sloc.deleted > 0

    u = opencode_reader.aggregate_usage(db, "ses-1")
    assert u.tool_calls.get("Edit") == 1
    assert u.tool_calls.get("Write") == 1
    assert any(op.path == "app.py" for op in u.tool_ops if op.tool == "Edit")
    assert any(op.path == "new_mod.py" for op in u.tool_ops if op.tool == "Write")


def test_read_conversation_maps_opencode_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    db = _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))

    convo = opencode_reader.read_conversation(db, "ses-1")
    kinds = [(b["role"], b["type"]) for b in convo]
    # part-reason has no text so the thinking block is dropped; the file part is
    # not conversational and is skipped. Text + three tool calls remain.
    assert kinds == [
        ("user", "text"),
        ("assistant", "tool_use"),
        ("assistant", "tool_use"),
        ("assistant", "tool_use"),
    ]
    assert convo[0]["text"] == "实现 OpenCode 支持"
    assert convo[1]["name"] == "Read"
    assert convo[3]["name"] == "Bash"


def test_analyze_opencode_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    _make_db(tmp_path / "opencode.db", str(tmp_path / "repo"))
    ref = opencode_reader.list_project_refs()[0]

    result = analyze.analyze_project(ref.key, source="opencode", project_ref=ref)

    assert result.source == "opencode"
    assert result.n_sessions == 1
    # total includes folded reasoning (50) into output: 1000+40+300+250 = 1590
    assert result.aggregate.usage.total == 1590
    assert result.reports[0].net_loc == 9
    assert result.aggregate.net_loc == 9


def test_global_worktree_slash_groups_by_session_directory(tmp_path, monkeypatch):
    """OpenCode ``project.worktree='/'`` must not collapse distinct chat dirs."""
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table project (id text primary key, name text, worktree text);
        create table session (
          id text primary key, parent_id text, project_id text, directory text,
          title text, time_created integer, time_updated integer, model text,
          permission text, tokens_input integer, tokens_output integer,
          tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer,
          cost integer, summary_additions integer, summary_deletions integer,
          summary_files integer, summary_diffs text
        );
        """
    )
    con.execute("insert into project values ('global', null, '/')")
    for i, directory in enumerate(("C:/playground/langfuse", "C:/playground/other"), start=1):
        con.execute(
            """
            insert into session values (
              ?, null, 'global', ?, ?, 1, 2, '{}', '{}',
              10, 5, 0, 0, 0, 0, 0, 0, 0, null
            )
            """,
            (f"ses-{i}", directory, f"t{i}"),
        )
    con.commit()
    con.close()

    refs = opencode_reader.list_project_refs()
    assert len(refs) == 2
    cwds = {r.cwd for r in refs}
    assert cwds == {"C:/playground/langfuse", "C:/playground/other"}
    assert all(r.display_name in ("langfuse", "other") for r in refs)
    # Sessions scoped to directory, not entire global project
    for r in refs:
        sids = opencode_reader.sessions_for_project(r)
        assert len(sids) == 1


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


def test_opencode_discovers_windows_localappdata_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_DATA_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    db = _make_db(
        tmp_path / "LocalAppData" / "opencode" / "data" / "opencode.db",
        str(tmp_path / "repo"),
    )

    refs = opencode_reader.list_project_refs()

    assert any(ref.path == db for ref in refs)


def test_opencode_env_override_accepts_comma_separated_dirs(tmp_path, monkeypatch):
    first = tmp_path / "missing"
    second = tmp_path / "actual"
    monkeypatch.setenv("OPENCODE_DATA_DIR", f"{first},{second}")
    db = _make_db(second / "opencode.sqlite3", str(tmp_path / "repo"))

    refs = opencode_reader.list_project_refs()

    assert [ref.path for ref in refs] == [db]


def test_opencode_legacy_storage_reads_split_message_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    session_dir = tmp_path / "storage" / "session" / "proj-legacy"
    message_dir = tmp_path / "storage" / "message"
    session_dir.mkdir(parents=True)
    message_dir.mkdir(parents=True)
    session_file = session_dir / "ses-legacy.json"
    session_file.write_text(
        _j({
            "id": "ses-legacy",
            "projectID": "proj-legacy",
            "title": "legacy",
            "tokens": {"input": 10, "output": 2},
        }),
        encoding="utf-8",
    )
    (message_dir / "ses-legacy.json").write_text(
        _j({
            "messages": [
                {"role": "user", "text": "旧版消息"},
                {"role": "assistant", "text": "ok"},
            ]
        }),
        encoding="utf-8",
    )

    refs = opencode_reader.list_project_refs()
    assert len(refs) == 1
    assert refs[0].session_paths == (session_file,)

    usage = opencode_reader.aggregate_usage(session_file.parent, str(session_file))
    assert usage.user_msgs == 1
    assert opencode_reader.read_user_messages(session_file.parent, str(session_file)) == ["旧版消息"]


def test_opencode_legacy_storage_reads_message_part_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    session_dir = tmp_path / "storage" / "session" / "proj-legacy"
    message_dir = tmp_path / "storage" / "message" / "ses-legacy"
    part_dir = tmp_path / "storage" / "part" / "msg-user"
    session_dir.mkdir(parents=True)
    message_dir.mkdir(parents=True)
    part_dir.mkdir(parents=True)
    session_file = session_dir / "ses-legacy.json"
    session_file.write_text(
        _j({
            "id": "ses-legacy",
            "projectID": "proj-legacy",
            "title": "legacy",
            "tokens": {"input": 10, "output": 2},
        }),
        encoding="utf-8",
    )
    (message_dir / "msg-user.json").write_text(
        _j({"id": "msg-user", "role": "user"}),
        encoding="utf-8",
    )
    (part_dir / "part-text.json").write_text(
        _j({"type": "text", "text": "拆分 part 消息"}),
        encoding="utf-8",
    )

    refs = opencode_reader.list_project_refs()
    assert len(refs) == 1

    usage = opencode_reader.aggregate_usage(session_file.parent, str(session_file))
    assert usage.user_msgs == 1
    assert opencode_reader.read_user_messages(session_file.parent, str(session_file)) == ["拆分 part 消息"]


def test_opencode_discovers_wsl_data_dir(tmp_path, monkeypatch):
    """_wsl_data_dirs() should find OpenCode dirs under simulated WSL trees."""
    # Build a fake WSL tree: <root>/Ubuntu/home/user/.local/share/opencode/
    wsl_root = tmp_path / "wsl_root"
    ubuntu = wsl_root / "Ubuntu" / "home" / "user"
    oc_dir = ubuntu / ".local" / "share" / "opencode"
    oc_dir.mkdir(parents=True)

    monkeypatch.setattr(opencode_reader, "_WSL_PREFIXES", (str(wsl_root),))
    monkeypatch.setattr(opencode_reader.sys, "platform", "win32")

    result = opencode_reader._wsl_data_dirs()
    assert len(result) == 1
    assert result[0] == oc_dir


def test_opencode_wsl_deduces_distros_across_prefixes(tmp_path, monkeypatch):
    """Same distro visible via both \\wsl$ and \\wsl.localhost should appear once."""
    wsl_root = tmp_path / "wsl_root"
    ubuntu = wsl_root / "Ubuntu" / "home" / "user"
    oc_dir = ubuntu / ".local" / "share" / "opencode"
    oc_dir.mkdir(parents=True)

    # Two prefixes pointing to the same tree
    monkeypatch.setattr(
        opencode_reader, "_WSL_PREFIXES", (str(wsl_root), str(wsl_root))
    )
    monkeypatch.setattr(opencode_reader.sys, "platform", "win32")

    result = opencode_reader._wsl_data_dirs()
    assert len(result) == 1


def test_opencode_wsl_no_opencode_dir_returns_empty(tmp_path, monkeypatch):
    """WSL users without OpenCode installed should contribute nothing."""
    wsl_root = tmp_path / "wsl_root"
    (wsl_root / "Ubuntu" / "home" / "user").mkdir(parents=True)

    monkeypatch.setattr(opencode_reader, "_WSL_PREFIXES", (str(wsl_root),))
    monkeypatch.setattr(opencode_reader.sys, "platform", "win32")

    assert opencode_reader._wsl_data_dirs() == []
