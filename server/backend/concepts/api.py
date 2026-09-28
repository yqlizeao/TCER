"""概念对齐 HTTP 路由（由 ``server.Handler`` 分发，前缀 ``/api/concepts/``）。

鉴权模型：
- 所有接口需登录（Bearer）；个人视图只用**登录身份**决定数据主体，不接受 person
  参数（红线 R2）；受托查看走 ``?as=<owner>`` 且必须有 ``person_grant`` 记录。
- 管理诊断需 manager；治理写操作按角色（governance 内再校验）。
- 每次个人 / 管理视图访问写 ``view_access_log``（R4）。
"""
from __future__ import annotations

from concepts import disclosure, governance, pipeline, profile, rbac, views

import db

MAX_DOC_CHARS = 2_000_000
DOC_KINDS = {"glossary": "术语表", "wiki": "Wiki", "prd": "需求文档", "onboarding": "入职文档",
             "im": "协作聊天导出", "other": "其他"}


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _need(user: str, role: str) -> None:
    if not rbac.has_role(user, role):
        raise ApiError(403, f"需要「{rbac.ROLES.get(role, role)}」权限")


def _one(qs: dict, key: str, default=None):
    return qs.get(key, [default])[0]


# ------------------------------ GET ------------------------------ #
def handle_get(route: str, qs: dict, user: str) -> dict:
    sub = route[len("/api/concepts"):] or "/"
    if sub == "/me":
        subject = _one(qs, "as") or user
        if not rbac.can_view_personal(user, subject):
            raise ApiError(403, "个人视图仅本人或本人授权的账号可见")
        if subject != user:   # 只记他人查看（本人自查不构成需审计的访问）
            disclosure.log_access(user, "personal", subject)
        data = views.personal(subject)
        data["viewing_as_grantee"] = subject != user
        return data
    if sub == "/whoami":
        return {"username": user, "roles": sorted(rbac.roles_of(user)),
                "role_labels": rbac.ROLES, "granted_by": rbac.granted_to(user),
                "functions": profile.ROLE_LABELS, "doc_kinds": DOC_KINDS}
    if sub == "/org":
        return views.org_overview()
    if sub == "/concept":
        slug = _one(qs, "slug") or ""
        d = views.concept_detail(slug)
        if d is None:
            raise ApiError(404, "概念不存在")
        return d
    if sub == "/diagnosis":
        _need(user, "manager")
        disclosure.log_access(user, "diagnosis")
        return views.diagnosis()
    if sub == "/proposals":
        return {"proposals": governance.list_proposals(_one(qs, "status") or "open"),
                "can_review": rbac.has_role(user, "reviewer")}
    if sub == "/disputes":
        return {"disputes": governance.list_disputes(slug=_one(qs, "slug"),
                                                     status=_one(qs, "status"))}
    if sub == "/termbase/export":
        return governance.export_termbase(include_draft=_one(qs, "draft") == "1")
    if sub == "/context":
        slugs = [s for s in (_one(qs, "slugs") or "").split(",") if s]
        return {"markdown": governance.context_markdown(slugs or None)}
    if sub == "/profiles":
        _need(user, "steward")
        return {"profiles": list(profile.all_profiles().values()),
                "known_persons": db.distinct_values().get("persons", []),
                "functions": profile.ROLE_LABELS}
    if sub == "/roles":
        _need(user, "admin")
        return {"users": rbac.list_role_assignments(), "roles": rbac.ROLES}
    if sub == "/documents":
        return {"documents": _list_documents(user), "kinds": DOC_KINDS}
    if sub == "/status":
        return pipeline.status() | {"rules": disclosure.rules()}
    raise ApiError(404, "not found")


def _list_documents(user: str) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, kind, title, uploaded_by, visibility, uploaded_at, "
                            "LENGTH(text) AS chars FROM source_document ORDER BY uploaded_at DESC")
        return [dict(r) for r in rows
                if r["visibility"] == "public" or r["uploaded_by"] == user]
    finally:
        conn.close()


# ------------------------------ POST ------------------------------ #
def handle_post(route: str, body: dict, user: str) -> dict:
    sub = route[len("/api/concepts"):]
    b = body or {}
    try:
        if sub == "/propose":
            pid = governance.propose(user, b.get("entry") or {}, reason=b.get("reason") or "",
                                     action=b.get("action"))
            return {"ok": True, "id": pid}
        if sub == "/decide":
            governance.decide(user, int(b["id"]), bool(b.get("approve")), b.get("note") or "")
            pipeline.schedule()
            return {"ok": True}
        if sub == "/dispute":
            return {"ok": True, "id": governance.raise_dispute(user, b.get("slug") or "",
                                                               b.get("body") or "")}
        if sub == "/dispute/resolve":
            governance.resolve_dispute(user, int(b["id"]), bool(b.get("accept")),
                                       b.get("reply") or "")
            return {"ok": True}
        if sub == "/termbase/import":
            res = governance.import_termbase(user, b.get("termbase") or {},
                                             apply=bool(b.get("apply")))
            if res.get("applied"):
                pipeline.schedule()
            return res
        if sub == "/candidate":
            return _candidate_action(user, b)
        if sub == "/consent":
            n = profile.set_consent(user, bool(b.get("on")))
            pipeline.schedule()
            return {"ok": True, "updated": n}
        if sub == "/my-profile":
            persons = profile.persons_of_user(user)
            target = b.get("person") or (persons[0] if persons else user)
            if target not in persons and target != user:
                raise ApiError(403, "只能设置自己的档案")
            profile.upsert(target, username=user, function=b.get("function") or None,
                           team=b.get("team") or None, source="self",
                           personal_opt_out=b.get("personal_opt_out"))
            return {"ok": True}
        if sub == "/grant":
            if b.get("revoke"):
                rbac.revoke(user, b.get("grantee") or "")
            else:
                rbac.grant(user, b.get("grantee") or "")
            return {"ok": True, "grants": rbac.grants_of(user)}
        if sub == "/profiles/import":
            _need(user, "steward")
            return profile.import_csv(b.get("csv") or "")
        if sub == "/profiles/set":
            _need(user, "steward")
            profile.upsert(b["person"], function=b.get("function") or None,
                           team=b.get("team") or None, username=b.get("username") or None,
                           source="imported")
            return {"ok": True}
        if sub == "/roles":
            _need(user, "admin")
            rbac.set_roles(b["username"], list(b.get("roles") or []))
            return {"ok": True}
        if sub == "/documents":
            return _add_document(user, b)
        if sub == "/documents/delete":
            return _delete_document(user, int(b["id"]))
        if sub == "/rebuild":
            _need(user, "steward")
            stats = pipeline.rebuild(full=bool(b.get("full")))
            return {"ok": True, "stats": stats}
    except (KeyError, TypeError) as e:
        raise ApiError(400, f"参数缺失或格式错误：{e}") from None
    except PermissionError as e:
        raise ApiError(403, str(e)) from None
    except ValueError as e:  # GovernanceError 是 ValueError 子类
        raise ApiError(400, str(e)) from None
    raise ApiError(404, "not found")


def _candidate_action(user: str, b: dict) -> dict:
    surface = (b.get("surface") or "").strip()
    action = b.get("action")
    if not surface:
        raise ApiError(400, "缺少候选词")
    conn = db.connect()
    try:
        if action == "ignore":          # 本人「无需收录」：只影响自己的视图
            conn.execute("INSERT OR IGNORE INTO candidate_ignore(owner, surface) VALUES(?,?)",
                         (user, surface))
        elif action in ("nominate", "reject"):
            if action == "reject":
                _need(user, "reviewer")
            status = "nominated" if action == "nominate" else "rejected"
            import time as _t
            conn.execute("INSERT INTO candidate_decision(surface, status, decided_by, decided_at) "
                         "VALUES(?,?,?,?) ON CONFLICT(surface) DO UPDATE SET status=excluded.status, "
                         "decided_by=excluded.decided_by, decided_at=excluded.decided_at",
                         (surface, status, user, int(_t.time())))
            conn.execute("UPDATE term_candidate SET status=? WHERE surface=?", (status, surface))
        else:
            raise ApiError(400, "action 必须为 nominate | reject | ignore")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _add_document(user: str, b: dict) -> dict:
    kind = b.get("kind") or "other"
    if kind not in DOC_KINDS:
        raise ApiError(400, "未知文档类型")
    text = b.get("text") or ""
    title = (b.get("title") or "").strip()
    if not title or not text.strip():
        raise ApiError(400, "标题与正文必填")
    if len(text) > MAX_DOC_CHARS:
        raise ApiError(400, "文档过大（上限 200 万字符）")
    vis = "private" if b.get("visibility") == "private" else "public"
    import time as _t
    conn = db.connect()
    try:
        c = conn.execute("INSERT INTO source_document(kind, title, uploaded_by, visibility, text, "
                         "uploaded_at) VALUES(?,?,?,?,?,?)",
                         (kind, title, user, vis, text, int(_t.time())))
        conn.commit()
        did = int(c.lastrowid)
    finally:
        conn.close()
    pipeline.schedule()
    return {"ok": True, "id": did}


def _delete_document(user: str, did: int) -> dict:
    conn = db.connect()
    try:
        r = conn.execute("SELECT uploaded_by FROM source_document WHERE id=?", (did,)).fetchone()
        if r is None:
            raise ApiError(404, "文档不存在")
        if r["uploaded_by"] != user and not rbac.has_role(user, "steward"):
            raise ApiError(403, "只能删除自己上传的文档")
        conn.execute("DELETE FROM source_document WHERE id=?", (did,))
        conn.commit()
    finally:
        conn.close()
    pipeline.schedule()
    return {"ok": True}
