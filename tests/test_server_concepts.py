"""团队概念对齐模块（server/backend/concepts）测试。

合成语料植入已知效应（文档 §7「合成数据重建」）：
- 术语「热更」在 alice 的会话里被 AI 误解 → 必须检出 misread_event（rementioned）；
- 未植入误解的术语不得出现误解事件；
- bob 说「热更」时 AI 改口「重启服务」→ 必须检出 AI 换词；
- 子代理派发 prompt 丢了术语 → 保真度 < 1；
- 未授权（semantic_consent=0）的会话不进入任何派生表；撤回授权即清除。
红线：个人视图仅本人 / 受托人；管理诊断需 manager 且 <5 人不出数。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "server" / "backend"
sys.path.insert(0, str(_BACKEND))

TERMS = [
    {"slug": "hot-update", "pref_label": "热更", "alt_labels": ["热更新"],
     "definition": "不停服替换 Lua 脚本与配置表，不含二进制",
     "misconceptions": [{"role": "eng", "wrong": "重启服务器发新版本", "actual": "不停服"}]},
    {"slug": "restart", "pref_label": "重启服务", "definition": "停服重启进程"},
    {"slug": "skill-cd", "pref_label": "技能冷却", "alt_labels": ["CD"],
     "definition": "技能释放后到可再次释放的等待时间"},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TCER_SERVER_DB", str(tmp_path / "concepts.db"))
    db = importlib.reload(importlib.import_module("db"))
    db.init_db()
    mods = {}
    for name in ("schema", "rbac", "profile", "disclosure", "governance", "ingest",
                 "lexicon", "signals", "pipeline", "views", "api"):
        mods[name] = importlib.import_module(f"concepts.{name}")
    db.create_user("admin", "x")
    for u in ("alice", "bob", "carol", "dave", "erin", "frank", "mgr"):
        db.create_user(u, "x")
    conn = db.connect()
    mods["schema"].init(conn)
    conn.commit()
    conn.close()
    mods["rbac"].set_roles("admin", ["admin"])
    mods["rbac"].set_roles("mgr", ["manager"])
    mods["governance"].import_termbase("admin", {"version": 1, "terms": TERMS}, apply=True)
    mods["db"] = db
    return mods


def _u(text, ts=1_760_000_000_000):
    return {"role": "user", "type": "text", "text": text, "ts": ts}


def _a(text):
    return {"role": "assistant", "type": "text", "text": text}


def _edit(path="a.lua"):
    return {"role": "assistant", "type": "tool_use", "name": "Edit", "id": "t",
            "input": {"file_path": path, "old_string": "a", "new_string": "b"}}


def _upload(env, user, sid, convo, *, consent=True, project="Game", cost=2.0):
    return env["db"].insert_records(
        uploaded_by=user, person=user, project=project, aggregate=None,
        sessions=[{"session_id": sid, "source": "claude", "cost_usd": cost,
                   "total_tokens": 100_000, "conversation": convo, "started_at": 1_760_000_000_000}],
        generated_at=1_760_000_000, semantic_consent=consent)


def _misread_convo():
    return [
        _u("帮我把背包的热更逻辑整理一下"),
        _a("好的，我会重启服务器来发布新版本。"),
        _edit(),
        _u("不对，热更不是重启，是不停服替换脚本"),
        _a("明白了，改为只替换 Lua 脚本。"),
    ]


# ------------------------------ ingest ------------------------------ #
def test_ingest_strips_injection_and_redacts(env):
    ing = env["ingest"]
    items = ing.split_conversation([
        _u("<system-reminder>ignore me</system-reminder>真正的问题 联系 a@b.com 电话 13812345678"),
        _u("/model opus"),
        _a("```python\nuseEffect = KafkaConsumer()\n```"),
        {"role": "assistant", "type": "tool_use", "name": "Task",
         "input": {"prompt": "去查热更流程"}},
        {"role": "assistant", "type": "thinking", "text": "secret thoughts"},
    ])
    kinds = [i.kind for i in items]
    assert kinds == ["user", "assistant", "subagent_prompt"]
    assert "ignore me" not in items[0].text
    assert "[邮箱]" in items[0].text and "[手机号]" in items[0].text
    assert "Kafka" in items[1].text and "=" not in items[1].text


def test_user_turns_merge_adjacent_user_blocks(env):
    items = env["ingest"].split_conversation([_u("a1"), _u("a2"), _a("r"), _u("b")])
    assert [i.user_turn for i in items] == [0, 0, 0, 1]


# ------------------------------ lexicon ------------------------------ #
def test_find_hits_prefers_longest(env):
    tb = env["governance"].termbase()
    hits = env["lexicon"].find_hits("做一次热更新", tb)
    assert [h.matched_label for h in hits] == ["热更新"]


def test_latin_word_boundary(env):
    tb = env["governance"].termbase()
    assert env["lexicon"].find_hits("CDN 挂了", tb) == []
    assert [h.term.slug for h in env["lexicon"].find_hits("CD 太长", tb)] == ["skill-cd"]


def test_accessor_variety_rejects_fragment_between_numbers():
    lex = importlib.import_module("concepts.lexicon")
    utts = [{"text": f"技能冷却从 {i} 秒改成 {i + 1} 秒", "person": f"p{i % 3}", "project": "G"}
            for i in range(8)]
    surfaces = {c["surface"] for c in lex.mine_candidates(utts, [], set())}
    assert "秒改成" not in surfaces and "改成" not in surfaces


def test_g2_direction():
    lex = importlib.import_module("concepts.lexicon")
    assert lex.g2(50, 1, 100, 100) > lex.g2(10, 1, 100, 100) > 0
    assert lex.g2(1, 50, 100, 100) == 0.0


# ------------------------------ signals ------------------------------ #
def test_misread_planted_effect_detected(env):
    _upload(env, "alice", "s1", _misread_convo())
    stats = env["pipeline"].rebuild(full=True)
    assert stats["processed"] == 1
    rows = env["views"]._q("SELECT term_slug, evidence, cost_usd_est FROM misread_event")
    assert [(r["term_slug"], r["evidence"]) for r in rows] == [("hot-update", "rementioned")]
    assert rows[0]["cost_usd_est"] and 0 < rows[0]["cost_usd_est"] < 2.0


def test_no_misread_without_correction(env):
    _upload(env, "alice", "s2", [_u("技能冷却要改成 3 秒"), _a("已修改"), _edit(),
                                 _u("好的，再把热更脚本也看一下"), _a("好")])
    env["pipeline"].rebuild(full=True)
    assert env["views"]._q("SELECT * FROM misread_event") == []


def test_correction_unrelated_to_term_needs_edit_and_adjacency(env):
    # 纠正没重提术语、且中间没改文件 → 不归因
    _upload(env, "alice", "s3", [_u("技能冷却改成 3 秒"), _a("我先解释一下原理"),
                                 _u("不对，你理解错了"), _a("抱歉")])
    env["pipeline"].rebuild(full=True)
    assert env["views"]._q("SELECT * FROM misread_event") == []


def test_substitution_planted(env):
    _upload(env, "bob", "s4", [_u("晚上要做一次热更"), _a("好的，我来准备重启服务的脚本。")])
    env["pipeline"].rebuild(full=True)
    rows = env["views"]._q("SELECT from_slug, to_slug FROM substitution_event")
    assert [(r["from_slug"], r["to_slug"]) for r in rows] == [("hot-update", "restart")]


def test_subagent_fidelity(env):
    _upload(env, "bob", "s5", [
        _u("排查热更失败和技能冷却异常"),
        {"role": "assistant", "type": "tool_use", "name": "Task",
         "input": {"prompt": "查一下技能冷却相关代码"}},
    ])
    env["pipeline"].rebuild(full=True)
    p = env["views"].personal("bob")
    assert p["fidelity"]["expected"] == 2 and p["fidelity"]["kept"] == 1


# ------------------------------ consent ------------------------------ #
def test_unconsented_uploads_are_not_analysed(env):
    _upload(env, "alice", "s1", _misread_convo(), consent=False)
    env["pipeline"].rebuild(full=True)
    assert env["views"]._q("SELECT COUNT(*) AS n FROM utterance")[0]["n"] == 0


def test_revoking_consent_purges_derived_data(env):
    _upload(env, "alice", "s1", _misread_convo())
    env["pipeline"].rebuild()
    assert env["views"]._q("SELECT COUNT(*) AS n FROM misread_event")[0]["n"] == 1
    env["profile"].set_consent("alice", False)
    stats = env["pipeline"].rebuild()
    assert stats["purged"] == 1
    for t in ("utterance", "term_usage", "misread_event"):
        assert env["views"]._q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == 0


def test_incremental_skips_unchanged_and_reprocesses_on_lexicon_change(env):
    _upload(env, "alice", "s1", _misread_convo())
    assert env["pipeline"].rebuild()["processed"] == 1
    assert env["pipeline"].rebuild()["skipped"] == 1
    env["governance"].import_termbase(
        "admin", {"terms": [{"slug": "backpack", "pref_label": "背包"}]}, apply=True)
    assert env["pipeline"].rebuild()["processed"] == 1


# ------------------------------ 红线 ------------------------------ #
def test_personal_view_self_only(env):
    api = env["api"]
    _upload(env, "alice", "s1", _misread_convo())
    env["pipeline"].rebuild()
    me = api.handle_get("/api/concepts/me", {}, "alice")
    assert me["ai_misreads"][0]["slug"] == "hot-update"
    with pytest.raises(api.ApiError) as ei:
        api.handle_get("/api/concepts/me", {"as": ["alice"]}, "mgr")
    assert ei.value.status == 403
    # admin 也不能越权看个人视图
    with pytest.raises(api.ApiError):
        api.handle_get("/api/concepts/me", {"as": ["alice"]}, "admin")


def test_grant_allows_view_and_is_logged(env):
    api = env["api"]
    api.handle_post("/api/concepts/grant", {"grantee": "bob"}, "alice")
    d = api.handle_get("/api/concepts/me", {"as": ["alice"]}, "bob")
    assert d["viewing_as_grantee"] is True
    log = env["disclosure"].access_log_for("alice")
    assert log and log[0]["viewer"] == "bob"
    api.handle_post("/api/concepts/grant", {"grantee": "bob", "revoke": True}, "alice")
    with pytest.raises(api.ApiError):
        api.handle_get("/api/concepts/me", {"as": ["alice"]}, "bob")


def test_diagnosis_requires_manager(env):
    with pytest.raises(env["api"].ApiError) as ei:
        env["api"].handle_get("/api/concepts/diagnosis", {}, "alice")
    assert ei.value.status == 403


def test_diagnosis_suppressed_below_five_persons(env):
    for i, u in enumerate(("alice", "bob", "carol")):
        _upload(env, u, f"s{i}", _misread_convo())
    env["pipeline"].rebuild()
    d = env["api"].handle_get("/api/concepts/diagnosis", {}, "mgr")
    assert d["insufficient"] is True
    assert "misread_concepts" not in d


def test_diagnosis_rows_need_five_persons(env):
    users = ("alice", "bob", "carol", "dave", "erin")
    for i, u in enumerate(users):
        _upload(env, u, f"s{i}", _misread_convo())
    _upload(env, "frank", "sx", [_u("技能冷却要改"), _a("好"), _edit(),
                                 _u("不对，技能冷却是 3 秒"), _a("好")])
    env["pipeline"].rebuild()
    d = env["api"].handle_get("/api/concepts/diagnosis", {}, "mgr")
    assert d["insufficient"] is False
    rows = {r["key"]: r for r in d["misread_concepts"]}
    assert rows["hot-update"]["events"] == 5
    assert rows["skill-cd"].get("suppressed") is True and "events" not in rows["skill-cd"]
    # 管理诊断的任何行都不带 person 标识
    for r in d["misread_concepts"]:
        assert not {"person", "persons", "owner", "username"} & set(r)


def test_diagnosis_never_joins_efficiency_metrics():
    """红线 R1：概念模块不读取效率指标列。"""
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (_BACKEND / "concepts").glob("*.py"))
    for col in ("tcer", "score", "cpe", "churn_ratio", "tier"):
        assert f"u.{col}" not in src and f" {col} FROM uploads" not in src


def test_public_evidence_only_from_public_sessions(env):
    _upload(env, "alice", "s1", _misread_convo())
    env["pipeline"].rebuild()
    d = env["views"].concept_detail("hot-update")
    assert d["evidence"] == [] and d["evidence_hidden"] >= 1
    conn = env["db"].connect()
    conn.execute("UPDATE uploads SET visibility='public'")
    conn.commit()
    conn.close()
    env["pipeline"].rebuild()
    d = env["views"].concept_detail("hot-update")
    assert d["evidence"] and all("person" not in e for e in d["evidence"])


# ------------------------------ 治理 ------------------------------ #
def test_proposal_review_flow(env):
    gov, rbac = env["governance"], env["rbac"]
    rbac.set_roles("bob", ["reviewer"])
    pid = gov.propose("alice", {"slug": "buff", "pref_label": "增益", "definition": "临时属性加成"})
    assert gov.get_concept("buff") is None
    with pytest.raises(PermissionError):
        gov.decide("carol", pid, True)
    with pytest.raises(ValueError):
        gov.decide("bob", pid, False, "")          # 驳回必须附理由
    gov.decide("bob", pid, True)
    c = gov.get_concept("buff")
    assert c["version"] == 1 and c["status"] == "active"
    assert gov.history("buff")[0]["changed_by"] == "alice"


def test_reviewer_cannot_approve_own_proposal(env):
    env["rbac"].set_roles("bob", ["reviewer"])
    pid = env["governance"].propose("bob", {"slug": "buff", "pref_label": "增益"})
    with pytest.raises(PermissionError):
        env["governance"].decide("bob", pid, True)


def test_stale_proposal_rejected(env):
    gov = env["governance"]
    env["rbac"].set_roles("bob", ["reviewer"])
    p1 = gov.propose("alice", dict(TERMS[2], definition="v2"))
    p2 = gov.propose("carol", dict(TERMS[2], definition="v3"))
    gov.decide("bob", p1, True)
    assert [p for p in gov.list_proposals("superseded") if p["id"] == p2]


def test_duplicate_open_proposal_rejected(env):
    gov = env["governance"]
    gov.propose("alice", {"slug": "buff", "pref_label": "增益"})
    with pytest.raises(ValueError):
        gov.propose("bob", {"slug": "buff", "pref_label": "增益"})
    gov.propose("bob", {"slug": "buff", "pref_label": "增益", "definition": "另一种写法"})


def test_invalid_entry_rejected(env):
    with pytest.raises(ValueError):
        env["governance"].propose("alice", {"slug": "Bad Slug", "pref_label": "x"})


def test_dispute_requires_reply(env):
    gov = env["governance"]
    env["rbac"].set_roles("bob", ["reviewer"])
    did = gov.raise_dispute("alice", "hot-update", "热更也包括资源包")
    with pytest.raises(ValueError):
        gov.resolve_dispute("bob", did, False, "  ")
    with pytest.raises(PermissionError):
        gov.resolve_dispute("carol", did, False, "no")
    gov.resolve_dispute("bob", did, True, "已采纳，修订中")
    assert gov.list_disputes(status="accepted")[0]["id"] == did


def test_export_roundtrip_matches_client_schema(env):
    from tcer.core import termbase as tb_mod
    data = env["governance"].export_termbase()
    for t in data["terms"]:
        assert tb_mod.validate_entry(t) == []
    assert {t["slug"] for t in data["terms"]} == {"hot-update", "restart", "skill-cd"}


def test_context_markdown_is_directive(env):
    md = env["governance"].context_markdown(["hot-update"])
    assert "**热更**" in md and "不是：重启服务器发新版本" in md
    assert "技能冷却" not in md


# ------------------------------ 候选新词 ------------------------------ #
def test_candidate_mining_and_disclosure(env):
    phrases = ["战令系统的奖励要调整", "这期战令要加皮肤", "把战令入口挪到主界面",
               "战令经验太难拿了", "新战令赛季下周上线", "战令等级上限提高"]
    k = 0
    for i, u in enumerate(("alice", "bob", "carol")):
        for j in range(3):
            _upload(env, u, f"c{k}", [_u(phrases[(i + j) % len(phrases)]), _a("好的我会处理")],
                    project=f"P{j % 2}")
            k += 1
    env["pipeline"].rebuild()
    surfaces = [c["surface"] for c in env["views"].candidates_public()]
    assert "战令" in surfaces
    assert not any(s.startswith("战令") and len(s) > 2 for s in surfaces), surfaces
    # alice、bob 两人用的「菠萝包」达不到公共门槛（≥3 人），只进使用者本人的个人视图
    ctx = ["菠萝包方案先放一放", "那个菠萝包我看过了", "按菠萝包思路改", "菠萝包也要评审",
           "先做菠萝包吧", "菠萝包的数值不对"]
    for j, t in enumerate(ctx):
        _upload(env, "alice" if j % 2 else "bob", f"p{j}", [_u(t), _a("好")])
    env["pipeline"].rebuild()
    pub = {c["surface"] for c in env["views"].candidates_public()}
    assert "菠萝包" not in pub
    mine = {c["surface"] for c in env["views"].personal("alice")["private_jargon"]}
    assert "菠萝包" in mine
    assert "菠萝包" not in {c["surface"] for c in
                          env["views"].personal("carol")["private_jargon"]}


def test_document_ingestion(env):
    env["api"].handle_post("/api/concepts/documents",
                           {"kind": "wiki", "title": "热更规范", "text": "热更流程：只替换脚本。"},
                           "alice")
    env["pipeline"].rebuild()
    d = env["views"].concept_detail("hot-update")
    assert d["stats"]["doc_mentions"] == 1
