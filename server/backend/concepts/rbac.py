"""最小 RBAC（文档 §8.1）+ 个人视图的本人授权。

角色：member（默认，不入表）| concept_owner | reviewer | manager | steward | admin。
admin 隐含全部角色，**但不隐含查看他人个人视图**——红线 R2：个人视图只有
本人与本人主动授权的账号可见，任何角色都绕不过。
"""
from __future__ import annotations

import time

import db

ROLES: dict[str, str] = {
    "member": "成员",
    "concept_owner": "概念负责人",
    "reviewer": "评审",
    "manager": "管理者",
    "steward": "数据管理员",
    "admin": "系统管理员",
}


def roles_of(username: str | None) -> set[str]:
    if not username:
        return set()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT role FROM user_role WHERE username=?", (username,)).fetchall()
    finally:
        conn.close()
    out = {"member"} | {r["role"] for r in rows}
    if "admin" in out:
        out |= set(ROLES)
    return out


def has_role(username: str | None, role: str) -> bool:
    return role in roles_of(username)


def set_roles(username: str, roles: list[str]) -> None:
    bad = [r for r in roles if r not in ROLES]
    if bad:
        raise ValueError(f"未知角色：{', '.join(bad)}")
    conn = db.connect()
    try:
        conn.execute("DELETE FROM user_role WHERE username=?", (username,))
        for r in roles:
            if r != "member":
                conn.execute("INSERT OR IGNORE INTO user_role(username, role) VALUES(?,?)",
                             (username, r))
        conn.commit()
    finally:
        conn.close()


def list_role_assignments() -> list[dict]:
    conn = db.connect()
    try:
        users = [r["username"] for r in conn.execute("SELECT username FROM users ORDER BY username")]
        users += [db.feishu_username(r["open_id"])
                  for r in conn.execute("SELECT open_id FROM feishu_users ORDER BY name")]
        rows = conn.execute("SELECT username, role FROM user_role").fetchall()
    finally:
        conn.close()
    by_user: dict[str, list[str]] = {}
    for r in rows:
        by_user.setdefault(r["username"], []).append(r["role"])
    names = db.feishu_name_map()
    out = []
    for u in dict.fromkeys(users + list(by_user)):
        disp = names.get(u[len("feishu:"):], u) if u.startswith("feishu:") else u
        out.append({"username": u, "display": disp, "roles": sorted(by_user.get(u, []))})
    return out


# ------------------------------ 本人授权 ------------------------------ #
def grant(owner: str, grantee: str) -> None:
    if not grantee or grantee == owner:
        raise ValueError("被授权账号无效")
    conn = db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO person_grant(owner, grantee, created_at) VALUES(?,?,?)",
                     (owner, grantee, int(time.time())))
        conn.commit()
    finally:
        conn.close()


def revoke(owner: str, grantee: str) -> None:
    conn = db.connect()
    try:
        conn.execute("DELETE FROM person_grant WHERE owner=? AND grantee=?", (owner, grantee))
        conn.commit()
    finally:
        conn.close()


def grants_of(owner: str) -> list[str]:
    conn = db.connect()
    try:
        return [r["grantee"] for r in conn.execute(
            "SELECT grantee FROM person_grant WHERE owner=? ORDER BY created_at", (owner,))]
    finally:
        conn.close()


def granted_to(grantee: str) -> list[str]:
    """哪些人授权了 ``grantee`` 查看自己的个人视图。"""
    conn = db.connect()
    try:
        return [r["owner"] for r in conn.execute(
            "SELECT owner FROM person_grant WHERE grantee=? ORDER BY created_at", (grantee,))]
    finally:
        conn.close()


def can_view_personal(viewer: str | None, subject: str) -> bool:
    """红线 R2 的唯一判定点。"""
    if not viewer:
        return False
    if viewer == subject:
        return True
    return subject in granted_to(viewer)
