"""三视角载荷组装（文档 §6）。所有数字出口都经过 ``disclosure``。

- ``personal(username)``      个人视图：只读 ``owner=username`` 的数据（红线 R2 由 api 层鉴权）
- ``org_overview()`` / ``concept_detail(slug)`` / ``candidates_public()``  团队全貌
- ``diagnosis()``             管理诊断：只有聚合，任何分组 <5 人整行不出数（R3）

语义层（义项判定 / 2×2 象限 / 义项分布差异）属于 P3，未启用时返回
``semantic: {enabled: false}``，前端显示占位说明而不是编造数字。
"""
from __future__ import annotations

import json
import math
import statistics
import time
from collections import Counter, defaultdict

import db
from concepts import disclosure, governance, profile, rbac

_DAY = 86400


def _labels() -> dict[str, dict]:
    return {c["slug"]: {"label": c["entry"].get("pref_label") or c["slug"],
                        "status": c["status"],
                        "definition": c["entry"].get("definition") or ""}
            for c in governance.list_concepts()}


def _q(sql: str, args: tuple = ()) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _anchor_ts() -> int:
    """时间窗锚点：取数据自身最新时间（同 personas._resolve_window），
    避免服务端时钟与上传数据跨度错开导致窗口内空无一物。"""
    r = _q("SELECT MAX(ts) AS t FROM utterance WHERE origin='upload'")
    return int(r[0]["t"]) if r and r[0]["t"] else int(time.time())


def _semantic_placeholder() -> dict:
    return {"enabled": False,
            "note": "义项判定（语义层）尚未启用：需配置判定模型并通过 POC 门禁后，"
                    "才会输出「用法与基线的差异」「各职能理解分布」与 2×2 诊断象限。"}


# =============================== 个人视图 =============================== #
def _dedupe_examples(evs: list[dict], n: int) -> list[dict]:
    out, seen = [], set()
    for e in evs:
        k = (e["usage_text"], e["correction_text"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"usage": disclosure.snippet(e["usage_text"]),
                    "correction": disclosure.snippet(e["correction_text"]),
                    "evidence": e["evidence"], "ts": e["ts"]})
        if len(out) >= n:
            break
    return out


def personal(username: str) -> dict:
    labels = _labels()
    persons = profile.persons_of_user(username)
    profs = profile.all_profiles()
    my_prof = next((profs[p] for p in persons if p in profs), None)

    usage_rows = _q(
        "SELECT tu.term_slug AS slug, COUNT(*) AS n, COUNT(DISTINCT u.session_key) AS sessions "
        "FROM term_usage tu JOIN utterance u ON u.id=tu.utterance_id "
        "WHERE u.owner=? AND u.speaker='user' GROUP BY tu.term_slug", (username,))
    my_usage = {r["slug"]: r for r in usage_rows}

    # 全团队基准：每个术语的整体误解率（仅作参照，不含任何他人身份信息）。
    team_use = {r["slug"]: r["n"] for r in _q(
        "SELECT tu.term_slug AS slug, COUNT(*) AS n FROM term_usage tu "
        "JOIN utterance u ON u.id=tu.utterance_id WHERE u.speaker='user' GROUP BY tu.term_slug")}
    team_mis = {r["slug"]: r["n"] for r in _q(
        "SELECT term_slug AS slug, COUNT(*) AS n FROM misread_event GROUP BY term_slug")}

    mis_rows = _q(
        "SELECT m.term_slug AS slug, m.evidence, m.cost_usd_est, m.tokens_est, "
        "m.user_turns_between, uu.text AS usage_text, cu.text AS correction_text, uu.ts "
        "FROM misread_event m JOIN utterance uu ON uu.id=m.usage_utt_id "
        "JOIN utterance cu ON cu.id=m.correction_utt_id WHERE m.owner=? ORDER BY uu.ts DESC",
        (username,))
    by_slug: dict[str, list] = defaultdict(list)
    for r in mis_rows:
        by_slug[r["slug"]].append(r)
    ai_misreads = []
    for slug, evs in by_slug.items():
        uses = my_usage.get(slug, {}).get("n", 0) or len(evs)
        cost = sum(e["cost_usd_est"] or 0 for e in evs)
        ai_misreads.append({
            "slug": slug, "label": labels.get(slug, {}).get("label", slug),
            "in_termbase": labels.get(slug, {}).get("status") == "active",
            "events": len(evs), "uses": uses, "rate": round(len(evs) / uses, 3) if uses else None,
            "strong_events": sum(e["evidence"] == "rementioned" for e in evs),
            "team_rate": round(team_mis.get(slug, 0) / team_use[slug], 3) if team_use.get(slug) else None,
            "cost_usd_est": round(cost, 4), "tokens_est": sum(e["tokens_est"] or 0 for e in evs),
            "examples": _dedupe_examples(evs, 3),
        })
    ai_misreads.sort(key=lambda x: (-x["events"], -(x["cost_usd_est"] or 0)))

    subs = _q("SELECT from_slug, to_slug, COUNT(*) AS n FROM substitution_event WHERE owner=? "
              "GROUP BY from_slug, to_slug ORDER BY n DESC LIMIT 20", (username,))
    for s in subs:
        s["from_label"] = labels.get(s["from_slug"], {}).get("label", s["from_slug"])
        s["to_label"] = labels.get(s["to_slug"], {}).get("label", s["to_slug"])

    fid = _q("SELECT expected_json, kept_json FROM fidelity_event WHERE owner=?", (username,))
    exp_n = sum(len(json.loads(r["expected_json"])) for r in fid)
    kept_n = sum(len(json.loads(r["kept_json"])) for r in fid)

    # 只有我在用的词：候选里使用者包含本人、且未达到公共队列门槛。
    ignored = {r["surface"] for r in _q("SELECT surface FROM candidate_ignore WHERE owner=?",
                                        (username,))}
    mine = []
    for c in _q("SELECT * FROM term_candidate WHERE status<>'rejected' ORDER BY g2 DESC"):
        ps = set(json.loads(c["persons_json"]))
        if not ps & set(persons) or c["surface"] in ignored:
            continue
        mine.append({"surface": c["surface"], "freq": c["freq"], "n_persons": c["n_persons"],
                     "public": disclosure.candidate_public(c["n_persons"], c["n_projects"]),
                     "nearest_slug": c["nearest_slug"],
                     "nearest_label": labels.get(c["nearest_slug"] or "", {}).get("label"),
                     "status": c["status"]})
    private_jargon = [m for m in mine if not m["public"]][:30]

    terms = []
    for slug, r in sorted(my_usage.items(), key=lambda kv: -kv[1]["n"]):
        lab = labels.get(slug, {})
        terms.append({"slug": slug, "label": lab.get("label", slug), "status": lab.get("status"),
                      "definition": lab.get("definition", ""), "uses": r["n"],
                      "sessions": r["sessions"], "misreads": len(by_slug.get(slug, []))})

    return {
        "username": username,
        "persons": persons,
        "profile": {"function": (my_prof or {}).get("function"),
                    "function_label": profile.function_label((my_prof or {}).get("function")),
                    "function_source": (my_prof or {}).get("function_source"),
                    "team": (my_prof or {}).get("team"),
                    "personal_opt_out": bool((my_prof or {}).get("personal_opt_out"))},
        "consent": profile.consent_summary(username),
        "terms": terms,
        "richness": {"concepts_used": len(my_usage),
                     "note": "你用到的团队概念越多样越好——丰富度高是优点，不是偏差。"},
        "ai_misreads": ai_misreads,
        "substitutions": subs,
        "fidelity": {"dispatches": len(fid), "expected": exp_n, "kept": kept_n,
                     "ratio": round(kept_n / exp_n, 3) if exp_n else None},
        "private_jargon": private_jargon,
        "disputes": governance.list_disputes(raised_by=username),
        "grants": rbac.grants_of(username),
        "access_log": disclosure.access_log_for(username, 30),
        "semantic": _semantic_placeholder(),
    }


# =============================== 团队全貌 =============================== #
def _concept_usage_stats() -> dict[str, dict]:
    rows = _q(
        "SELECT tu.term_slug AS slug, COUNT(*) AS n, COUNT(DISTINCT u.person) AS persons, "
        "COUNT(DISTINCT u.project) AS projects, COUNT(DISTINCT u.session_key) AS sessions, "
        "MAX(u.ts) AS last_ts FROM term_usage tu JOIN utterance u ON u.id=tu.utterance_id "
        "WHERE u.speaker='user' GROUP BY tu.term_slug")
    out = {r["slug"]: r for r in rows}
    for r in _q("SELECT term_slug AS slug, COUNT(*) AS n, SUM(cost_usd_est) AS cost "
                "FROM misread_event GROUP BY term_slug"):
        out.setdefault(r["slug"], {"slug": r["slug"], "n": 0, "persons": 0, "projects": 0,
                                   "sessions": 0, "last_ts": None})
        out[r["slug"]]["misreads"] = r["n"]
        out[r["slug"]]["misread_cost"] = r["cost"] or 0.0
    for r in _q("SELECT tu.term_slug AS slug, COUNT(*) AS n FROM term_usage tu "
                "JOIN utterance u ON u.id=tu.utterance_id WHERE u.speaker='doc' "
                "GROUP BY tu.term_slug"):
        out.setdefault(r["slug"], {"slug": r["slug"], "n": 0, "persons": 0, "projects": 0,
                                   "sessions": 0, "last_ts": None})
        out[r["slug"]]["doc_mentions"] = r["n"]
    return out


def org_overview() -> dict:
    stats = _concept_usage_stats()
    concepts = []
    for c in governance.list_concepts():
        e = c["entry"]
        s = stats.get(c["slug"], {})
        n = s.get("n", 0)
        mis = s.get("misreads", 0)
        concepts.append({
            "slug": c["slug"], "label": e.get("pref_label") or c["slug"],
            "term_en": e.get("term_en", ""), "status": c["status"], "version": c["version"],
            "mda_layer": e.get("mda_layer", ""), "owner": c["owner"],
            "definition": e.get("definition", ""), "alt_labels": e.get("alt_labels", []),
            "n_renderings": len(e.get("renderings") or {}),
            "n_misconceptions": len(e.get("misconceptions") or []),
            "uses": n, "persons": s.get("persons", 0), "projects": s.get("projects", 0),
            "doc_mentions": s.get("doc_mentions", 0),
            # 误解率只在使用人数达到候选公开门槛时给出（避免从小样本反推个人）。
            "misread_rate": (round(mis / n, 3) if n and s.get("persons", 0)
                             >= disclosure.CANDIDATE_MIN_PERSONS else None),
            "last_ts": s.get("last_ts"),
        })
    concepts.sort(key=lambda x: (x["status"] == "deprecated", -x["uses"], x["slug"]))
    return {"concepts": concepts, "cooccurrence": cooccurrence(),
            "trend": trend(), "health": governance.health(),
            "candidates": candidates_public(), "semantic": _semantic_placeholder(),
            "rules": disclosure.rules()}


def concept_detail(slug: str) -> dict | None:
    c = governance.get_concept(slug)
    if not c:
        return None
    ev_rows = _q(
        "SELECT u.text, u.project, u.ts, u.public, tu.char_start, tu.char_end "
        "FROM term_usage tu JOIN utterance u ON u.id=tu.utterance_id "
        "WHERE tu.term_slug=? AND u.speaker IN ('user','doc') ORDER BY u.ts DESC LIMIT 400",
        (slug,))
    fmap = profile.function_map(allow_inferred=True)
    fn_rows = _q("SELECT u.person, COUNT(*) AS n FROM term_usage tu JOIN utterance u "
                 "ON u.id=tu.utterance_id WHERE tu.term_slug=? AND u.speaker='user' "
                 "GROUP BY u.person", (slug,))
    by_fn: dict[str, dict] = {}
    for r in fn_rows:
        fn = fmap.get(r["person"] or "", "unknown")
        d = by_fn.setdefault(fn, {"uses": 0, "persons": 0})
        d["uses"] += r["n"]
        d["persons"] += 1
    functions, other = [], {"uses": 0, "persons": 0}
    for fn, d in by_fn.items():
        if fn != "unknown" and disclosure.function_stats_ok(d["uses"], d["persons"]):
            functions.append({"function": fn, "label": profile.function_label(fn), **d})
        else:
            other["uses"] += d["uses"]
            other["persons"] += d["persons"]
    functions.sort(key=lambda x: -x["uses"])
    if other["uses"]:
        functions.append({"function": "other", "label": "其他 / 未设置职能", **other,
                          "merged": True})
    stats = _concept_usage_stats().get(slug, {})
    co = [e for e in cooccurrence()["edges"] if slug in (e["a"], e["b"])]
    return {
        "concept": c, "history": governance.history(slug),
        "disputes": [{k: d[k] for k in ("id", "body", "status", "reply", "created_at",
                                        "resolved_at")}
                     for d in governance.list_disputes(slug=slug)],
        "stats": {"uses": stats.get("n", 0), "persons": stats.get("persons", 0),
                  "projects": stats.get("projects", 0), "sessions": stats.get("sessions", 0),
                  "misreads": stats.get("misreads", 0), "doc_mentions": stats.get("doc_mentions", 0)},
        "functions": functions,
        "evidence": disclosure.public_evidence(ev_rows),
        "evidence_hidden": sum(1 for r in ev_rows if not r["public"]),
        "cooccurs": co,
        "open_proposals": [p for p in governance.list_proposals("open") if p["slug"] == slug],
        "semantic": _semantic_placeholder(),
    }


def candidates_public() -> list[dict]:
    labels = _labels()
    out = []
    for c in _q("SELECT * FROM term_candidate WHERE status<>'rejected' ORDER BY g2 DESC"):
        if not disclosure.candidate_public(c["n_persons"], c["n_projects"]):
            continue
        out.append({"surface": c["surface"], "g2": c["g2"], "freq": c["freq"],
                    "n_persons": c["n_persons"], "n_projects": c["n_projects"],
                    "nearest_slug": c["nearest_slug"],
                    "nearest_label": labels.get(c["nearest_slug"] or "", {}).get("label"),
                    "status": c["status"]})
    return out[:100]


def cooccurrence(min_count: int = 3) -> dict:
    """同一条 user/doc utterance 内的概念共现，按 NPMI 排序（描述层，非结论）。"""
    rows = _q("SELECT tu.utterance_id AS uid, tu.term_slug AS slug FROM term_usage tu "
              "JOIN utterance u ON u.id=tu.utterance_id WHERE u.speaker IN ('user','doc')")
    per: dict[int, set] = defaultdict(set)
    for r in rows:
        per[r["uid"]].add(r["slug"])
    n = len(per)
    single: Counter = Counter()
    pair: Counter = Counter()
    for s in per.values():
        for a in s:
            single[a] += 1
        ss = sorted(s)
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                pair[(ss[i], ss[j])] += 1
    edges = []
    for (a, b), c in pair.items():
        if c < min_count or n == 0:
            continue
        p_ab, p_a, p_b = c / n, single[a] / n, single[b] / n
        if p_ab >= 1.0:
            npmi = 1.0
        else:
            npmi = math.log(p_ab / (p_a * p_b)) / -math.log(p_ab)
        edges.append({"a": a, "b": b, "count": c, "npmi": round(npmi, 3)})
    edges.sort(key=lambda e: (-e["npmi"], -e["count"]))
    return {"nodes": [{"slug": s, "count": c} for s, c in single.most_common(80)],
            "edges": edges[:200]}


def trend(months: int = 6, top: int = 8) -> dict:
    since = _anchor_ts() - months * 31 * _DAY
    rows = _q("SELECT tu.term_slug AS slug, strftime('%Y-%m', u.ts, 'unixepoch') AS m, "
              "COUNT(*) AS n FROM term_usage tu JOIN utterance u ON u.id=tu.utterance_id "
              "WHERE u.speaker='user' AND u.ts>=? GROUP BY slug, m", (since,))
    total: Counter = Counter()
    for r in rows:
        total[r["slug"]] += r["n"]
    keep = [s for s, _ in total.most_common(top)]
    months_axis = sorted({r["m"] for r in rows if r["m"]})
    series = {s: {m: 0 for m in months_axis} for s in keep}
    for r in rows:
        if r["slug"] in series and r["m"]:
            series[r["slug"]][r["m"]] = r["n"]
    labels = _labels()
    return {"months": months_axis,
            "series": [{"slug": s, "label": labels.get(s, {}).get("label", s),
                        "values": [series[s][m] for m in months_axis]} for s in keep]}


# =============================== 管理诊断 =============================== #
def coverage() -> dict:
    r = _q("SELECT COUNT(*) AS sessions, SUM(semantic_consent) AS consented, "
           "SUM(CASE WHEN raw_json LIKE '%\"conversation\"%' THEN 1 ELSE 0 END) AS with_text, "
           "COUNT(DISTINCT uploaded_by) AS uploaders FROM uploads WHERE kind='session'")[0]
    consenting = _q("SELECT COUNT(DISTINCT uploaded_by) AS n FROM uploads "
                    "WHERE kind='session' AND semantic_consent=1")[0]["n"]
    analysed_persons = _q("SELECT COUNT(DISTINCT person) AS n FROM utterance "
                          "WHERE origin='upload'")[0]["n"]
    fmap = profile.function_map(allow_inferred=True)
    persons = [r["person"] for r in _q("SELECT DISTINCT person FROM utterance WHERE origin='upload'")]
    fn_count: Counter = Counter(fmap.get(p or "", "unknown") for p in persons)
    functions = []
    for fn, n in fn_count.most_common():
        functions.append({"key": fn, "label": profile.function_label(fn) if fn != "unknown"
                          else "未设置职能", "n_persons": n,
                          "enough": disclosure.aggregate_ok(n)})
    return {"sessions": r["sessions"] or 0, "with_text": r["with_text"] or 0,
            "consented": r["consented"] or 0, "uploaders": r["uploaders"] or 0,
            "consenting_uploaders": consenting, "analysed_persons": analysed_persons,
            "functions": functions,
            "enough": disclosure.aggregate_ok(analysed_persons)}


def diagnosis() -> dict:
    cov = coverage()
    labels = _labels()
    if not cov["enough"]:
        return {"coverage": cov, "insufficient": True,
                "reason": f"已授权分析的成员不足 {disclosure.MIN_AGGREGATE_PERSONS} 人，"
                          f"按披露规则不输出任何管理诊断。",
                "health": governance.health(), "semantic": _semantic_placeholder(),
                "rules": disclosure.rules()}

    # —— 人↔AI 误解代价（按概念）——
    use_rows = {r["slug"]: r for r in _q(
        "SELECT tu.term_slug AS slug, COUNT(*) AS n, COUNT(DISTINCT u.person) AS persons "
        "FROM term_usage tu JOIN utterance u ON u.id=tu.utterance_id WHERE u.speaker='user' "
        "GROUP BY tu.term_slug")}
    mis = _q("SELECT term_slug AS slug, person, project, cost_usd_est, tokens_est, "
             "user_turns_between, evidence FROM misread_event")
    by_c: dict[str, list] = defaultdict(list)
    for m in mis:
        by_c[m["slug"]].append(m)
    misread_concepts = []
    for slug, evs in by_c.items():
        persons = {e["person"] for e in evs}
        uses = use_rows.get(slug, {}).get("n", 0)
        misread_concepts.append({
            "key": slug, "label": labels.get(slug, {}).get("label", slug),
            "in_termbase": labels.get(slug, {}).get("status") == "active",
            "n_persons": len(persons), "events": len(evs), "uses": uses,
            "rate": round(len(evs) / uses, 3) if uses else None,
            "strong_ratio": round(sum(e["evidence"] == "rementioned" for e in evs) / len(evs), 3),
            "cost_usd_est": round(sum(e["cost_usd_est"] or 0 for e in evs), 4),
            "tokens_est": sum(e["tokens_est"] or 0 for e in evs),
            "median_turns": statistics.median(e["user_turns_between"] for e in evs),
        })
    misread_concepts.sort(key=lambda x: (-(x["cost_usd_est"] or 0), -x["events"]))

    by_p: dict[str, list] = defaultdict(list)
    for m in mis:
        by_p[m["project"] or "未标注"].append(m)
    misread_projects = [{
        "key": p, "label": p, "n_persons": len({e["person"] for e in evs}), "events": len(evs),
        "cost_usd_est": round(sum(e["cost_usd_est"] or 0 for e in evs), 4),
        "top_concepts": [labels.get(s, {}).get("label", s)
                         for s, _ in Counter(e["slug"] for e in evs).most_common(5)],
    } for p, evs in by_p.items()]
    misread_projects.sort(key=lambda x: -x["cost_usd_est"])

    # —— AI 换词 ——
    subs_raw = _q("SELECT from_slug, to_slug, person FROM substitution_event")
    pairs: dict[tuple, list] = defaultdict(list)
    for s in subs_raw:
        pairs[(s["from_slug"], s["to_slug"])].append(s["person"])
    substitutions = [{
        "key": f"{a}->{b}",
        "label": f"{labels.get(a, {}).get('label', a)} → {labels.get(b, {}).get('label', b)}",
        "from_slug": a, "to_slug": b, "events": len(ps), "n_persons": len(set(ps)),
    } for (a, b), ps in pairs.items()]
    substitutions.sort(key=lambda x: -x["events"])

    # —— AI↔AI 保真（按项目）——
    fid = _q("SELECT project, person, expected_json, kept_json FROM fidelity_event")
    fp: dict[str, dict] = defaultdict(lambda: {"exp": 0, "kept": 0, "persons": set(), "n": 0})
    for r in fid:
        d = fp[r["project"] or "未标注"]
        d["exp"] += len(json.loads(r["expected_json"]))
        d["kept"] += len(json.loads(r["kept_json"]))
        d["persons"].add(r["person"])
        d["n"] += 1
    fidelity = [{"key": p, "label": p, "n_persons": len(d["persons"]), "dispatches": d["n"],
                 "ratio": round(d["kept"] / d["exp"], 3) if d["exp"] else None}
                for p, d in fp.items()]
    fidelity.sort(key=lambda x: (x["ratio"] if x["ratio"] is not None else 1))

    # —— 同项目跨职能共用（30 天窗）——
    cross = cross_function_shared()

    return {
        "coverage": cov, "insufficient": False,
        "misread_concepts": disclosure.suppress_small_groups(misread_concepts)[:30],
        "misread_projects": disclosure.suppress_small_groups(misread_projects)[:30],
        "substitutions": disclosure.suppress_small_groups(substitutions)[:30],
        "fidelity": disclosure.suppress_small_groups(fidelity)[:30],
        "cross_function": cross,
        "health": governance.health(),
        "semantic": _semantic_placeholder(),
        "rules": disclosure.rules(),
    }


def cross_function_shared(window_days: int = 30) -> list[dict]:
    """同一项目、同一时间窗内被 ≥2 个（人数达标的）职能共同使用的概念。

    这是 AI 会话数据里能观测到的「跨职能共用」（文档 E1）；再叠加误解率，
    高共用 × 高误解 = 两个职能在同一项目里对同一个词各说各话的高危信号。
    """
    fmap = profile.function_map(allow_inferred=True)
    since = _anchor_ts() - window_days * _DAY
    rows = _q("SELECT tu.term_slug AS slug, u.project, u.person FROM term_usage tu "
              "JOIN utterance u ON u.id=tu.utterance_id WHERE u.speaker='user' AND u.ts>=?",
              (since,))
    grid: dict[tuple, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        fn = fmap.get(r["person"] or "")
        if fn:
            grid[(r["slug"], r["project"] or "未标注")][fn].add(r["person"])
    labels = _labels()
    mis = {r["slug"]: r["n"] for r in _q("SELECT term_slug AS slug, COUNT(*) AS n "
                                         "FROM misread_event GROUP BY term_slug")}
    use = {r["slug"]: r["n"] for r in _q(
        "SELECT tu.term_slug AS slug, COUNT(*) AS n FROM term_usage tu JOIN utterance u "
        "ON u.id=tu.utterance_id WHERE u.speaker='user' GROUP BY tu.term_slug")}
    out = []
    for (slug, proj), fns in grid.items():
        n_persons = len(set().union(*fns.values()))
        if len(fns) < 2:
            continue
        out.append({"key": f"{slug}@{proj}", "label": labels.get(slug, {}).get("label", slug),
                    "slug": slug, "project": proj,
                    "functions": sorted(profile.function_label(f) for f in fns),
                    "n_persons": n_persons,
                    "misread_rate": round(mis.get(slug, 0) / use[slug], 3) if use.get(slug) else None})
    out.sort(key=lambda x: (-(x["misread_rate"] or 0), -len(x["functions"])))
    return disclosure.suppress_small_groups(out)[:30]
