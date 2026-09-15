"""Tests for per-session visibility & permission model (server db layer).

Ownership is keyed on ``uploaded_by`` (login username). A viewer sees their own
session rows unconditionally plus anyone else's rows marked ``public``. Bulk
project-visibility only flips rows the caller owns. New rows default to private,
and a re-upload of an existing session must NOT reset a visibility the owner set.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "server" / "backend"
sys.path.insert(0, str(_BACKEND))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh server db module pointed at an isolated sqlite file."""
    monkeypatch.setenv("TCER_SERVER_DB", str(tmp_path / "vis_test.db"))
    mod = importlib.import_module("db")
    mod = importlib.reload(mod)  # re-read _DB_PATH from the patched env
    mod.init_db()
    return mod


def _session_row(sid: str, *, title=None, net_loc=100, tokens=1_000_000,
                 cost=1.0) -> dict:
    return {
        "session_id": sid,
        "title": title or sid,
        "net_loc": net_loc,
        "total_tokens": tokens,
        "code_added": net_loc,
        "cost_usd": cost,
        "tcer": 100.0,
    }


def _upload(db, uploaded_by, person, project, sids, **kw):
    return db.insert_records(
        uploaded_by=uploaded_by, person=person, project=project,
        aggregate=None,
        sessions=[_session_row(s, **kw) for s in sids],
        generated_at=1_700_000_000,
    )


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_new_sessions_default_to_private(db):
    _upload(db, "alice", "alice", "projA", ["s1", "s2"])
    lst = db.sessions_list(viewer="alice")
    assert {s["session_id"] for s in lst["sessions"]} == {"s1", "s2"}
    assert all(s["visibility"] == "private" for s in lst["sessions"])
    assert all(s["is_owner"] for s in lst["sessions"])


# --------------------------------------------------------------------------- #
# List filtering by owner + public
# --------------------------------------------------------------------------- #
def test_viewer_sees_only_own_private_sessions(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    _upload(db, "bob", "bob", "projA", ["b1"])
    seen = {s["session_id"] for s in db.sessions_list(viewer="alice")["sessions"]}
    assert seen == {"a1"}          # bob's private row is invisible to alice


def test_public_sessions_are_visible_to_others(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    _upload(db, "bob", "bob", "projA", ["b1"])
    rows = db.sessions_list(viewer="bob")
    bid = next(s["id"] for s in rows["sessions"] if s["session_id"] == "b1")
    assert db.set_session_visibility(bid, "bob", "public") == "ok"
    # Now alice can see bob's public session, flagged as not-owned.
    seen = {s["session_id"]: s for s in db.sessions_list(viewer="alice")["sessions"]}
    assert set(seen) == {"a1", "b1"}
    assert seen["b1"]["is_owner"] is False
    assert seen["b1"]["visibility"] == "public"


def test_unauthenticated_viewer_sees_only_public(db):
    _upload(db, "alice", "alice", "projA", ["a1", "a2"])
    rows = db.sessions_list(viewer=None)
    assert rows["sessions"] == []
    a1 = _row_id(db, "a1")
    db.set_session_visibility(a1, "alice", "public")
    seen = {s["session_id"] for s in db.sessions_list(viewer=None)["sessions"]}
    assert seen == {"a1"}


# --------------------------------------------------------------------------- #
# Detail permission
# --------------------------------------------------------------------------- #
def test_detail_forbidden_for_others_private(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    a1 = _row_id(db, "a1")
    assert db.session_detail(a1, viewer="bob") == "forbidden"
    assert db.session_detail(a1, viewer="alice")["session_id"] == "a1"


def test_detail_missing_row_is_none(db):
    assert db.session_detail(99999, viewer="alice") is None


def test_detail_public_readable_by_others(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    a1 = _row_id(db, "a1")
    db.set_session_visibility(a1, "alice", "public")
    d = db.session_detail(a1, viewer="bob")
    assert d["is_owner"] is False and d["visibility"] == "public"


# --------------------------------------------------------------------------- #
# Setting visibility — ownership enforced
# --------------------------------------------------------------------------- #
def test_only_owner_can_set_visibility(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    a1 = _row_id(db, "a1")
    assert db.set_session_visibility(a1, "bob", "public") == "forbidden"
    assert db.session_detail(a1, viewer="bob") == "forbidden"  # unchanged


def test_set_visibility_rejects_bad_value(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    a1 = _row_id(db, "a1")
    with pytest.raises(ValueError):
        db.set_session_visibility(a1, "alice", "semi")


# --------------------------------------------------------------------------- #
# Re-upload must not clobber a visibility the owner set
# --------------------------------------------------------------------------- #
def test_reupload_preserves_visibility(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    a1 = _row_id(db, "a1")
    db.set_session_visibility(a1, "alice", "public")
    # Same (person, project, session_id) → updates in place; must stay public.
    _upload(db, "alice", "alice", "projA", ["a1"], net_loc=200)
    d = db.session_detail(_row_id(db, "a1"), viewer="bob")
    assert d != "forbidden" and d["visibility"] == "public"


# --------------------------------------------------------------------------- #
# Bulk project visibility — only touches caller's own rows
# --------------------------------------------------------------------------- #
def test_bulk_sets_only_callers_rows(db):
    _upload(db, "alice", "alice", "projA", ["a1", "a2"])
    _upload(db, "bob", "bob", "projA", ["b1"])
    n = db.set_project_visibility("alice", "projA", "public")
    assert n == 2                       # bob's b1 untouched
    # alice's rows now public, bob's still private/forbidden to alice.
    seen = {s["session_id"]: s for s in db.sessions_list(viewer="carol")["sessions"]}
    assert set(seen) == {"a1", "a2"}    # carol sees alice's public, not bob's
    assert db.session_detail(_row_id(db, "b1"), viewer="carol") == "forbidden"


def test_group_summary_counts_owned_split(db):
    _upload(db, "alice", "alice", "projA", ["a1", "a2", "a3"])
    db.set_session_visibility(_row_id(db, "a1"), "alice", "public")
    g = db.project_group_summary("projA", viewer="alice")
    assert g["owned_count"] == 3
    assert g["owned_public"] == 1
    assert g["owned_private"] == 2
    assert g["sessions"] == 3           # all visible to owner


def test_group_summary_hides_others_private(db):
    _upload(db, "alice", "alice", "projA", ["a1"])
    _upload(db, "bob", "bob", "projA", ["b1"])
    g = db.project_group_summary("projA", viewer="alice")
    assert g["sessions"] == 1           # only alice's own visible
    assert g["owned_count"] == 1


def _row_id(db, sid: str) -> int:
    conn = db.connect()
    try:
        r = conn.execute("SELECT id FROM uploads WHERE session_id=?", (sid,)).fetchone()
        return r["id"]
    finally:
        conn.close()


def test_millisecond_timestamp_normalization_and_migration(db):
    # 模拟客户端上报毫秒时间戳 generated_at = 1_700_000_000_000 (13位)
    db.insert_records(
        uploaded_by="alice",
        person="alice",
        project="projA",
        aggregate=None,
        sessions=[_session_row("s_ms", title="MS Session")],
        generated_at=1_700_000_000_000,
    )
    res = db.sessions_list(viewer="alice")
    sess = next(s for s in res["sessions"] if s["session_id"] == "s_ms")
    assert sess["ts"] == 1_700_000_000

    # 模拟老版本库升级：已有毫秒时间戳且从未跑过迁移（user_version=0）
    conn = db.connect()
    try:
        conn.execute("UPDATE uploads SET ts=1710000000000 WHERE session_id='s_ms'")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()

    # 触发 init_db() 自愈迁移（迁移一次后打标，不再重复执行）
    db.init_db()

    conn = db.connect()
    try:
        ts = conn.execute("SELECT ts FROM uploads WHERE session_id='s_ms'").fetchone()["ts"]
        assert ts == 1_710_000_000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()

    # 已打标：重复 init_db 不再触碰数据（否则秒级 ts 会被再除 1000 毁掉）
    db.init_db()
    conn = db.connect()
    try:
        ts = conn.execute("SELECT ts FROM uploads WHERE session_id='s_ms'").fetchone()["ts"]
        assert ts == 1_710_000_000
    finally:
        conn.close()


def test_aggregate_cleaned_up_when_sessions_uploaded(db):
    """当某项目先上传了仅聚合记录，随后又上传了带明细会话时：
    1. 旧的 aggregate 记录必须被清理/抑制，不能在会话列表中以「仅聚合」假会话残留；
    2. _fetch_rows 不能对该项目既取 aggregate 又取 sessions 造成双重计数。
    """
    agg_row = {
        "title": "projA",
        "net_loc": 500,
        "total_tokens": 5_000_000,
        "code_added": 500,
        "cost_usd": 5.0,
        "tcer": 100.0,
    }
    # 第一次：仅聚合上传
    db.insert_records(
        uploaded_by="alice",
        person="alice",
        project="projA",
        aggregate=agg_row,
        sessions=None,
        generated_at=1_700_000_000,
    )
    lst1 = db.sessions_list(viewer="alice")
    assert len(lst1["sessions"]) == 1
    assert lst1["sessions"][0]["aggregate_only"] is True

    # 第二次：带会话明细上传（不同批次）
    db.insert_records(
        uploaded_by="alice",
        person="alice",
        project="projA",
        aggregate=agg_row,
        sessions=[_session_row("s1"), _session_row("s2")],
        generated_at=1_700_001_000,
    )
    lst2 = db.sessions_list(viewer="alice")
    # 会话列表中只能看到真实会话 s1, s2，旧的「仅聚合」必须消失
    sids = [s["session_id"] for s in lst2["sessions"]]
    assert set(sids) == {"s1", "s2"}
    assert all(not s["aggregate_only"] for s in lst2["sessions"])

    # 数据库中已没有该项目的 kind='aggregate' 冗余行
    conn = db.connect()
    try:
        aggs = conn.execute("SELECT id FROM uploads WHERE kind='aggregate' AND project='projA'").fetchall()
        assert len(aggs) == 0
    finally:
        conn.close()


def test_historical_aggregate_cleanup_migration(db):
    """模拟在修复前已经处于 user_version=1 的老数据库：
    存在因为历史跨批次上传遗留的 orphan aggregate 记录。
    执行 init_db() 应该自动迁移到 user_version=2 并清除该冗余记录。
    """
    conn = db.connect()
    try:
        # 手动构造 user_version=1 的库状态，并插入一对冲突的 aggregate 与 session
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO uploads(batch_id, uploaded_at, uploaded_by, person, project, kind, ts, raw_json) "
            "VALUES('b_old', 1700000000, 'alice', 'alice', 'projOld', 'aggregate', 1700000000, '{}')"
        )
        conn.execute(
            "INSERT INTO uploads(batch_id, uploaded_at, uploaded_by, person, project, kind, session_id, ts, raw_json) "
            "VALUES('b_new', 1700001000, 'alice', 'alice', 'projOld', 'session', 'sess_old', 1700001000, '{}')"
        )
        # 另一个项目只有 aggregate（合法保留）
        conn.execute(
            "INSERT INTO uploads(batch_id, uploaded_at, uploaded_by, person, project, kind, ts, raw_json) "
            "VALUES('b_only_agg', 1700000000, 'bob', 'bob', 'projOnlyAgg', 'aggregate', 1700000000, '{}')"
        )
        conn.commit()
    finally:
        conn.close()

    # 触发 init_db()
    db.init_db()

    conn = db.connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # projOld 的 aggregate 应该已被删除
        rem_old = conn.execute("SELECT id FROM uploads WHERE project='projOld' AND kind='aggregate'").fetchall()
        assert len(rem_old) == 0
        # projOld 的 session 完好保留
        rem_sess = conn.execute("SELECT id FROM uploads WHERE project='projOld' AND kind='session'").fetchall()
        assert len(rem_sess) == 1
        # projOnlyAgg 没有任何 session，其 aggregate 必须保留
        rem_bob = conn.execute("SELECT id FROM uploads WHERE project='projOnlyAgg' AND kind='aggregate'").fetchall()
        assert len(rem_bob) == 1
    finally:
        conn.close()
