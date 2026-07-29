"""Seed the TCER web DB with mock data for acceptance testing.

Generates sessions across several people / projects / models over the past
~30 days so every filter and all three dimension charts have something to show.
Idempotent-ish: wipes existing rows first so re-running gives a clean set.

    python web/backend/seed_mock.py

The data has a **planted ground truth** so the Decision Lab can be checked
against a known answer rather than eyeballed:

- ``task_type`` is the dominant driver of TCER (creation ≫ maintenance ≫
  non-coding) **and** is unevenly distributed across models — Sonnet draws far
  more creation work. A naive per-model comparison therefore ranks Sonnet first
  for the wrong reason; a correct stratified comparison should shrink or remove
  that gap. This is the confounder the whole engine exists to defeat.
- ``reasoning_effort=high`` costs ~45% more with no TCER gain → the engine
  should emit a "更贵但没换来产出" finding.
- Skill ``dataviz`` genuinely helps; skill ``legacy-helper`` and MCP ``zread``
  don't but burn extra tokens → "未见收益" findings.
- Large sessions rework more → the small-batch workflow finding.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

PEOPLE = ["joey", "alice", "bob"]
PROJECTS = ["TCER", "WebApp", "DataPipe"]
MODELS = [
    ("Opus 4.8", "claude-opus-4-8"),
    ("Opus 4.8", "claude-opus-4.8"),   # variant id — must auto-merge with above
    ("Sonnet 5", "claude-sonnet-5"),
    ("GLM-4.6", "glm-4-6"),
]

# Per-model rough efficiency profile so charts show visible separation.
MODEL_PROFILE = {
    "Opus 4.8": dict(tcer=(900, 1500), ctei=(0.7, 0.95), chr=(0.85, 0.96)),
    "Sonnet 5": dict(tcer=(1100, 1800), ctei=(0.6, 0.85), chr=(0.80, 0.93)),
    "GLM-4.6": dict(tcer=(700, 1200), ctei=(0.4, 0.7), chr=(0.6, 0.85)),
}

SOURCES = ["claude", "codex", "grok"]
TASK_TYPES = ["code_creation", "code_maintenance", "non_coding"]
# TCER multiplier by task type — the confounder. Creation produces far more net
# LOC per token than debugging does, regardless of which model is driving.
TASK_TCER_FACTOR = {"code_creation": 1.0, "code_maintenance": 0.45, "non_coding": 0.15}
# Task mix per model. Sonnet is over-assigned creation work, so an unstratified
# ranking flatters it; that's exactly the bias the Decision Lab must remove.
TASK_MIX = {
    "Opus 4.8": [0.35, 0.45, 0.20],
    "Sonnet 5": [0.75, 0.20, 0.05],
    "GLM-4.6": [0.35, 0.45, 0.20],
}


def _rng(lo, hi):
    return round(random.uniform(lo, hi), 4)


def _pick(options, weights):
    return random.choices(options, weights=weights, k=1)[0]


def main() -> int:
    db.init_db()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM uploads")
        conn.commit()
    finally:
        conn.close()

    random.seed(42)
    now = int(time.time())
    day = 86400
    total = 0

    for person in PEOPLE:
        for project in PROJECTS:
            # not every person touches every project
            if random.random() < 0.25:
                continue
            n_days = random.randint(8, 20)
            for _ in range(n_days):
                days_ago = random.randint(0, 29)
                started_ms = (now - days_ago * day - random.randint(0, day)) * 1000
                label, model_id = random.choice(MODELS)
                prof = MODEL_PROFILE[label]
                task_type = _pick(TASK_TYPES, TASK_MIX[label])
                effort = _pick(["medium", "high"], [0.6, 0.4])
                source = random.choice(SOURCES)
                # Add-ons: dataviz genuinely lifts TCER; legacy-helper and the
                # zread MCP only add token overhead.
                skills = []
                if random.random() < 0.35:
                    skills.append("dataviz")
                if random.random() < 0.30:
                    skills.append("legacy-helper")
                mcps = ["zread"] if random.random() < 0.35 else []

                ctei = _rng(*prof["ctei"])
                chr_ = _rng(*prof["chr"])
                net_loc = random.randint(50, 900)
                total_tokens = random.randint(20_000, 400_000)
                # Overhead add-ons burn extra tokens for the same output, which
                # is exactly how a useless plugin shows up in real data: TCER
                # (net LOC per million tokens) falls because the denominator grew.
                overhead = 1.0
                if "legacy-helper" in skills:
                    overhead *= _rng(1.3, 1.7)
                if mcps:
                    overhead *= _rng(1.2, 1.5)
                total_tokens = int(total_tokens * overhead)
                tcer = _rng(*prof["tcer"]) * TASK_TCER_FACTOR[task_type] / overhead
                if "dataviz" in skills:
                    tcer *= _rng(1.25, 1.55)
                tcer = round(tcer, 4)
                # Split total into a plausible in / out / cache breakdown so the
                # dashboard KPI card (输入/输出/缓存创建/缓存命中) has real numbers.
                cache_read = int(total_tokens * _rng(0.55, 0.75))
                cache_write = int(total_tokens * _rng(0.08, 0.18))
                inp = int(total_tokens * _rng(0.04, 0.10))
                out = max(0, total_tokens - cache_read - cache_write - inp)
                # Bigger sessions rework more — the small-batch signal.
                size_pressure = min(1.0, total_tokens / 400_000)
                churn = _rng(0.02, 0.15) + 0.22 * size_pressure
                # Cost tracks tokens (as it does in reality) rather than being
                # independent noise; high effort adds ~45% on top and buys
                # nothing measurable.
                cost = (total_tokens / 1e6) * _rng(6.0, 11.0)
                cost *= 1.45 if effort == "high" else 1.0
                tool_calls = {"Read": random.randint(3, 40),
                              "Edit": random.randint(1, 25),
                              "Bash": random.randint(0, 15)}
                for srv in mcps:
                    tool_calls[f"mcp__{srv}__search_doc"] = random.randint(1, 6)
                tool_variants = {f"Skill:{s}": random.randint(1, 3) for s in skills}
                n_tools = sum(tool_calls.values())
                err_rate = _rng(0.0, 0.12)
                session = {
                    "session_id": f"{person}-{project}-{days_ago}-{random.randint(1000,9999)}",
                    "title": f"{project} · {label} 会话",
                    "tcer": tcer,
                    "ctei": ctei,
                    "cost_usd": round(cost, 4),
                    "cpe": round(cost / (net_loc / 1000), 4),
                    "net_loc": net_loc,
                    "code_added": net_loc + random.randint(0, 400),
                    "total_tokens": total_tokens,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_write_tokens": cache_write,
                    "cache_read_tokens": cache_read,
                    "churn_ratio": round(min(churn, 0.9), 4),
                    "chr": chr_,
                    "read_before_write": _rng(0.4, 0.95),
                    "search_edit_ratio": _rng(0.2, 0.8),
                    "tool_error_rate": err_rate,
                    "tool_error_count": int(round(err_rate * n_tools)),
                    "started_at": started_ms,
                    "models_label": label,
                    "models": [model_id],
                    # --- configuration dimensions (Decision Lab) ---
                    "source": source,
                    "task_type": task_type,
                    "reasoning_effort": effort,
                    "permission_profile": _pick(["default", "acceptEdits"], [0.7, 0.3]),
                    "cli_version": "2.0.0",
                    "assistant_turns": random.randint(5, 60),
                    "user_msgs": random.randint(1, 20),
                    "session_duration_minutes": round(_rng(3, 120), 1),
                    "high_churn_file_count": random.randint(0, 4),
                    "tool_calls": tool_calls,
                    "tool_variants": tool_variants,
                }
                db.insert_records(
                    uploaded_by="seed", person=person, project=project,
                    aggregate=None, sessions=[session], generated_at=now,
                )
                total += 1

    print(f"seeded {total} session rows")
    vals = db.distinct_values()
    print("persons :", vals["persons"])
    print("projects:", vals["projects"])
    print("models  :", vals["models"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())