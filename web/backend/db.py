"""SQLite storage for uploaded TCER reports.

Pure stdlib. One file DB (``tcer_web.db`` next to this module by default, or
``TCER_WEB_DB`` env override). Two tables:

- ``users``    : login credentials (salted PBKDF2 hash).
- ``uploads``  : one row per uploaded session/aggregate record, flattened for
                 querying by person / project / model / time.

The ``uploads`` schema mirrors ``tcer.core.export.report_row_dict`` field names
so the client can send those dicts verbatim; only the columns we filter/plot on
are promoted to real columns, the full row is kept in ``raw_json``.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

_DB_PATH = Path(os.environ.get("TCER_WEB_DB") or (Path(__file__).parent / "tcer_web.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    salt          TEXT NOT NULL,
    pwd_hash      TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at   INTEGER NOT NULL,   -- epoch s, server receive time
    uploaded_by   TEXT NOT NULL,      -- login username (audit)
    person        TEXT,               -- reported user (may be "anonymous")
    project       TEXT,               -- project key
    kind          TEXT NOT NULL,      -- "aggregate" | "session"
    session_id    TEXT,
    model         TEXT,               -- primary model label (for per-model plots)
    ts            INTEGER,            -- record time (epoch s): session start or generated_at
    tcer          REAL,
    ctei          REAL,
    cost_usd      REAL,
    net_loc       INTEGER,
    total_tokens  INTEGER,
    churn_ratio   REAL,
    chr           REAL,               -- cache hit ratio
    read_before_write REAL,
    search_edit_ratio REAL,
    tool_error_rate   REAL,
    raw_json      TEXT NOT NULL       -- full report_row_dict
);

CREATE INDEX IF NOT EXISTS idx_uploads_person  ON uploads(person);
CREATE INDEX IF NOT EXISTS idx_uploads_project ON uploads(project);
CREATE INDEX IF NOT EXISTS idx_uploads_model   ON uploads(model);
CREATE INDEX IF NOT EXISTS idx_uploads_ts      ON uploads(ts);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Users / auth
# --------------------------------------------------------------------------- #
def _hash_pwd(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return dk.hex()


def create_user(username: str, password: str) -> bool:
    """Create a user. Returns False if the username already exists."""
    salt = secrets.token_hex(16)
    pwd_hash = _hash_pwd(password, salt)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users(username, salt, pwd_hash, created_at) VALUES(?,?,?,?)",
            (username, salt, pwd_hash, int(time.time())),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def verify_user(username: str, password: str) -> bool:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT salt, pwd_hash FROM users WHERE username=?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    expected = row["pwd_hash"]
    actual = _hash_pwd(password, row["salt"])
    return secrets.compare_digest(expected, actual)


def user_count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #
def _primary_model(row: dict) -> str | None:
    label = row.get("models_label")
    if label:
        return label
    models = row.get("models") or []
    return models[0] if models else None


def insert_records(
    uploaded_by: str,
    person: str | None,
    project: str | None,
    aggregate: dict | None,
    sessions: list[dict] | None,
    generated_at: int | None,
) -> int:
    """Flatten and insert an upload payload. Returns number of rows inserted."""
    now = int(time.time())
    rows: list[tuple] = []

    def _mk(row: dict, kind: str) -> tuple:
        ts = row.get("started_at")
        if ts:
            ts = int(ts) // 1000 if ts > 10_000_000_000 else int(ts)
        else:
            ts = generated_at or now
        return (
            now, uploaded_by, person, project, kind,
            row.get("session_id"),
            _primary_model(row),
            ts,
            row.get("tcer"), row.get("ctei"), row.get("cost_usd"),
            row.get("net_loc"), row.get("total_tokens"), row.get("churn_ratio"),
            row.get("chr"), row.get("read_before_write"),
            row.get("search_edit_ratio"), row.get("tool_error_rate"),
            json.dumps(row, ensure_ascii=False, default=str),
        )

    if aggregate:
        rows.append(_mk(aggregate, "aggregate"))
    for s in sessions or []:
        rows.append(_mk(s, "session"))

    if not rows:
        return 0

    conn = connect()
    try:
        conn.executemany(
            """INSERT INTO uploads(
                uploaded_at, uploaded_by, person, project, kind, session_id,
                model, ts, tcer, ctei, cost_usd, net_loc, total_tokens,
                churn_ratio, chr, read_before_write, search_edit_ratio,
                tool_error_rate, raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def distinct_values() -> dict:
    """Return distinct persons / projects / models for filter dropdowns."""
    conn = connect()
    try:
        def col(name: str) -> list[str]:
            q = f"SELECT DISTINCT {name} AS v FROM uploads WHERE {name} IS NOT NULL ORDER BY v"
            return [r["v"] for r in conn.execute(q).fetchall()]
        return {
            "persons": col("person"),
            "projects": col("project"),
            "models": col("model"),
        }
    finally:
        conn.close()


def query_series(
    dimension: str,
    persons: list[str] | None,
    projects: list[str] | None,
    models: list[str] | None,
    start_ts: int | None,
    end_ts: int | None,
    metric: str = "tcer",
) -> dict:
    """Return time-series grouped by ``dimension`` (person|project|model).

    Output: ``{"series": {group_value: [[ts, metric_value], ...], ...}}``
    Only rows with a non-null metric are included. Sessions preferred; falls
    back to aggregate rows when no session rows match.
    """
    if dimension not in ("person", "project", "model"):
        raise ValueError("dimension must be person|project|model")
    if metric not in (
        "tcer", "ctei", "cost_usd", "net_loc", "total_tokens", "churn_ratio",
        "chr", "read_before_write", "search_edit_ratio", "tool_error_rate",
    ):
        raise ValueError("unsupported metric")

    where = [f"{metric} IS NOT NULL", f"{dimension} IS NOT NULL"]
    params: list = []

    def _in(col: str, vals: list[str] | None) -> None:
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params.extend(vals)

    _in("person", persons)
    _in("project", projects)
    _in("model", models)
    if start_ts is not None:
        where.append("ts >= ?"); params.append(start_ts)
    if end_ts is not None:
        where.append("ts <= ?"); params.append(end_ts)

    conn = connect()
    try:
        base = " AND ".join(where)
        # Prefer per-session points; use aggregate only if no sessions exist.
        q = (
            f"SELECT {dimension} AS g, ts, {metric} AS v FROM uploads "
            f"WHERE {base} AND kind='session' ORDER BY ts"
        )
        recs = conn.execute(q, params).fetchall()
        if not recs:
            q = (
                f"SELECT {dimension} AS g, ts, {metric} AS v FROM uploads "
                f"WHERE {base} AND kind='aggregate' ORDER BY ts"
            )
            recs = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    series: dict[str, list] = {}
    for r in recs:
        series.setdefault(r["g"], []).append([r["ts"], r["v"]])
    return {"series": series, "dimension": dimension, "metric": metric}