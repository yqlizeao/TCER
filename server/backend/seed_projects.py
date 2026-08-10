"""Seed the DB with a project-portfolio dataset for the 项目总览 board.

Unlike ``seed_mock.py`` (calibrated for the Decision Lab's planted ground
truth), this produces the *executive project board* look: a spread of projects
with dispersed AI penetration and ROI, real tech stacks, and deliberate over- /
under-performers so the outlier panel and the red under-performing bubble both
populate — matching the reference design.

    python server/backend/seed_projects.py

Wipes existing upload rows first (accounts/aliases kept).
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import db  # noqa: E402
from tcer.core import metrics  # noqa: E402

random.seed(7)

# (project, stack, penetration, roi_target, acceptance, model, source, is_bad)
# penetration = read_before_write proxy; roi_target drives TCER = base × roi.
PORTFOLIO = [
    ("PRJ-Astra",  ["React", "Node"],       0.852, 4.2, 0.724, "claude-opus-4-8",  "claude", False),
    ("PLT-Data",   ["Python", "Spark"],     0.920, 4.8, 0.811, "claude-sonnet-5",  "claude", False),
    ("PRJ-Zephyr", ["Go", "Kubernetes"],    0.658, 2.9, 0.540, "claude-opus-4-8",  "claude", False),
    ("SVC-Auth",   ["Rust", "gRPC"],        0.780, 3.4, 0.690, "claude-sonnet-5",  "codex",  False),
    ("APP-Mobile", ["Swift", "Kotlin"],     0.712, 2.6, 0.605, "gpt-4o",           "codex",  False),
    ("PRJ-Legacy", ["Java", "Spring"],      0.801, 0.8, 0.320, "gpt-3.5-turbo",    "grok",   True),
]

_BASE = getattr(metrics, "TCER_BASELINE", 26.22) or 26.22
_CPE_BASE = getattr(metrics, "CPE_BASELINE", 13.62) or 13.62


def _rng(lo, hi):
    return random.uniform(lo, hi)


def _demo_conversation(proj, stack) -> list[dict]:
    """A representative turn-by-turn transcript in the normalized block shape the
    session-detail view renders: user / assistant text / thinking / tool_call /
    tool_result blocks. Purely illustrative demo content."""
    lang = (stack or ["代码"])[0]
    return [
        {"role": "user", "blocks": [
            {"type": "text", "text": f"帮我在 {proj} 里加一个 {lang} 的分页接口，带单元测试。"}]},
        {"role": "assistant", "blocks": [
            {"type": "thinking", "text": "先看现有路由和数据层约定，再决定分页参数风格（offset/limit vs cursor）。"},
            {"type": "text", "text": "好的，我先读一下现有的路由结构。"},
            {"type": "tool_call", "name": "Read", "args": {"path": "src/routes/items.py"}},
        ]},
        {"role": "tool", "blocks": [
            {"type": "tool_result", "name": "Read", "ok": True,
             "text": "def list_items():\n    return db.all()  # 无分页"}]},
        {"role": "assistant", "blocks": [
            {"type": "text", "text": "现有接口没有分页。我按 limit/offset 加，并保持返回结构兼容。"},
            {"type": "tool_call", "name": "Edit", "args": {"path": "src/routes/items.py",
             "summary": "+ limit/offset 分页 + 总数返回"}},
        ]},
        {"role": "tool", "blocks": [
            {"type": "tool_result", "name": "Edit", "ok": True, "text": "已写入 18 行"}]},
        {"role": "assistant", "blocks": [
            {"type": "tool_call", "name": "Write", "args": {"path": "tests/test_items_paging.py",
             "summary": "新增分页单测 3 例"}},
        ]},
        {"role": "tool", "blocks": [
            {"type": "tool_result", "name": "Write", "ok": True, "text": "已创建，24 行"}]},
        {"role": "assistant", "blocks": [
            {"type": "tool_call", "name": "Bash", "args": {"cmd": "pytest tests/test_items_paging.py -q"}},
        ]},
        {"role": "tool", "blocks": [
            {"type": "tool_result", "name": "Bash", "ok": True, "text": "3 passed in 0.4s"}]},
        {"role": "assistant", "blocks": [
            {"type": "text", "text": "完成：分页接口 + 3 个单测全绿。返回体新增 total 字段，旧调用方不受影响。"}]},
    ]


def main() -> int:
    db.init_db()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='uploads'")
        conn.commit()
    finally:
        conn.close()

    now = int(time.time())
    total = 0
    for (proj, stack, pen, roi, acc, model_id, source, is_bad) in PORTFOLIO:
        n_sessions = random.randint(14, 26)
        for _ in range(n_sessions):
            days_ago = random.randint(0, 29)
            started_ms = (now - days_ago * 86400 - random.randint(0, 80000)) * 1000

            # TCER dispersed around the project's ROI target.
            tcer = max(1.0, _BASE * roi * _rng(0.85, 1.15))
            # cost efficiency: bad project also burns more per line.
            cpe = _CPE_BASE * (1.9 if is_bad else _rng(0.6, 1.1))
            churn = (1 - acc) * _rng(0.9, 1.1)
            churn = round(min(max(churn, 0.02), 0.9), 4)
            rbw = round(min(max(pen * _rng(0.95, 1.05), 0.05), 0.99), 4)
            err_rate = round(_rng(0.12, 0.22) if is_bad else _rng(0.0, 0.08), 4)

            net_loc = random.randint(300, 2600)
            total_tokens = int(net_loc / (tcer / 1_000_000))
            inp = int(total_tokens * _rng(0.05, 0.12))
            cache_read = int(total_tokens * _rng(0.55, 0.75))
            cache_write = int(total_tokens * _rng(0.08, 0.18))
            out = max(0, total_tokens - inp - cache_read - cache_write)
            cost = cpe * (net_loc / 1000.0)
            n_tools = random.randint(10, 90)

            score = metrics.efficiency_score(
                tcer, cpe, churn, err_rate, rbw, net_loc=net_loc)
            session = {
                "session_id": f"{proj}-{days_ago}-{random.randint(1000,9999)}",
                "title": f"{proj} 会话",
                "tcer": round(tcer, 2),
                "score": score,
                "tier": metrics.tier(score),
                "cost_usd": round(cost, 4),
                "cpe": round(cpe, 4),
                "net_loc": net_loc,
                "code_added": net_loc + random.randint(0, 400),
                "total_tokens": total_tokens,
                "input_tokens": inp,
                "output_tokens": out,
                "cache_write_tokens": cache_write,
                "cache_read_tokens": cache_read,
                "churn_ratio": churn,
                "chr": round(cache_read / max(inp + cache_read + cache_write, 1), 4),
                "read_before_write": rbw,
                "search_edit_ratio": round(_rng(0.2, 0.8), 4),
                "tool_error_rate": err_rate,
                "tool_error_count": int(round(err_rate * n_tools)),
                "tool_call_count": n_tools,
                "started_at": started_ms,
                "models_label": model_id,
                "models": [model_id],
                "languages": stack,
                "source": source,
                "task_type": random.choice(["code_creation", "code_maintenance"]),
                "reasoning_effort": random.choice(["medium", "high"]),
                "permission_profile": "default",
                "cli_version": "2.0.0",
                "assistant_turns": random.randint(5, 60),
                "user_msgs": random.randint(1, 20),
                "session_duration_minutes": round(_rng(20, 180), 1),
                "high_churn_file_count": random.randint(0, 4),
                "tool_calls": {"Read": random.randint(3, 30), "Edit": random.randint(2, 20)},
                "tool_variants": {},
                "conversation": _demo_conversation(proj, stack),
            }
            db.insert_records(
                uploaded_by="seed", person=source, project=proj,
                aggregate=None, sessions=[session], generated_at=now,
            )
            total += 1

    print(f"seeded {total} project-portfolio rows across {len(PORTFOLIO)} projects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())