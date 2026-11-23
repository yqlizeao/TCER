from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tcer.core import t3_reader as t3r

T = t3r._T3_THREADS
P = t3r._T3_PTURNS
I = t3r._T3_ITEMS
S = t3r._T3_PSESSIONS
A = t3r._T3_SUBAGENTS
J = t3r._T3_PROJECTS


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(p)
    con.executescript(f"""
        create table {J}(project_id text primary key, workspace_root text,
                         title text, deleted_at text, archived_at text);
        create table {T}(thread_id text primary key, project_id text,
                         title text, created_at text, updated_at text,
                         deleted_at text, default_provider text,
                         provider_instance_id text, payload_json text);
        create table {P}(provider_turn_id text primary key, thread_id text,
                         ordinal integer, status text, started_at text,
                         completed_at text, payload_json text);
        create table {I}(item_id text primary key, thread_id text,
                         provider_turn_id text, ordinal integer, type text,
                         status text, payload_json text);
        create table {S}(provider_session_id text, thread_id text,
                         model text, updated_at text, payload_json text);
        create table {A}(thread_id text, child_thread_id text,
                         started_at text);
    """)
    con.close()
    monkeypatch.setattr(t3r, "t3_state_db", lambda: p)
    return p


def _thread(db: Path, tid: str, pid: str = "p1", model: str = "swe-2-high",
            title: str | None = None) -> None:
    con = sqlite3.connect(db)
    con.execute(
        f"insert into {T} values(?,?,?,?,?,?,?,?,?)",
        (tid, pid, title or f"t-{tid}", "2026-01-01T10:00:00Z",
         "2026-01-01T11:00:00Z", None, "acpRegistry_devin",
         "acpRegistry_devin",
         _j({"modelSelection": {"model": model},
             "providerInstanceId": "acpRegistry_devin"})))
    con.commit(); con.close()


def _project(db: Path, pid: str, root: str) -> None:
    con = sqlite3.connect(db)
    con.execute(f"insert into {J} values(?,?,?,null,null)", (pid, root, pid))
    con.commit(); con.close()


def _pturn(db: Path, pid: str, tid: str, ordinal: int, usage: dict | None,
           status: str = "completed") -> None:
    payload = {}
    if usage is not None:
        payload["turnTokenUsage"] = usage
    con = sqlite3.connect(db)
    con.execute(
        f"insert into {P} values(?,?,?,?,?,?,?)",
        (pid, tid, ordinal, status, "2026-01-01T10:00:00Z",
         "2026-01-01T10:00:05Z", _j(payload)))
    con.commit(); con.close()


def _complete(i=1000, o=200, cr=500, cw=100, rsn=30):
    return {"usageStatus": "complete", "inputTokens": i, "outputTokens": o,
            "cachedInputTokens": cr, "cacheCreationTokens": cw,
            "reasoningTokens": rsn}


def _item(db: Path, iid: str, tid: str, itype: str, payload: dict,
          status: str = "completed", pid: str = "pt1", ordinal: int = 0) -> None:
    con = sqlite3.connect(db)
    con.execute(
        f"insert into {I} values(?,?,?,?,?,?,?)",
        (iid, tid, pid, ordinal, itype, status, _j(payload)))
    con.commit(); con.close()


# --------------------------------------------------------------------------- #

def test_import_threads_skipped(db):
    _project(db, "p1", "c:/repo/a")
    _thread(db, "t1", model="gemini-4-neon")
    _thread(db, "import:claudeAgent:abc", model="claude-fable-5-2")
    refs = t3r.list_project_refs()
    ref = next(r for r in refs if r.key.endswith("p1") or "repo" in (r.key or ""))
    tids = t3r.sessions_for_project(ref)
    assert "t1" in tids
    assert not any(t.startswith("import:") for t in tids)


def test_usage_complete_counted(db):
    _thread(db, "t1")
    _pturn(db, "pt1", "t1", 0, _complete())
    _pturn(db, "pt2", "t1", 1, _complete(i=2000, o=100, cr=0, cw=0, rsn=0))
    u = t3r.aggregate_usage(db, "t1")
    # inputTokens 含缓存子集 → 净输入 = input - cached - creation
    assert u.input_tokens == (1000 - 500 - 100) + 2000
    assert u.output_tokens == 300
    assert u.cache_read_input_tokens == 500
    assert u.cache_creation_input_tokens == 100
    assert u.reasoning_output_tokens == 30
    assert u.assistant_msgs == 2


def test_usage_unavailable_is_not_zero(db):
    _thread(db, "t1")
    _pturn(db, "pt1", "t1", 0, {"usageStatus": "unavailable"})
    _pturn(db, "pt2", "t1", 1, _complete())
    u = t3r.aggregate_usage(db, "t1")
    assert u.input_tokens == 400 and u.output_tokens == 200
    assert u.assistant_msgs == 1
    assert u.empty_usage_skipped >= 1     # 不适用，绝不编数


def test_tool_items_and_errors(db):
    _thread(db, "t1")
    _item(db, "i1", "t1", "command_execution",
          {"command": "ls", "exitCode": 0})
    _item(db, "i2", "t1", "command_execution",
          {"command": "false", "exitCode": 1})
    _item(db, "i3", "t1", "dynamic_tool", {"toolName": "read"})
    _item(db, "i4", "t1", "file_change",
          {"fileName": "a.py", "diffStr": "@@\n+x\n"}, status="completed")
    _item(db, "i5", "t1", "checkpoint", {})
    u = t3r.aggregate_usage(db, "t1")
    assert u.tool_calls["Bash"] == 2
    assert u.tool_calls["Read"] == 1
    assert u.tool_calls["Edit"] == 1
    assert "checkpoint" not in str(u.tool_calls)
    assert u.tool_errors == 1


def test_file_change_loc_replay(db):
    _thread(db, "t1")
    diff = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n keep\n-gone\n+new1\n+new2\n"
    _item(db, "i1", "t1", "file_change",
          {"fileName": "a.py", "diffStr": diff}, status="completed")
    _item(db, "i2", "t1", "file_change",
          {"fileName": "b.py", "diffStr": "@@\n+lost\n"}, status="failed")
    sloc = t3r.session_loc_full(db, "t1")
    assert sloc.added == 1 and sloc.deleted == 0    # +2/-1 → 净 +1（Edit 净行差口径）
    assert sloc.file_edit_counts == {"a.py": 1}     # failed b.py 不回放


def test_user_messages_and_subagent_fold(db):
    _thread(db, "t1")
    _item(db, "i1", "t1", "user_message", {"text": "first ask"})
    _item(db, "i2", "t1", "user_message", {"text": "/slash"})
    _item(db, "i3", "t1", "user_message", {"text": ""})
    _thread(db, "sub1")
    _item(db, "s1", "sub1", "user_message", {"text": "child"})
    _pturn(db, "psub", "sub1", 0, _complete(o=10))
    con = sqlite3.connect(db)
    con.execute(
        f"insert into {A} values('t1','sub1','2026-01-01T10:00:00Z')")
    con.commit(); con.close()
    u = t3r.aggregate_usage(db, "t1")
    assert u.user_msgs == 3                     # 父 2 + 折叠子 1
    assert u.slash_command_count == 1
    assert u.output_tokens == 10 and u.input_tokens == 400   # 子代理用量并入父
    assert t3r.subagents_of(db, "t1") == 1


def test_project_grouping(db):
    _project(db, "p1", "c:/repo/a")
    _project(db, "p2", "c:/repo/b")
    _thread(db, "t1", pid="p1")
    _thread(db, "t2", pid="p2")
    refs = t3r.list_project_refs()
    assert len(refs) == 2
    got = {t for r in refs for t in t3r.sessions_for_project(r)}
    assert got == {"t1", "t2"}
