"""Seed the TCER web DB with mock data for acceptance testing.

Generates sessions across several people / projects / models over the past
~30 days so every filter and all three dimension charts have something to show.
Idempotent-ish: wipes existing rows first so re-running gives a clean set.

    python web/backend/seed_mock.py
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
    ("Sonnet 5", "claude-sonnet-5"),
    ("GLM-4.6", "glm-4-6"),
]

# Per-model rough efficiency profile so charts show visible separation.
MODEL_PROFILE = {
    "Opus 4.8": dict(tcer=(900, 1500), ctei=(0.7, 0.95), chr=(0.85, 0.96)),
    "Sonnet 5": dict(tcer=(1100, 1800), ctei=(0.6, 0.85), chr=(0.80, 0.93)),
    "GLM-4.6": dict(tcer=(700, 1200), ctei=(0.4, 0.7), chr=(0.6, 0.85)),
}


def _rng(lo, hi):
    return round(random.uniform(lo, hi), 4)


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
                tcer = _rng(*prof["tcer"])
                ctei = _rng(*prof["ctei"])
                chr_ = _rng(*prof["chr"])
                net_loc = random.randint(50, 900)
                session = {
                    "session_id": f"{person}-{project}-{days_ago}-{random.randint(1000,9999)}",
                    "tcer": tcer,
                    "ctei": ctei,
                    "cost_usd": _rng(0.2, 6.0),
                    "net_loc": net_loc,
                    "total_tokens": random.randint(20_000, 400_000),
                    "churn_ratio": _rng(0.0, 0.35),
                    "chr": chr_,
                    "read_before_write": _rng(0.4, 0.95),
                    "search_edit_ratio": _rng(0.2, 0.8),
                    "tool_error_rate": _rng(0.0, 0.12),
                    "started_at": started_ms,
                    "models_label": label,
                    "models": [model_id],
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