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