"""治理层：团队术语库（文档 §5.6）。

- ``concept``：当前生效条目（匹配 / 基线都读它）。``entry_json`` 与客户端
  ``TermEntry.to_dict()`` 同构，校验复用 ``tcer.core.termbase.validate_entry``，
  两端规则不漂移。
- ``concept_proposal``：待评审的变更（新建 / 修改 / 废弃）。评审通过才写入
  ``concept``、版本 +1、写 ``concept_history``、递增 lexicon_version 触发重算。
- ``concept_dispute``：「我认为基线有问题」。驳回必须附理由。
"""
from __future__ import annotations

import json
import sqlite3
import time

import db
from concepts import rbac, schema

from tcer.core import termbase as tb_mod

PROPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS concept_proposal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    action      TEXT NOT NULL,                  -- create | update | deprecate
    entry_json  TEXT NOT NULL,
    base_version INTEGER,                       -- 提案基于的版本（并发修改检测）
    proposed_by TEXT NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | approved | rejected | superseded
    decided_by  TEXT,
    decision_note TEXT,
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cp_status ON concept_proposal(status);
"""

# 概念被质疑累积到该数量仍未处理 → 治理健康度告警。
DISPUTE_ALERT = 3


class GovernanceError(ValueError):
    pass


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(PROPOSAL_SCHEMA)


# ------------------------------ 读 ------------------------------ #
def _row_to_concept(r) -> dict:
    entry = json.loads(r["entry_json"])
    return {"slug": r["slug"], "entry": entry, "status": r["status"], "version": r["version"],
            "owner": r["owner"], "approved_by": r["approved_by"],
            "approved_at": r["approved_at"], "updated_at": r["updated_at"]}


def list_concepts(include_deprecated: bool = True) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM concept ORDER BY slug").fetchall()
    finally:
        conn.close()
    out = [_row_to_concept(r) for r in rows]
    return out if include_deprecated else [c for c in out if c["status"] != "deprecated"]


def get_concept(slug: str) -> dict | None:
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM concept WHERE slug=?", (slug,)).fetchone()
    finally:
        conn.close()
    return _row_to_concept(r) if r else None


def history(slug: str) -> list[dict]:
    conn = db.connect()
    try:
        return [{"version": r["version"], "status": r["status"], "changed_by": r["changed_by"],
                 "changed_at": r["changed_at"], "reason": r["reason"],
                 "entry": json.loads(r["entry_json"])}
                for r in conn.execute("SELECT * FROM concept_history WHERE slug=? "
                                      "ORDER BY version DESC", (slug,))]
    finally:
        conn.close()


def termbase(*, include_draft: bool = True) -> tb_mod.Termbase:
    """当前生效术语库（供匹配）。默认含草稿——草稿用法也值得积累证据供评审。"""
    terms = []
    for c in list_concepts(include_deprecated=False):
        if c["status"] == "draft" and not include_draft:
            continue
        e = tb_mod.TermEntry.from_dict(c["entry"])
        e.status = "active" if c["status"] == "active" else "draft"
        terms.append(e)
    return tb_mod.Termbase(version=1, terms=terms)


# ------------------------------ 提案 ------------------------------ #
def _validate(entry: dict, *, creating: bool) -> dict:
    if not isinstance(entry, dict):
        raise GovernanceError("条目必须为 JSON 对象")
    errs = tb_mod.validate_entry(entry)
    if errs:
        raise GovernanceError("；".join(errs))
    norm = tb_mod.TermEntry.from_dict(entry).to_dict()
    return norm


def propose(user: str, entry: dict, *, reason: str = "", action: str | None = None) -> int:
    entry = _validate(entry, creating=True)
    slug = entry["slug"]
    cur = get_concept(slug)
    if action is None:
        action = "create" if cur is None else "update"
    if action == "create" and cur is not None:
        raise GovernanceError(f"概念 {slug} 已存在，请改为修改提案")
    if action in ("update", "deprecate") and cur is None:
        raise GovernanceError(f"概念 {slug} 不存在")
    if action == "deprecate":
        entry = dict(cur["entry"]) | {"status": "deprecated"}
    blob = json.dumps(entry, ensure_ascii=False)
    conn = db.connect()
    try:
        dup = conn.execute("SELECT 1 FROM concept_proposal WHERE slug=? AND status='open' "
                           "AND action=? AND entry_json=?", (slug, action, blob)).fetchone()
        if dup:
            raise GovernanceError("已有内容相同的提案在等待评审")
        c = conn.execute(
            "INSERT INTO concept_proposal(slug, action, entry_json, base_version, proposed_by, "
            "reason, created_at) VALUES(?,?,?,?,?,?,?)",
            (slug, action, blob, cur["version"] if cur else None, user, reason or "",
             int(time.time())))
        conn.commit()
        return int(c.lastrowid)
    finally:
        conn.close()


def list_proposals(status: str | None = "open") -> list[dict]:
    conn = db.connect()
    try:
        q = "SELECT * FROM concept_proposal"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        rows = conn.execute(q + " ORDER BY created_at DESC", args).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["entry"] = json.loads(d.pop("entry_json"))
        cur = get_concept(d["slug"])
        d["current"] = cur["entry"] if cur else None
        d["stale"] = bool(cur and d["base_version"] is not None
                          and cur["version"] != d["base_version"])
        out.append(d)
    return out


def can_review(user: str, proposal: dict) -> bool:
    roles = rbac.roles_of(user)
    if "admin" in roles:
        return True
    if "reviewer" not in roles:
        return False
    return proposal["proposed_by"] != user  # 评审不能批自己的提案（admin 除外，用于冷启动）


def decide(user: str, proposal_id: int, approve: bool, note: str = "") -> dict:
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM concept_proposal WHERE id=?", (proposal_id,)).fetchone()
        if r is None:
            raise GovernanceError("提案不存在")
        p = dict(r)
        if p["status"] != "open":
            raise GovernanceError("提案已处理")
        if not can_review(user, p):
            raise PermissionError("需要评审权限，且不能评审自己的提案")
        if not approve and not (note or "").strip():
            raise GovernanceError("驳回必须填写理由")
        now = int(time.time())
        if approve:
            cur = conn.execute("SELECT version FROM concept WHERE slug=?", (p["slug"],)).fetchone()
            if cur and p["base_version"] is not None and cur["version"] != p["base_version"]:
                raise GovernanceError("该概念在提案之后已被修改，请重新提案")
            entry = json.loads(p["entry_json"])
            _apply(conn, entry, changed_by=p["proposed_by"], approved_by=user,
                   reason=p["reason"] or p["action"], now=now)
            # 同概念其余未决提案标记为过期（基于旧版本）。
            conn.execute("UPDATE concept_proposal SET status='superseded', decided_at=? "
                         "WHERE slug=? AND status='open' AND id<>?", (now, p["slug"], proposal_id))
        conn.execute("UPDATE concept_proposal SET status=?, decided_by=?, decision_note=?, "
                     "decided_at=? WHERE id=?",
                     ("approved" if approve else "rejected", user, note or "", now, proposal_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _apply(conn: sqlite3.Connection, entry: dict, *, changed_by: str, approved_by: str,
           reason: str, now: int) -> None:
    slug = entry["slug"]
    status = entry.get("status") or "active"
    cur = conn.execute("SELECT version FROM concept WHERE slug=?", (slug,)).fetchone()
    version = (cur["version"] + 1) if cur else 1
    blob = json.dumps(entry, ensure_ascii=False)
    conn.execute(
        "INSERT INTO concept(slug, entry_json, status, version, owner, created_by, approved_by, "
        "approved_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET entry_json=excluded.entry_json, status=excluded.status, "
        "version=excluded.version, owner=excluded.owner, approved_by=excluded.approved_by, "
        "approved_at=excluded.approved_at, updated_at=excluded.updated_at",
        (slug, blob, status, version, entry.get("owner") or None, changed_by, approved_by,
         now, now))
    conn.execute("INSERT INTO concept_history(slug, version, entry_json, status, changed_by, "
                 "changed_at, reason) VALUES(?,?,?,?,?,?,?)",
                 (slug, version, blob, status, changed_by, now, reason))
    schema.bump_lexicon_version(conn)


def import_termbase(user: str, data: dict | list, *, apply: bool = False,
                    reason: str = "导入术语库") -> dict:
    """导入客户端 termbase.json（``{"version":1,"terms":[…]}`` 或裸列表）。

    默认按 slug 生成提案进评审；``apply=True`` 仅 admin/steward 可用（冷启动批量入库）。
    与现有条目完全相同的跳过。
    """
    terms = data.get("terms") if isinstance(data, dict) else data
    if not isinstance(terms, list):
        raise GovernanceError("缺少 terms 列表")
    if apply and not (rbac.has_role(user, "admin") or rbac.has_role(user, "steward")):
        raise PermissionError("直接入库需要管理员或数据管理员权限")
    stats = {"proposed": 0, "applied": 0, "unchanged": 0, "errors": []}
    for i, raw in enumerate(terms, 1):
        try:
            entry = _validate(raw, creating=True)
        except GovernanceError as e:
            stats["errors"].append(f"#{i} {raw.get('slug', '') if isinstance(raw, dict) else ''}：{e}")
            continue
        cur = get_concept(entry["slug"])
        if cur and cur["entry"] == entry:
            stats["unchanged"] += 1
            continue
        if apply:
            conn = db.connect()
            try:
                _apply(conn, entry, changed_by=user, approved_by=user, reason=reason,
                       now=int(time.time()))
                conn.commit()
            finally:
                conn.close()
            stats["applied"] += 1
        else:
            propose(user, entry, reason=reason)
            stats["proposed"] += 1
    return stats


def export_termbase(*, include_draft: bool = False) -> dict:
    """导出为客户端 termbase.json 同构结构（客户端「从团队同步」直接可读）。"""
    terms = []
    for c in list_concepts(include_deprecated=True):
        if c["status"] == "draft" and not include_draft:
            continue
        e = dict(c["entry"])
        e["status"] = c["status"] if c["status"] in tb_mod.STATUS_SET else "draft"
        terms.append(e)
    return {"version": 1, "terms": terms}


# ------------------------------ 质疑 ------------------------------ #
def raise_dispute(user: str, slug: str, body: str) -> int:
    if not get_concept(slug):
        raise GovernanceError("概念不存在")
    if not (body or "").strip():
        raise GovernanceError("请说明你认为基线哪里有问题")
    conn = db.connect()
    try:
        c = conn.execute("INSERT INTO concept_dispute(slug, raised_by, body, created_at) "
                         "VALUES(?,?,?,?)", (slug, user, body.strip(), int(time.time())))
        conn.commit()
        return int(c.lastrowid)
    finally:
        conn.close()


def list_disputes(slug: str | None = None, status: str | None = None,
                  raised_by: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM concept_dispute WHERE 1=1", []
    for col, val in (("slug", slug), ("status", status), ("raised_by", raised_by)):
        if val:
            q += f" AND {col}=?"
            args.append(val)
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at DESC", args)]
    finally:
        conn.close()


def resolve_dispute(user: str, dispute_id: int, accept: bool, reply: str) -> None:
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM concept_dispute WHERE id=?", (dispute_id,)).fetchone()
        if r is None:
            raise GovernanceError("质疑不存在")
        if r["status"] != "open":
            raise GovernanceError("质疑已处理")
        concept = conn.execute("SELECT owner FROM concept WHERE slug=?", (r["slug"],)).fetchone()
        roles = rbac.roles_of(user)
        is_owner = concept is not None and concept["owner"] == user
        if not (is_owner or "reviewer" in roles or "concept_owner" in roles):
            raise PermissionError("需要概念负责人或评审权限")
        if not (reply or "").strip():
            raise GovernanceError("处理质疑必须回复（驳回须说明理由）")
        conn.execute("UPDATE concept_dispute SET status=?, reply=?, resolved_by=?, resolved_at=? "
                     "WHERE id=?", ("accepted" if accept else "rejected", reply.strip(), user,
                                    int(time.time()), dispute_id))
        conn.commit()
    finally:
        conn.close()


# ------------------------------ 健康度 ------------------------------ #
def health() -> dict:
    now = int(time.time())
    concepts = list_concepts()
    open_props = list_proposals("open")
    disputes = list_disputes(status="open")
    by_slug: dict[str, int] = {}
    for d in disputes:
        by_slug[d["slug"]] = by_slug.get(d["slug"], 0) + 1
    oldest = min((d["created_at"] for d in disputes), default=None)
    return {
        "n_concepts": len(concepts),
        "n_active": sum(c["status"] == "active" for c in concepts),
        "n_draft": sum(c["status"] == "draft" for c in concepts),
        "n_deprecated": sum(c["status"] == "deprecated" for c in concepts),
        "no_owner": sorted(c["slug"] for c in concepts
                           if c["status"] != "deprecated" and not c["owner"]),
        "no_definition": sorted(c["slug"] for c in concepts
                                if c["status"] != "deprecated" and not c["entry"].get("definition")),
        "open_proposals": len(open_props),
        "open_disputes": len(disputes),
        "oldest_dispute_days": round((now - oldest) / 86400, 1) if oldest else None,
        "hot_disputes": sorted([s for s, n in by_slug.items() if n >= DISPUTE_ALERT]),
    }


# ------------------------------ 上下文术语段 ------------------------------ #
def context_markdown(slugs: list[str] | None = None, *, title: str = "团队术语") -> str:
    """生成可直接粘进 CLAUDE.md / AGENTS.md 的术语段（文档 §6.4）。

    给 AI 读的，所以写成指令式：含义、别名、常见误解、禁止替换。
    """
    concepts = [c for c in list_concepts(include_deprecated=False) if c["status"] == "active"]
    if slugs:
        want = list(dict.fromkeys(slugs))
        index = {c["slug"]: c for c in concepts}
        concepts = [index[s] for s in want if s in index]
    lines = [f"## {title}", "",
             "以下术语在本团队有特定含义。遇到时按这里的定义理解；沿用用户原话，"
             "不要改用其他叫法；拿不准时先问，不要按通用含义猜。", ""]
    for c in concepts:
        e = c["entry"]
        head = f"- **{e['pref_label']}**"
        if e.get("term_en"):
            head += f"（{e['term_en']}）"
        if e.get("definition"):
            head += f"：{e['definition']}"
        lines.append(head)
        if e.get("alt_labels"):
            lines.append(f"  - 同义叫法：{'、'.join(e['alt_labels'])}")
        for role, text in sorted((e.get("renderings") or {}).items()):
            lines.append(f"  - {tb_mod.ROLE_LABELS.get(role, role)}口中：{text}")
        for m in e.get("misconceptions") or []:
            wrong = m.get("wrong", "")
            actual = m.get("actual", "")
            lines.append(f"  - 不是：{wrong}" + (f"（实际是：{actual}）" if actual else ""))
    if not concepts:
        lines.append("（暂无已批准的术语）")
    return "\n".join(lines).rstrip() + "\n"
