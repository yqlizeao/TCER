"""成员职能档案与语义分析授权（文档 §4.1 / §8.2）。

``person_profile.person`` 是归一后的 person 标签（与 ``uploads`` 聚合口径一致），
``username`` 是登录名。个人视图按**登录名**鉴权（``uploads.uploaded_by``），
职能统计按 person 聚合。
"""
from __future__ import annotations

import csv
import io
import time

import db

try:
    from tcer.core.termbase import ROLE_LABELS, _ROLE_KEY_ALIASES
except Exception:  # pragma: no cover
    ROLE_LABELS = {"art": "美术", "designer": "策划", "ux": "交互", "eng": "程序",
                   "audio": "音频", "qa": "QA"}
    _ROLE_KEY_ALIASES = {}

FUNCTION_SOURCES = ("imported", "self", "feishu", "inferred")

# 中文职能名反查（CSV 导入允许填「策划」「程序」）。
_CN_TO_KEY = {v: k for k, v in ROLE_LABELS.items()}


def normalize_function(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s in ROLE_LABELS:
        return s
    low = s.lower()
    if low in ROLE_LABELS:
        return low
    if low in _ROLE_KEY_ALIASES:
        return _ROLE_KEY_ALIASES[low]
    if s in _CN_TO_KEY:
        return _CN_TO_KEY[s]
    return None


def function_label(key: str | None) -> str:
    return ROLE_LABELS.get(key or "", "未设置")


def upsert(person: str, *, username: str | None = None, function: str | None = None,
           team: str | None = None, source: str = "self",
           personal_opt_out: bool | None = None) -> None:
    if source not in FUNCTION_SOURCES:
        raise ValueError("function_source 非法")
    fn = normalize_function(function) if function else None
    if function and fn is None:
        raise ValueError(f"未知职能：{function}")
    now = int(time.time())
    conn = db.connect()
    try:
        cur = conn.execute("SELECT * FROM person_profile WHERE person=?", (person,)).fetchone()
        if cur is None:
            conn.execute(
                "INSERT INTO person_profile(person, username, function, team, function_source, "
                "personal_opt_out, updated_at) VALUES(?,?,?,?,?,?,?)",
                (person, username, fn, team, source, 1 if personal_opt_out else 0, now))
        else:
            conn.execute(
                "UPDATE person_profile SET username=COALESCE(?, username), "
                "function=COALESCE(?, function), team=COALESCE(?, team), "
                "function_source=CASE WHEN ? IS NULL THEN function_source ELSE ? END, "
                "personal_opt_out=COALESCE(?, personal_opt_out), updated_at=? WHERE person=?",
                (username, fn, team, fn, source,
                 None if personal_opt_out is None else (1 if personal_opt_out else 0),
                 now, person))
        conn.commit()
    finally:
        conn.close()


def all_profiles() -> dict[str, dict]:
    conn = db.connect()
    try:
        return {r["person"]: dict(r) for r in conn.execute("SELECT * FROM person_profile")}
    finally:
        conn.close()


def function_map(*, allow_inferred: bool = True) -> dict[str, str]:
    """person → function key；``allow_inferred=False`` 时排除推断来源（红线 R5）。"""
    out = {}
    for p, r in all_profiles().items():
        if not r.get("function"):
            continue
        if not allow_inferred and r.get("function_source") == "inferred":
            continue
        out[p] = r["function"]
    return out


def persons_of_user(username: str) -> list[str]:
    """登录名名下的所有 canonical person（上传身份可能多个，如本名 + 匿名假名）。"""
    conn = db.connect()
    try:
        raws = [r["person"] for r in conn.execute(
            "SELECT DISTINCT person FROM uploads WHERE uploaded_by=?", (username,))]
        bound = [r["person"] for r in conn.execute(
            "SELECT person FROM person_profile WHERE username=?", (username,))]
    finally:
        conn.close()
    amap = db.get_aliases("person")
    fmap = db.feishu_name_map()
    out = [db.canonical_person(p, amap, fmap) for p in raws if p]
    return list(dict.fromkeys(out + bound))


def import_csv(text: str) -> dict:
    """CSV：person,function[,team][,username]。表头可选；职能允许中英文。"""
    ok, errors = 0, []
    reader = csv.reader(io.StringIO(text))
    for i, row in enumerate(reader, 1):
        if not row or not any(c.strip() for c in row):
            continue
        cells = [c.strip() for c in row] + ["", "", "", ""]
        person, fn, team, uname = cells[:4]
        if i == 1 and person.lower() in ("person", "成员", "姓名"):
            continue
        if not person:
            errors.append(f"第 {i} 行：缺少成员")
            continue
        try:
            upsert(person, function=fn or None, team=team or None,
                   username=uname or None, source="imported")
            ok += 1
        except ValueError as e:
            errors.append(f"第 {i} 行：{e}")
    return {"imported": ok, "errors": errors}


# ------------------------------ 语义授权 ------------------------------ #
def consent_summary(username: str) -> dict:
    conn = db.connect()
    try:
        r = conn.execute(
            "SELECT COUNT(*) AS n, SUM(semantic_consent) AS c, "
            "SUM(CASE WHEN raw_json LIKE '%\"conversation\"%' THEN 1 ELSE 0 END) AS d "
            "FROM uploads WHERE kind='session' AND uploaded_by=?", (username,)).fetchone()
    finally:
        conn.close()
    return {"sessions": r["n"] or 0, "consented": r["c"] or 0, "with_text": r["d"] or 0}


def set_consent(username: str, on: bool) -> int:
    """本人一键开/关名下全部会话的语义授权。关闭时派生数据由 pipeline 立即清除。"""
    conn = db.connect()
    try:
        cur = conn.execute("UPDATE uploads SET semantic_consent=? WHERE kind='session' "
                           "AND uploaded_by=?", (1 if on else 0, username))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
