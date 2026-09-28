"""S3 信号层：零模型的人↔AI / AI↔AI 沟通损耗信号（文档 §5.4）。

全部是纯函数，输入一个会话的 ``ingest.Item`` 序列 + 每条的术语命中 slug 集合，
输出事件字典；落库在 ``pipeline``。

判定口径（精确率优先，宁缺毋滥）：

- **误解事件**：用户在第 t 轮用了术语 X，之后 1–3 个用户轮次内出现纠正消息 v
  （``parse_util.is_correction``，与客户端 SSOT 同一张正则），且满足其一：
  ① v 里再次提到 X（rementioned，强证据）；
  ② v 紧接下一轮（t+1）且中间 AI 改过文件（edited，弱证据，单独标注）。
  一条纠正只归因到它之前最近一次提到该术语的用法，不重复计。
- **AI 换词**：用户在第 t 轮用 X，本轮 AI 回复全文**一次也没沿用** X 的任何叫法，
  却提到了本轮用户没说过的另一术语 Y。
- **AI↔AI 保真**：子代理派发 prompt 与前两轮用户消息的术语交集。
"""
from __future__ import annotations

from tcer.core.parse_util import is_correction

MISREAD_WINDOW = 3


def _first_user_items(items: list, hits: dict[int, set[str]]) -> dict[int, list[int]]:
    """user_turn → 该轮 user item 下标列表。"""
    out: dict[int, list[int]] = {}
    for i, it in enumerate(items):
        if it.kind == "user":
            out.setdefault(it.user_turn, []).append(i)
    return out


def misread_events(items: list, hits: dict[int, set[str]], *,
                   session_cost: float | None = None,
                   session_tokens: int | None = None) -> list[dict]:
    """返回 ``[{slug, usage_idx, correction_idx, user_turns_between, ai_blocks_between,
    cost_usd_est, tokens_est, evidence}]``（idx 为 items 下标）。"""
    user_by_turn = _first_user_items(items, hits)
    ai_total = sum(1 for it in items if it.kind in ("assistant", "edit", "subagent_prompt"))
    events: list[dict] = []
    for vi, v in enumerate(items):
        if v.kind != "user" or not is_correction(v.text):
            continue
        # 每轮只看首条用户消息作为纠正（同轮后续是补充说明）。
        if user_by_turn.get(v.user_turn, [None])[0] != vi:
            continue
        v_slugs = hits.get(vi, set())
        claimed: set[str] = set()
        for back in range(1, MISREAD_WINDOW + 1):
            t = v.user_turn - back
            if t < 0:
                break
            for ui in reversed(user_by_turn.get(t, [])):
                for slug in sorted(hits.get(ui, set())):
                    if slug in claimed:
                        continue
                    between = items[ui + 1:vi]
                    edited = any(it.kind == "edit" for it in between)
                    if slug in v_slugs:
                        evidence = "rementioned"
                    elif back == 1 and edited:
                        evidence = "edited"
                    else:
                        continue
                    claimed.add(slug)
                    ai_between = sum(1 for it in between
                                     if it.kind in ("assistant", "edit", "subagent_prompt"))
                    share = (ai_between / ai_total) if ai_total else 0.0
                    events.append({
                        "slug": slug, "usage_idx": ui, "correction_idx": vi,
                        "user_turns_between": back, "ai_blocks_between": ai_between,
                        "cost_usd_est": round(session_cost * share, 6) if session_cost else None,
                        "tokens_est": int(session_tokens * share) if session_tokens else None,
                        "evidence": evidence,
                    })
    return events


def substitution_events(items: list, hits: dict[int, set[str]]) -> list[dict]:
    """``[{from_slug, to_slug, user_idx, ai_idx}]``。"""
    turns: dict[int, dict] = {}
    for i, it in enumerate(items):
        if it.user_turn < 0:
            continue
        d = turns.setdefault(it.user_turn, {"user": [], "ai": []})
        if it.kind == "user":
            d["user"].append(i)
        elif it.kind == "assistant":
            d["ai"].append(i)
    out = []
    for t, d in turns.items():
        if not d["user"] or not d["ai"]:
            continue
        user_slugs: set[str] = set()
        for ui in d["user"]:
            user_slugs |= hits.get(ui, set())
        if not user_slugs:
            continue
        ai_slugs: set[str] = set()
        for ai in d["ai"]:
            ai_slugs |= hits.get(ai, set())
        dropped = user_slugs - ai_slugs
        introduced = ai_slugs - user_slugs
        if not dropped or not introduced:
            continue
        # 只在「用户只提了一个术语」时才成对（多对多时无法判断谁替换了谁）。
        if len(dropped) != 1 or len(introduced) != 1:
            continue
        frm, to = next(iter(dropped)), next(iter(introduced))
        ai_idx = next(ai for ai in d["ai"] if to in hits.get(ai, set()))
        out.append({"from_slug": frm, "to_slug": to, "user_idx": d["user"][0], "ai_idx": ai_idx})
    return out


def fidelity_events(items: list, hits: dict[int, set[str]]) -> list[dict]:
    """``[{idx, expected:[slug], kept:[slug]}]``；只记录有期望术语的派发。"""
    out = []
    for i, it in enumerate(items):
        if it.kind != "subagent_prompt":
            continue
        expected: set[str] = set()
        for j in range(0, i):
            u = items[j]
            if u.kind == "user" and it.user_turn - 1 <= u.user_turn <= it.user_turn:
                expected |= hits.get(j, set())
        if not expected:
            continue
        kept = expected & hits.get(i, set())
        out.append({"idx": i, "expected": sorted(expected), "kept": sorted(kept)})
    return out
