"""团队概念对齐造数脚本（开发 / 验收用）。

植入已知效应（与 tests/test_server_concepts.py 同一口径）：
- 「热更」被 AI 反复理解成「重启服务」→ 误解事件 + AI 换词，集中在项目 Arena；
- 「技能冷却」偶发误解；
- 「战令」是多人多项目在用、尚未入库的候选新词；
- 策划 / 程序两个职能在同一项目共用「热更」。

用法：
    TCER_SERVER_DB=/tmp/demo.db python server/backend/seed_concepts.py
随后启动 server，用 admin/admin（管理者 + 全部角色）或 dev1/dev1 登录查看。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
from concepts import governance, pipeline, profile, rbac  # noqa: E402

TERMS = [
    {"slug": "hot-update", "pref_label": "热更", "term_en": "Hot Update", "alt_labels": ["热更新"],
     "definition": "不停服替换 Lua 脚本与配置表，不含客户端二进制与服务器进程重启",
     "owner": "dev1",
     "renderings": {"eng": "脚本与配置的在线替换", "designer": "不用重新下载包就能改数值"},
     "misconceptions": [{"role": "eng", "wrong": "重启服务器发布新版本", "actual": "不停服"},
                        {"role": "designer", "wrong": "所有改动都能热更", "actual": "二进制改动必须发版"}]},
    {"slug": "restart", "pref_label": "重启服务", "definition": "停服并重启服务器进程"},
    {"slug": "skill-cd", "pref_label": "技能冷却", "alt_labels": ["CD"],
     "definition": "技能释放后到可再次释放的等待时间", "owner": "plan1",
     "misconceptions": [{"role": "eng", "wrong": "全局公共冷却", "actual": "单技能独立计时"}]},
    {"slug": "drop-table", "pref_label": "掉落表", "definition": "怪物 / 宝箱掉落物品及概率的配置表"},
    {"slug": "matchmaking", "pref_label": "匹配", "term_en": "Matchmaking",
     "definition": "按段位与延迟为玩家组队开局的服务"},
]

MEMBERS = [("dev1", "eng"), ("dev2", "eng"), ("dev3", "eng"), ("plan1", "designer"),
           ("plan2", "designer"), ("plan3", "designer"), ("qa1", "qa")]


def _u(t, ts):
    return {"role": "user", "type": "text", "text": t, "ts": ts * 1000}


def _a(t):
    return {"role": "assistant", "type": "text", "text": t}


def _edit():
    return {"role": "assistant", "type": "tool_use", "name": "Edit", "input": {"file_path": "x.lua"}}


def _task(p):
    return {"role": "assistant", "type": "tool_use", "name": "Task", "input": {"prompt": p}}


SCENES = [
    ("Arena", lambda t: [_u("今晚要对背包系统做一次热更", t), _a("好的，我来准备重启服务的脚本并通知玩家停服。"), _edit(),
                         _u("不对，热更不需要重启服务，只替换脚本", t + 60), _a("明白，改为只替换 Lua 脚本。")]),
    ("Arena", lambda t: [_u("掉落表里金币概率调低一点，然后热更上去", t), _a("已修改掉落表，热更脚本已生成。"),
                         _u("战令奖励也顺便检查一下", t + 90), _a("已检查。")]),
    ("Arena", lambda t: [_u("技能冷却从 5 秒改成 3 秒", t), _a("已把全局公共冷却改成 3 秒。"), _edit(),
                         _u("错了，技能冷却是单个技能的，不是全局", t + 50), _a("已改为单技能计时。")]),
    ("Raid", lambda t: [_u("匹配服务延迟太高，排查一下，再看看热更流程有没有影响", t),
                        _task("排查匹配服务的延迟问题"), _a("匹配服务延迟来自数据库查询。")]),
    ("Raid", lambda t: [_u("战令第二期的经验曲线要调整", t), _a("好的，已调整经验曲线。"),
                        _u("战令入口放到主界面", t + 40), _a("已调整。")]),
    ("Raid", lambda t: [_u("这次改动能热更吗", t), _a("可以通过重启服务完成更新。"), _u("不对，热更就是不停服", t + 30), _a("抱歉，已修正。")]),
]


def main() -> None:
    db.init_db()
    if db.user_count() == 0 or not db.verify_user("admin", "admin"):
        db.create_user("admin", "admin")
    for u, _ in MEMBERS:
        db.create_user(u, u)
    db.init_db()
    rbac.set_roles("admin", ["admin"])
    rbac.set_roles("dev1", ["concept_owner", "reviewer"])
    rbac.set_roles("plan1", ["concept_owner", "reviewer", "manager"])
    governance.import_termbase("admin", {"version": 1, "terms": TERMS}, apply=True)
    rng = random.Random(7)
    import time as _time
    base = int(_time.time()) - 60 * 86400
    k = 0
    for u, fn in MEMBERS:
        profile.upsert(u, username=u, function=fn, source="imported")
        sessions_by_project: dict[str, list] = {}
        for i in range(8):
            proj, scene = SCENES[rng.randrange(len(SCENES))]
            t = base + rng.randrange(0, 60 * 86400)
            sessions_by_project.setdefault(proj, []).append({
                "session_id": f"{u}-{i}", "source": "claude", "cost_usd": round(rng.uniform(0.5, 4), 2),
                "total_tokens": rng.randrange(80_000, 900_000), "started_at": t * 1000,
                "title": f"{proj} 会话 {i}", "conversation": scene(t)})
            k += 1
        for proj, sess in sessions_by_project.items():
            db.insert_records(uploaded_by=u, person=u, project=proj, aggregate=None, sessions=sess,
                              generated_at=base, semantic_consent=True)
    conn = db.connect()
    conn.execute("UPDATE uploads SET visibility='public' WHERE id % 3 = 0")
    conn.commit()
    conn.close()
    governance.propose("plan2", {"slug": "battle-pass", "pref_label": "战令",
                                 "definition": "赛季付费成长奖励线"}, reason="多人多项目在用")
    governance.raise_dispute("plan3", "hot-update", "热更也应该包括美术资源包的替换")
    stats = pipeline.rebuild(full=True)
    print(f"seeded {k} sessions · {stats}")


if __name__ == "__main__":
    main()
