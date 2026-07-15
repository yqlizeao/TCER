"""SQLite storage for uploaded TCER reports.

Pure stdlib for storage; reuses ``tcer.core.pricing`` for model-id
normalization so the web layer agrees with the desktop app on which raw model
strings are "the same model" (e.g. ``claude-opus-4-8`` ≡ ``claude-opus-4.8``).

One file DB (``tcer_web.db`` next to this module by default, or ``TCER_WEB_DB``
env override). Tables:

- ``users``            : login credentials (salted PBKDF2 hash).
- ``uploads``          : one row per uploaded session/aggregate record, flattened
                         for querying by person / project / model / time. Rows
                         from a single upload share a ``batch_id`` so a detailed
                         upload's aggregate row can be de-duplicated against its
                         own session rows.
- ``project_aliases``  : manual ``raw -> canonical`` project-name merges.
- ``model_aliases``    : manual ``raw -> canonical`` model merges (on top of the
                         automatic pricing-table normalization).

The ``uploads`` schema mirrors ``tcer.core.export.report_row_dict`` field names
so the client can send those dicts verbatim; only the columns we filter / plot /
sum on are promoted to real columns, the full row is kept in ``raw_json``.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

# Reuse the desktop app's model-id resolver for canonical model grouping.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
try:
    from tcer.core import pricing  # noqa: E402
except Exception:  # pragma: no cover - web can still run without the package
    pricing = None  # type: ignore

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
    batch_id      TEXT NOT NULL,      -- shared by all rows of one upload call
    uploaded_at   INTEGER NOT NULL,   -- epoch s, server receive time
    uploaded_by   TEXT NOT NULL,      -- login username (audit)
    person        TEXT,               -- reported user (may be "anonymous")
    project       TEXT,               -- project key (raw, pre-alias)
    kind          TEXT NOT NULL,      -- "aggregate" | "session"
    session_id    TEXT,
    title         TEXT,
    model         TEXT,               -- primary model label (raw, pre-normalize)
    ts            INTEGER,            -- record time (epoch s): session start or generated_at
    tcer          REAL,
    ctei          REAL,
    cost_usd      REAL,
    net_loc       INTEGER,
    total_tokens  INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_read_tokens  INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_uploads_batch   ON uploads(batch_id);

CREATE TABLE IF NOT EXISTS project_aliases (
    raw        TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_aliases (
    raw        TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_aliases (
    raw        TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL
);
"""

# Columns added after the first schema shipped; applied idempotently on init.
_MIGRATIONS = {
    "uploads": {
        "batch_id": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT",
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "cache_write_tokens": "INTEGER",
        "cache_read_tokens": "INTEGER",
    },
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        # Additive migrations for DBs created by an earlier schema version.
        for table, cols in _MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
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


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def insert_records(
    uploaded_by: str,
    person: str | None,
    project: str | None,
    aggregate: dict | None,
    sessions: list[dict] | None,
    generated_at: int | None,
) -> int:
    """Flatten and insert an upload payload. Returns number of rows inserted.

    All rows produced by one call share a ``batch_id`` so summing queries can
    drop a detailed upload's aggregate row (its sessions already cover it)
    while keeping aggregate rows from aggregate-only uploads.
    """
    now = int(time.time())
    batch_id = secrets.token_hex(8)
    rows: list[tuple] = []

    def _mk(row: dict, kind: str) -> tuple:
        ts = row.get("started_at")
        if ts:
            ts = int(ts) // 1000 if ts > 10_000_000_000 else int(ts)
        else:
            ts = generated_at or now
        return (
            batch_id, now, uploaded_by, person, project, kind,
            row.get("session_id"), row.get("title"),
            _primary_model(row),
            ts,
            row.get("tcer"), row.get("ctei"), row.get("cost_usd"),
            _int(row.get("net_loc")), _int(row.get("total_tokens")),
            _int(row.get("input_tokens")), _int(row.get("output_tokens")),
            _int(row.get("cache_write_tokens")), _int(row.get("cache_read_tokens")),
            row.get("churn_ratio"),
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
                batch_id, uploaded_at, uploaded_by, person, project, kind,
                session_id, title, model, ts, tcer, ctei, cost_usd, net_loc,
                total_tokens, input_tokens, output_tokens, cache_write_tokens,
                cache_read_tokens, churn_ratio, chr, read_before_write,
                search_edit_ratio, tool_error_rate, raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Alias / canonicalization
# --------------------------------------------------------------------------- #
def _alias_table(kind: str) -> str:
    if kind == "project":
        return "project_aliases"
    if kind == "model":
        return "model_aliases"
    if kind == "person":
        return "person_aliases"
    raise ValueError("kind must be project|model|person")


def get_aliases(kind: str) -> dict[str, str]:
    """Return the manual ``raw -> canonical`` alias map for 'project'|'model'."""
    table = _alias_table(kind)
    conn = connect()
    try:
        return {r["raw"]: r["canonical"]
                for r in conn.execute(f"SELECT raw, canonical FROM {table}")}
    finally:
        conn.close()


def set_alias(kind: str, raw: str, canonical: str | None) -> None:
    """Upsert (or, if ``canonical`` is falsy/equal to raw, delete) one mapping."""
    table = _alias_table(kind)
    conn = connect()
    try:
        if not canonical or canonical == raw:
            conn.execute(f"DELETE FROM {table} WHERE raw=?", (raw,))
        else:
            conn.execute(
                f"INSERT INTO {table}(raw, canonical) VALUES(?,?) "
                f"ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical",
                (raw, canonical),
            )
        conn.commit()
    finally:
        conn.close()


def canonical_project(raw: str | None, amap: dict[str, str]) -> str:
    return amap.get(raw or "", raw or "未标注")


def canonical_person(raw: str | None, amap: dict[str, str]) -> str:
    return amap.get(raw or "", raw or "未标注")


def canonical_model(raw: str | None, amap: dict[str, str]) -> str:
    if not raw:
        return "未标注"
    if raw in amap:
        return amap[raw]
    if pricing is None:
        return raw
    try:
        # The pricing normalizer folds case + '-'/'_' but keeps '.' literal, so
        # "claude-opus-4.8" and "claude-opus-4-8" wouldn't merge. Try the raw id
        # and a dot<->dash variant, preferring whichever resolves to a real
        # table key (table_key returns None on fallback-to-default).
        for cand in (raw, raw.replace(".", "-"), raw.replace("-", ".")):
            if pricing.table_key(cand) is not None:
                return pricing.normalize(cand)
        return pricing.normalize(raw)
    except Exception:
        return raw


def model_display(canonical: str) -> str:
    """Friendly display label for a canonical model key."""
    if pricing is not None and canonical and canonical != "未标注":
        try:
            return pricing.label(canonical)
        except Exception:
            pass
    return canonical


# --------------------------------------------------------------------------- #
# Querying
# --------------------------------------------------------------------------- #
_METRICS = (
    "tcer", "ctei", "cost_usd", "net_loc", "total_tokens", "churn_ratio",
    "chr", "read_before_write", "search_edit_ratio", "tool_error_rate",
)


def _fetch_rows(
    conn: sqlite3.Connection,
    persons: list[str] | None,
    projects: list[str] | None,
    models: list[str] | None,
    start_ts: int | None,
    end_ts: int | None,
    project_amap: dict[str, str],
    model_amap: dict[str, str],
    person_amap: dict[str, str],
) -> list[dict]:
    """Fetch de-duplicated rows, resolve canonical person/project/model, filter.

    De-dup rule: keep every session row, plus aggregate rows whose batch has no
    session rows (aggregate-only uploads). SQL applies only the time filter;
    canonical person/project/model filtering is done in Python because those
    depend on alias maps + pricing normalization.
    """
    where = [
        "(kind='session' OR (kind='aggregate' AND NOT EXISTS ("
        "SELECT 1 FROM uploads s WHERE s.batch_id=u.batch_id AND s.kind='session')))"
    ]
    params: list = []
    if start_ts is not None:
        where.append("ts >= ?"); params.append(start_ts)
    if end_ts is not None:
        where.append("ts <= ?"); params.append(end_ts)

    q = f"SELECT * FROM uploads u WHERE {' AND '.join(where)} ORDER BY ts"
    person_set = set(persons) if persons else None
    proj_set = set(projects) if projects else None
    model_set = set(models) if models else None

    out: list[dict] = []
    for r in conn.execute(q, params):
        d = dict(r)
        d["c_person"] = canonical_person(d["person"], person_amap)
        d["c_project"] = canonical_project(d["project"], project_amap)
        d["c_model"] = canonical_model(d["model"], model_amap)
        if person_set is not None and d["c_person"] not in person_set:
            continue
        if proj_set is not None and d["c_project"] not in proj_set:
            continue
        if model_set is not None and d["c_model"] not in model_set:
            continue
        out.append(d)
    return out


def _agg_metrics(rows: list[dict]) -> dict:
    """Roll a set of rows into one metrics dict (sums + recomputed ratios)."""
    n = len(rows)
    tok = sum(r["total_tokens"] or 0 for r in rows)
    inp = sum(r["input_tokens"] or 0 for r in rows)
    out = sum(r["output_tokens"] or 0 for r in rows)
    cw = sum(r["cache_write_tokens"] or 0 for r in rows)
    cr = sum(r["cache_read_tokens"] or 0 for r in rows)
    net = sum(r["net_loc"] or 0 for r in rows)
    cost = sum(r["cost_usd"] or 0.0 for r in rows)

    def avg(key: str):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    tcer = round(net / (tok / 1_000_000), 2) if tok else None
    cache_in = inp + cr + cw
    chr_ = round(cr / cache_in, 4) if cache_in else None
    return {
        "sessions": n,
        "total_tokens": tok, "input_tokens": inp, "output_tokens": out,
        "cache_write_tokens": cw, "cache_read_tokens": cr,
        "net_loc": net, "cost_usd": round(cost, 4),
        "tcer": tcer, "chr": chr_ if chr_ is not None else avg("chr"),
        "ctei": avg("ctei"), "churn_ratio": avg("churn_ratio"),
        "read_before_write": avg("read_before_write"),
        "search_edit_ratio": avg("search_edit_ratio"),
        "tool_error_rate": avg("tool_error_rate"),
    }


def _alias_maps() -> tuple[dict, dict, dict]:
    """Load (project, model, person) alias maps in one place."""
    return get_aliases("project"), get_aliases("model"), get_aliases("person")


def distinct_values() -> dict:
    """Distinct canonical persons / projects / models for filter dropdowns."""
    project_amap, model_amap, person_amap = _alias_maps()
    conn = connect()
    try:
        raw_persons = [r["v"] for r in conn.execute(
            "SELECT DISTINCT person AS v FROM uploads WHERE person IS NOT NULL")]
        raw_projects = [r["v"] for r in conn.execute(
            "SELECT DISTINCT project AS v FROM uploads WHERE project IS NOT NULL")]
        raw_models = [r["v"] for r in conn.execute(
            "SELECT DISTINCT model AS v FROM uploads WHERE model IS NOT NULL")]
    finally:
        conn.close()
    persons = sorted({canonical_person(p, person_amap) for p in raw_persons})
    projects = sorted({canonical_project(p, project_amap) for p in raw_projects})
    models = sorted({canonical_model(m, model_amap) for m in raw_models})
    return {"persons": persons, "projects": projects, "models": models}


def overview(
    persons: list[str] | None = None,
    projects: list[str] | None = None,
    models: list[str] | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    metric: str = "tcer",
) -> dict:
    """Big-number totals + three daily curves (by person / project / model)."""
    if metric not in _METRICS:
        raise ValueError("unsupported metric")
    project_amap, model_amap, person_amap = _alias_maps()
    conn = connect()
    try:
        rows = _fetch_rows(conn, persons, projects, models, start_ts, end_ts,
                           project_amap, model_amap, person_amap)
    finally:
        conn.close()

    totals = _agg_metrics(rows)
    series = {
        "person": _daily_series(rows, "c_person", metric),
        "project": _daily_series(rows, "c_project", metric),
        "model": _daily_series(rows, "c_model", metric),
    }
    return {"totals": totals, "series": series, "metric": metric,
            "row_count": len(rows)}


def _daily_series(rows: list[dict], group_key: str, metric: str) -> dict:
    """``{group: [[day_ts, value], ...]}`` with per-day aggregation."""
    day = 86400
    buckets: dict[str, dict[int, list[dict]]] = {}
    for r in rows:
        g = r.get(group_key) or "未标注"
        d = (int(r["ts"]) // day) * day
        buckets.setdefault(g, {}).setdefault(d, []).append(r)

    series: dict[str, list] = {}
    for g, days in buckets.items():
        pts = []
        for d in sorted(days):
            m = _agg_metrics(days[d])
            v = m.get(metric)
            if v is not None:
                pts.append([d, v])
        if pts:
            series[g] = pts
    return series


def aggregate_by(dimension: str,
                 persons: list[str] | None = None,
                 projects: list[str] | None = None,
                 models: list[str] | None = None,
                 start_ts: int | None = None,
                 end_ts: int | None = None) -> dict:
    """Aggregation table grouped by 'project'|'person'|'model'.

    Each row carries rolled-up metrics plus the list of raw names merged into
    the canonical group (so the UI can show / adjust the alias mapping).
    """
    if dimension not in ("project", "person", "model"):
        raise ValueError("dimension must be project|person|model")
    project_amap, model_amap, person_amap = _alias_maps()
    conn = connect()
    try:
        rows = _fetch_rows(conn, persons, projects, models, start_ts, end_ts,
                           project_amap, model_amap, person_amap)
    finally:
        conn.close()

    key = {"project": "c_project", "person": "c_person", "model": "c_model"}[dimension]
    raw_key = {"project": "project", "person": "person", "model": "model"}[dimension]
    groups: dict[str, list[dict]] = {}
    raws: dict[str, set] = {}
    for r in rows:
        g = r.get(key) or "未标注"
        groups.setdefault(g, []).append(r)
        raws.setdefault(g, set()).add(r.get(raw_key) or "")

    out = []
    for g, grp in groups.items():
        m = _agg_metrics(grp)
        m["group"] = g
        m["display"] = model_display(g) if dimension == "model" else g
        m["raw_names"] = sorted(x for x in raws[g] if x)
        out.append(m)
    out.sort(key=lambda x: (x["total_tokens"] or 0), reverse=True)
    return {"dimension": dimension, "rows": out}


def sessions_list(persons: list[str] | None = None,
                  projects: list[str] | None = None,
                  models: list[str] | None = None,
                  start_ts: int | None = None,
                  end_ts: int | None = None,
                  limit: int = 1000) -> dict:
    """Filtered session list for the secondary sidebar.

    ``aggregate``-kind rows are flagged ``aggregate_only`` so the UI can mark
    them and show a placeholder instead of a session detail view.
    """
    project_amap, model_amap, person_amap = _alias_maps()
    conn = connect()
    try:
        rows = _fetch_rows(conn, persons, projects, models, start_ts, end_ts,
                           project_amap, model_amap, person_amap)
    finally:
        conn.close()

    rows.sort(key=lambda r: r["ts"] or 0, reverse=True)
    out = []
    for r in rows[:limit]:
        out.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "title": r["title"] or r["session_id"] or f"#{r['id']}",
            "person": r["c_person"],
            "project": r["c_project"],
            "model": model_display(r["c_model"]),
            "ts": r["ts"],
            "tcer": r["tcer"],
            "ctei": r["ctei"],
            "net_loc": r["net_loc"],
            "total_tokens": r["total_tokens"],
            "cost_usd": r["cost_usd"],
            "aggregate_only": r["kind"] == "aggregate",
        })
    return {"sessions": out, "total": len(rows)}


def session_detail(row_id: int) -> dict | None:
    """Full ``raw_json`` for one uploaded row, plus its provenance flags."""
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM uploads WHERE id=?", (row_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    try:
        raw = json.loads(r["raw_json"])
    except (ValueError, TypeError):
        raw = {}
    return {
        "id": r["id"],
        "session_id": r["session_id"],
        "title": r["title"],
        "person": r["person"],
        "project": r["project"],
        "model": r["model"],
        "ts": r["ts"],
        "aggregate_only": r["kind"] == "aggregate",
        "has_transcript": bool(raw.get("transcript")),
        "raw": raw,
    }