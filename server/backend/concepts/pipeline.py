"""重建调度（文档 §3 S1–S3 落库）。

入口：
- ``process_upload(conn, row, tb, …)``：处理一条 session 上传（幂等：先删后写）。
- ``rebuild(full=False)``：增量——按 ``concept_processed`` 的内容哈希 + lexicon 版本
  判断哪些上传需要重算；撤回授权 / 删除的上传清除派生数据；最后重挖候选新词。
- ``schedule()``：上传后调用，后台线程合并触发（多次上传只跑一次）。

只处理 ``kind='session' AND semantic_consent=1`` 且带 conversation 的行（授权门，§8.2）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time

import db
from concepts import governance, ingest, lexicon, schema, signals

_DERIVED_BY_ORIGIN = ("misread_event", "substitution_event", "fidelity_event")

_lock = threading.Lock()
_pending = threading.Event()
_worker: threading.Thread | None = None
_state = {"running": False, "last_run_at": None, "last_stats": None, "last_error": None}


# ------------------------------ 清除 ------------------------------ #
def _purge_origin(conn: sqlite3.Connection, origin: str, ref: str) -> None:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM utterance WHERE origin=? AND origin_ref=?", (origin, ref))]
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM term_usage WHERE utterance_id IN ({ph})", ids)
    conn.execute("DELETE FROM utterance WHERE origin=? AND origin_ref=?", (origin, ref))
    if origin == "upload":
        for t in _DERIVED_BY_ORIGIN:
            conn.execute(f"DELETE FROM {t} WHERE origin_ref=?", (ref,))
        conn.execute("DELETE FROM session_facet WHERE origin_ref=?", (ref,))
    conn.execute("DELETE FROM concept_processed WHERE origin=? AND origin_ref=?", (origin, ref))


# ------------------------------ 单条处理 ------------------------------ #
def _content_hash(raw_json: str, extra: str = "") -> str:
    return hashlib.sha1((raw_json + "|" + extra).encode("utf-8", "ignore")).hexdigest()


def process_upload(conn: sqlite3.Connection, row: sqlite3.Row | dict, tb,
                   *, person: str, project: str, lex_v: int) -> dict:
    ref = str(row["id"])
    _purge_origin(conn, "upload", ref)
    data = json.loads(row["raw_json"])
    blocks = data.get("conversation") or []
    items = ingest.split_conversation(blocks)
    sid = data.get("session_id") or ref
    session_key = f"{data.get('source') or 'unknown'}:{sid}"
    owner = row["uploaded_by"]
    public = 1 if (row["visibility"] or "private") == "public" else 0

    idx_to_uid: dict[int, int] = {}
    hits: dict[int, set[str]] = {}
    n_usage = 0
    for i, it in enumerate(items):
        if it.kind == "edit":
            continue
        cur = conn.execute(
            "INSERT INTO utterance(origin, origin_ref, block_idx, session_key, owner, person, "
            "project, speaker, user_turn, ts, public, text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("upload", ref, it.block_idx, session_key, owner, person, project, it.kind,
             it.user_turn, it.ts or row["ts"], public, it.text))
        uid = int(cur.lastrowid)
        idx_to_uid[i] = uid
        found = lexicon.find_hits(it.text, tb)
        if found:
            hits[i] = {h.term.slug for h in found}
            for h in found:
                conn.execute(
                    "INSERT OR IGNORE INTO term_usage(utterance_id, term_slug, surface, "
                    "char_start, char_end) VALUES(?,?,?,?,?)",
                    (uid, h.term.slug, h.matched_label, h.start, h.end))
                n_usage += 1

    base = (ref, session_key, owner, person, project)
    cost = data.get("cost_usd")
    tokens = data.get("total_tokens")
    mis = signals.misread_events(items, hits,
                                 session_cost=float(cost) if cost else None,
                                 session_tokens=int(tokens) if tokens else None)
    for e in mis:
        conn.execute(
            "INSERT INTO misread_event(origin_ref, session_key, owner, person, project, term_slug, "
            "usage_utt_id, correction_utt_id, user_turns_between, ai_blocks_between, cost_usd_est, "
            "tokens_est, evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*base, e["slug"], idx_to_uid[e["usage_idx"]], idx_to_uid[e["correction_idx"]],
             e["user_turns_between"], e["ai_blocks_between"], e["cost_usd_est"], e["tokens_est"],
             e["evidence"]))
    for e in signals.substitution_events(items, hits):
        conn.execute(
            "INSERT INTO substitution_event(origin_ref, owner, person, project, from_slug, to_slug, "
            "user_utt_id, ai_utt_id) VALUES(?,?,?,?,?,?,?,?)",
            (ref, owner, person, project, e["from_slug"], e["to_slug"],
             idx_to_uid[e["user_idx"]], idx_to_uid[e["ai_idx"]]))
    for e in signals.fidelity_events(items, hits):
        conn.execute(
            "INSERT INTO fidelity_event(origin_ref, owner, person, project, utt_id, expected_json, "
            "kept_json) VALUES(?,?,?,?,?,?,?)",
            (ref, owner, person, project, idx_to_uid[e["idx"]],
             json.dumps(e["expected"], ensure_ascii=False), json.dumps(e["kept"], ensure_ascii=False)))
    for name, val in (("user_turns", max((it.user_turn for it in items), default=-1) + 1),
                      ("source", data.get("source") or "")):
        conn.execute("INSERT OR REPLACE INTO session_facet(session_key, origin_ref, name, value) "
                     "VALUES(?,?,?,?)", (session_key, ref, name, str(val)))
    conn.execute(
        "INSERT OR REPLACE INTO concept_processed(origin, origin_ref, content_hash, "
        "lexicon_version, processed_at) VALUES(?,?,?,?,?)",
        ("upload", ref, _content_hash(row["raw_json"], row["visibility"] or ""), lex_v,
         int(time.time())))
    return {"utterances": len(idx_to_uid), "usages": n_usage, "misreads": len(mis)}


def process_document(conn: sqlite3.Connection, doc: sqlite3.Row, tb, lex_v: int) -> int:
    ref = str(doc["id"])
    _purge_origin(conn, "doc", ref)
    n = 0
    paras = [p for p in (doc["text"] or "").split("\n\n") if p.strip()]
    for i, p in enumerate(paras):
        t = ingest.clean_text(p)
        if not t:
            continue
        cur = conn.execute(
            "INSERT INTO utterance(origin, origin_ref, block_idx, owner, speaker, ts, public, text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("doc", ref, i, doc["uploaded_by"], "doc", doc["uploaded_at"],
             1 if doc["visibility"] == "public" else 0, t))
        uid = int(cur.lastrowid)
        for h in lexicon.find_hits(t, tb):
            conn.execute("INSERT OR IGNORE INTO term_usage(utterance_id, term_slug, surface, "
                         "char_start, char_end) VALUES(?,?,?,?,?)",
                         (uid, h.term.slug, h.matched_label, h.start, h.end))
        n += 1
    conn.execute("INSERT OR REPLACE INTO concept_processed(origin, origin_ref, content_hash, "
                 "lexicon_version, processed_at) VALUES(?,?,?,?,?)",
                 ("doc", ref, _content_hash(doc["text"] or "", doc["visibility"]), lex_v,
                  int(time.time())))
    return n


# ------------------------------ 候选新词 ------------------------------ #
def mine_candidates(conn: sqlite3.Connection, tb) -> int:
    user_utts = [dict(r) for r in conn.execute(
        "SELECT text, person, project FROM utterance WHERE speaker='user'")]
    bg = [r[0] for r in conn.execute(
        "SELECT text FROM utterance WHERE speaker='assistant' ORDER BY RANDOM() LIMIT 20000")]
    cands = lexicon.mine_candidates(user_utts, bg, lexicon.known_label_set(tb))
    decisions = {r["surface"]: r["status"] for r in conn.execute(
        "SELECT surface, status FROM candidate_decision")}
    now = int(time.time())
    conn.execute("DELETE FROM term_candidate")
    for c in cands:
        conn.execute(
            "INSERT INTO term_candidate(surface, g2, freq, n_persons, n_projects, persons_json, "
            "nearest_slug, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (c["surface"], c["g2"], c["freq"], len(c["persons"]), len(c["projects"]),
             json.dumps(c["persons"], ensure_ascii=False), lexicon.nearest_slug(c["surface"], tb),
             decisions.get(c["surface"], "new"), now))
    return len(cands)


# ------------------------------ 重建 ------------------------------ #
def rebuild(full: bool = False) -> dict:
    with _lock:
        _state["running"] = True
        t0 = time.time()
        try:
            stats = _rebuild_locked(full)
            _state["last_stats"] = stats
            _state["last_error"] = None
            return stats
        except Exception as e:  # noqa: BLE001 — 记录后抛出，后台线程不崩
            _state["last_error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            _state["running"] = False
            _state["last_run_at"] = int(time.time())
            if _state.get("last_stats"):
                _state["last_stats"]["seconds"] = round(time.time() - t0, 2)


def _rebuild_locked(full: bool) -> dict:
    tb = governance.termbase()
    conn = db.connect()
    try:
        lex_v = schema.lexicon_version(conn)
        processed = {(r["origin"], r["origin_ref"]): (r["content_hash"], r["lexicon_version"])
                     for r in conn.execute("SELECT * FROM concept_processed")}
        amap_p = db.get_aliases("project")
        amap_u = db.get_aliases("person")
        fmap = db.feishu_name_map()
        rows = conn.execute(
            "SELECT id, uploaded_by, person, project, ts, visibility, raw_json FROM uploads "
            "WHERE kind='session' AND semantic_consent=1").fetchall()
        live: set[str] = set()
        stats = {"processed": 0, "skipped": 0, "purged": 0, "usages": 0, "misreads": 0,
                 "documents": 0, "candidates": 0, "lexicon_version": lex_v}
        for r in rows:
            if '"conversation"' not in r["raw_json"]:
                continue
            ref = str(r["id"])
            live.add(ref)
            h = _content_hash(r["raw_json"], r["visibility"] or "")
            if not full and processed.get(("upload", ref)) == (h, lex_v):
                stats["skipped"] += 1
                continue
            res = process_upload(
                conn, r, tb,
                person=db.canonical_person(r["person"], amap_u, fmap),
                project=db.canonical_project(r["project"], amap_p), lex_v=lex_v)
            stats["processed"] += 1
            stats["usages"] += res["usages"]
            stats["misreads"] += res["misreads"]
            if stats["processed"] % 50 == 0:
                conn.commit()
        # 撤回授权 / 被删除的上传：清除全部派生数据。
        for (origin, ref) in list(processed):
            if origin == "upload" and ref not in live:
                _purge_origin(conn, "upload", ref)
                stats["purged"] += 1
        docs = conn.execute("SELECT * FROM source_document").fetchall()
        live_docs = set()
        for d in docs:
            ref = str(d["id"])
            live_docs.add(ref)
            h = _content_hash(d["text"] or "", d["visibility"])
            if not full and processed.get(("doc", ref)) == (h, lex_v):
                continue
            process_document(conn, d, tb, lex_v)
            stats["documents"] += 1
        for (origin, ref) in list(processed):
            if origin == "doc" and ref not in live_docs:
                _purge_origin(conn, "doc", ref)
        stats["candidates"] = mine_candidates(conn, tb)
        schema.set_meta(conn, "last_rebuild_at", str(int(time.time())))
        conn.commit()
        return stats
    finally:
        conn.close()


def _loop() -> None:
    while True:
        _pending.wait()
        time.sleep(1.0)       # 合并短时间内的多次触发
        _pending.clear()
        try:
            rebuild(full=False)
        except Exception:  # noqa: BLE001 — 状态已记录到 _state
            pass


def schedule() -> None:
    """上传 / 术语库变更 / 授权变更后调用：后台增量重建（合并触发）。"""
    global _worker
    _pending.set()
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, name="concepts-rebuild", daemon=True)
        _worker.start()


def status() -> dict:
    conn = db.connect()
    try:
        last = schema.get_meta(conn, "last_rebuild_at", "")
    finally:
        conn.close()
    return {"running": _state["running"], "last_rebuild_at": int(last) if last else None,
            "last_stats": _state["last_stats"], "last_error": _state["last_error"]}
