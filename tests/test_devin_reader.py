from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tcer.core import devin_reader as dr


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """Synthetic Devin root: cli/sessions.db + User/{acp-messages,globalStorage}."""
    r = tmp_path / "devin"
    (r / "cli").mkdir(parents=True)
    (r / "User" / "acp-messages").mkdir(parents=True)
    (r / "User" / "globalStorage").mkdir(parents=True)
    monkeypatch.setattr(dr, "_devin_roots", lambda: [r])
    return r


def _sessions_db(root: Path) -> Path:
    db = root / "cli" / "sessions.db"
    con = sqlite3.connect(db)
    con.executescript("""
        create table sessions(
            id text primary key, working_directory text, backend_type text,
            model text, agent_mode text, created_at real, last_activity_at real,
            title text, hidden integer default 0, metadata text);
        create table message_nodes(
            node_id integer primary key, session_id text,
            chat_message text, created_at real);
        create table tool_call_state(
            session_id text, tool_call_id text,
            tool_call_json text, tool_call_update_json text,
            source_node_ids text);
        create table subagent_heads(session_id text, node_id integer);
        create table prompt_history(id integer, text text);
    """)
    con.close()
    return db


def _session(db: Path, sid: str, cwd: str, model: str = "swe-2-high") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into sessions(id, working_directory, model, created_at, "
        "last_activity_at, title, hidden) values(?,?,?,?,?,?,0)",
        (sid, cwd, model, 1730000000.0, 1730001000.0, f"title-{sid}"))
    con.commit()
    con.close()


def _node(db: Path, sid: str, nid: int, msg: dict, ts: float = 1730000001.0) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into message_nodes(node_id, session_id, chat_message, created_at) "
        "values(?,?,?,?)", (nid, sid, _j(msg), ts))
    con.commit()
    con.close()


def _user(text: str, source: str = "user") -> dict:
    return {"role": "user", "content": text,
            "metadata": {"is_user_input": True,
                         "telemetry": {"source": source}}}


def _assistant(rid: str, *, i=100, o=50, cr=1000, cw=0, model="swe-2-high",
               ttft=400, dur=2000) -> dict:
    return {"role": "assistant",
            "metadata": {"request_id": rid, "generation_model": model,
                         "metrics": {"input_tokens": i, "output_tokens": o,
                                     "cache_read_tokens": cr,
                                     "cache_creation_tokens": cw,
                                     "context_input_tokens": i + cr + cw,
                                     "ttft_ms": ttft,
                                     "total_time_ms": dur}}}


def _tool(db: Path, sid: str, cid: str, name: str, status: str = "completed",
          upd_extra: dict | None = None, call_extra: dict | None = None) -> None:
    call = {"title": name, "rawInput": {}, **(call_extra or {})}
    upd = {"status": status,
           "_meta": {"cognition.ai/inferenceToolName": name},
           **(upd_extra or {})}
    con = sqlite3.connect(db)
    con.execute(
        "insert into tool_call_state(session_id, tool_call_id, tool_call_json, "
        "tool_call_update_json) values(?,?,?,?)",
        (sid, cid, _j(call), _j(upd)))
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #

def test_request_id_dedup(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _node(db, "s1", 1, _assistant("r1"))
    _node(db, "s1", 2, _assistant("r1"))   # duplicate snapshot, same request
    _node(db, "s1", 3, _assistant("r2", o=60))
    u = dr.aggregate_usage(dr.DevinSession("db", db, "s1"))
    assert u.input_tokens == 200
    assert u.output_tokens == 110
    assert u.cache_read_input_tokens == 2000
    assert u.assistant_msgs == 2
    assert u.peak_input_tokens == 1100
    assert len(u.turn_stats) == 2
    assert u.ttft_ms_samples == [400, 400]
    assert "swe-2" in u.models          # normalize → 价表键


def test_streaming_snapshot_merges_max(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _node(db, "s1", 1, _assistant("r1", o=10))
    _node(db, "s1", 2, _assistant("r1", o=50))   # later snapshot grows
    u = dr.aggregate_usage(dr.DevinSession("db", db, "s1"))
    assert u.output_tokens == 50 and u.assistant_msgs == 1


def test_user_msgs_only_real_user_input(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _node(db, "s1", 1, _user("fix the bug"))
    _node(db, "s1", 2, _user("/compact"))
    _node(db, "s1", 3, _user("subagent task", source="subagent_task"))
    _node(db, "s1", 4, {"role": "user", "content": "synthetic",
                        "metadata": {"is_user_input": False}})
    u = dr.aggregate_usage(dr.DevinSession("db", db, "s1"))
    assert u.user_msgs == 2
    assert u.slash_command_count == 1
    assert u.user_message_texts == ["fix the bug", "/compact"]


def test_tool_calls_canonical_and_errors(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _tool(db, "s1", "t1", "exec")
    _tool(db, "s1", "t2", "edit", status="failed")
    _tool(db, "s1", "t3", "web_search")
    u = dr.aggregate_usage(dr.DevinSession("db", db, "s1"))
    assert u.tool_calls == {"Bash": 1, "Edit": 1, "WebSearch": 1}
    assert u.tool_errors == 1
    assert u.tool_errors_by_tool == {"Edit": 1}
    assert u.web_search_count == 1


def test_loc_replay_completed_diff_only(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _tool(db, "s1", "t1", "edit", upd_extra={
        "content": [{"type": "diff", "path": "a.py",
                     "oldText": "x\n", "newText": "x\ny\nz\n"}]})
    _tool(db, "s1", "t2", "edit", status="failed", upd_extra={
        "content": [{"type": "diff", "path": "b.py",
                     "oldText": "p\n", "newText": "p\nq\n"}]})
    meta = dr.read_session_meta(db, "s1")
    sloc = dr._loc_scan(dr.DevinSession("db", db, "s1"), meta)[0]
    assert sloc.added - sloc.deleted == 2     # failed edit not replayed


def test_project_and_session_enumeration(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _session(db, "s2", "c:/repo/b")
    _session(db, "hidden", "c:/repo/a")
    con = sqlite3.connect(db)
    con.execute("update sessions set hidden=1 where id='hidden'")
    con.commit(); con.close()
    refs = dr.list_project_refs()
    keys = {r.key for r in refs}
    assert f"devin::{dr._norm_cwd('c:/repo/a')}" in keys
    assert f"devin::{dr._norm_cwd('c:/repo/b')}" in keys
    sids = [h.key for h in dr.sessions_for_project(f"devin::{dr._norm_cwd('c:/repo/a')}")]
    assert sids == ["s1"]


# --------------------------------------------------------------------------- #
# acp transcripts (local residue + cloud)
# --------------------------------------------------------------------------- #

def _acp_db(root: Path, uuid_hash: str, msgs: list[tuple[str, dict]],
            info: dict | None = None) -> Path:
    db = root / "User" / "acp-messages" / f"{uuid_hash}.db"
    con = sqlite3.connect(db)
    con.executescript(
        "create table messages(kind text, payload text, position integer);"
        "create table meta(key text, value text);")
    for pos, (kind, payload) in enumerate(msgs):
        con.execute("insert into messages values(?,?,?)", (kind, _j(payload), pos))
    con.execute("insert into meta values('message_count', ?)", (str(len(msgs)),))
    if info is not None:
        con.execute("insert into meta values('info', ?)", (_j(info),))
    con.commit(); con.close()
    return db


def _ms_index(root: Path, entries: dict[str, str]) -> None:
    db = root / "User" / "globalStorage" / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("create table ItemTable(key text primary key, value text)")
    for k, uuid_hash in entries.items():
        full = "windsurf.acp.messageStore.session.acp/" + k
        con.execute("insert into ItemTable values(?,?)",
                    (full, _j({"uuid": uuid_hash, "lastUpdated": 1})))
    con.commit(); con.close()


def _acp_user(text: str, ts: int = 1730000001000) -> tuple[str, dict]:
    return ("user_message", {"sourceEventTimestampMs": ts,
                             "content": [{"content": {"text": text}}]})


def _acp_call(cid: str, name: str, status: str = "completed",
              extra_meta: dict | None = None,
              content: list | None = None) -> tuple[str, dict]:
    return ("tool_call", {
        "sourceEventTimestampMs": 1730000002000,
        "content": {"toolCallId": cid, "status": status,
                    "_meta": {"cognition.ai/inferenceToolName": name,
                              **(extra_meta or {})},
                    **({"content": content} if content else {})}})


def test_acp_cloud_session_enumerated_and_flagged(root):
    _acp_db(root, "abc123", [
        _acp_user("deploy the fix"),
        _acp_call("c1", "exec", extra_meta={"cognition.ai/repoName": "org/repo"}),
    ], info={"configOptions": [{"id": "repo_id", "currentValue": "org/repo"}]})
    _ms_index(root, {"devin-cloud/devin-session-cache/1/user-u/account-a/devin-cloud/devin-ff": "abc123"})
    refs = dr.list_project_refs()
    assert any(r.key == "devin::cloud::org/repo" for r in refs)
    hs = dr.sessions_for_project("devin::cloud::org/repo")
    assert len(hs) == 1 and hs[0].cloud and hs[0].kind == "acp"
    meta = dr.read_acp_meta(hs[0])
    assert meta.thread_source == "cloud"          # 云端用卡片云图标区分
    assert "云端" not in meta.title
    u = dr.aggregate_usage(hs[0])
    assert u.input_tokens == 0                    # no metering — never fabricate
    assert u.user_msgs == 1
    assert u.tool_calls == {"Bash": 1}


def test_acp_local_transcript_groups_by_cwd(root):
    _acp_db(root, "def456", [
        _acp_user("hi"),
        _acp_call("c1", "read", extra_meta={"cognition.ai/cwd": "c:/repo/z"}),
    ])
    _ms_index(root, {"devin-cli/residual-name": "def456"})
    refs = dr.list_project_refs()
    key = f"devin::{dr._norm_cwd('c:/repo/z')}"
    assert any(r.key == key for r in refs)
    assert len(dr.sessions_for_project(key)) == 1


def test_acp_file_updates_loc(root):
    _acp_db(root, "loc1", [
        _acp_call("c1", "edit",
                  extra_meta={"cognition.ai/cwd": "c:/repo/z",
                              "cognition.ai/fileUpdates": [
                                  {"file_path": "a.py", "lines_added": 10,
                                   "lines_removed": 3, "action_type": "edit"}]}),
    ])
    h = dr.DevinSession("acp", root / "User" / "acp-messages" / "loc1.db",
                        "loc1", cloud=False, ms_key="devin-cli/x")
    sloc = dr.session_loc_full(h)
    assert sloc.added - sloc.deleted == 7


def test_hidden_sessions_excluded(root):
    db = _sessions_db(root)
    _session(db, "s1", "c:/repo/a")
    _session(db, "ghost", "c:/repo/a")
    con = sqlite3.connect(db)
    con.execute("update sessions set hidden=1 where id='ghost'")
    con.commit(); con.close()
    hs = dr.sessions_for_project(f"devin::{dr._norm_cwd('c:/repo/a')}")
    assert [h.key for h in hs] == ["s1"]
