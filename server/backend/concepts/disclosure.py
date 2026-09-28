"""披露层：概念模块所有对外数据的**唯一出口**（文档 §2.3 / §8.3）。

门槛挂在展示层而不是分析层——分析照常处理全部授权数据，视图输出前统一经过这里
过滤。改门槛只改本文件；``tests/test_concepts_disclosure.py`` 钉住红线。
"""
from __future__ import annotations

import time

import db

# 管理视图 / 职能级统计的最小聚合人数（红线 R3）。
MIN_AGGREGATE_PERSONS = 5
# 候选新词进入公共队列：≥3 人使用且 ≥2 个项目；否则只在使用者本人视图出现。
CANDIDATE_MIN_PERSONS = 3
CANDIDATE_MIN_PROJECTS = 2
# 公共视图每个概念最多展示的原文证据条数；单条截断长度。
MAX_PUBLIC_EVIDENCE = 20
EVIDENCE_CHARS = 160
# 职能级义项 / 用法统计的最小样本（每职能 ≥N 条用法且来自 ≥M 人）。
MIN_FUNCTION_USAGES = 8
MIN_FUNCTION_USAGE_PERSONS = 3

SUPPRESSED = "人数不足，按披露规则不展示"


def aggregate_ok(n_persons: int) -> bool:
    return n_persons >= MIN_AGGREGATE_PERSONS


def candidate_public(n_persons: int, n_projects: int) -> bool:
    return n_persons >= CANDIDATE_MIN_PERSONS and n_projects >= CANDIDATE_MIN_PROJECTS


def function_stats_ok(n_usages: int, n_persons: int) -> bool:
    return n_usages >= MIN_FUNCTION_USAGES and n_persons >= MIN_FUNCTION_USAGE_PERSONS


def snippet(text: str, start: int | None = None, end: int | None = None,
            width: int = EVIDENCE_CHARS) -> str:
    """以命中位置为中心截一段证据；无位置时取开头。"""
    t = (text or "").replace("\n", " ")
    if start is None:
        return t[:width] + ("…" if len(t) > width else "")
    half = max(20, (width - (end or start) + start) // 2)
    a = max(0, start - half)
    b = min(len(t), (end or start) + half)
    return ("…" if a > 0 else "") + t[a:b] + ("…" if b < len(t) else "")


def public_evidence(rows: list[dict]) -> list[dict]:
    """公共视图证据：只保留来源会话 visibility=public 的片段，且不带作者。"""
    out = []
    for r in rows:
        if not r.get("public"):
            continue
        out.append({"text": snippet(r["text"], r.get("char_start"), r.get("char_end")),
                    "project": r.get("project"), "ts": r.get("ts")})
        if len(out) >= MAX_PUBLIC_EVIDENCE:
            break
    return out


def suppress_small_groups(rows: list[dict], persons_key: str = "n_persons") -> list[dict]:
    """管理视图的逐行过滤：人数不足的分组整行不出数，只保留标签 + 说明。"""
    out = []
    for r in rows:
        if aggregate_ok(int(r.get(persons_key) or 0)):
            out.append(r)
        else:
            out.append({k: r[k] for k in ("key", "label") if k in r}
                       | {"suppressed": True, "reason": SUPPRESSED})
    return out


def log_access(viewer: str, view: str, subject: str | None = None) -> None:
    conn = db.connect()
    try:
        conn.execute("INSERT INTO view_access_log(viewer, subject, view, at) VALUES(?,?,?,?)",
                     (viewer, subject, view, int(time.time())))
        conn.commit()
    finally:
        conn.close()


def access_log_for(subject: str, limit: int = 100) -> list[dict]:
    """本人可查看谁看过自己的个人视图（红线 R4）。"""
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT viewer, view, at FROM view_access_log WHERE subject=? "
            "ORDER BY at DESC LIMIT ?", (subject, limit))]
    finally:
        conn.close()


def rules() -> dict:
    """前端展示用：当前生效的披露规则（写在页面脚注，规则透明）。"""
    return {
        "min_aggregate_persons": MIN_AGGREGATE_PERSONS,
        "candidate_min_persons": CANDIDATE_MIN_PERSONS,
        "candidate_min_projects": CANDIDATE_MIN_PROJECTS,
        "max_public_evidence": MAX_PUBLIC_EVIDENCE,
        "min_function_usages": MIN_FUNCTION_USAGES,
        "min_function_usage_persons": MIN_FUNCTION_USAGE_PERSONS,
    }
