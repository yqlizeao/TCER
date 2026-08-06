"""Persona views — decision-maker dashboards over uploaded TCER sessions.

Two audiences, two endpoints, each answering the questions that role actually
asks — not a generic metric dump:

- ``executive`` (CTO / 财务 / 老板): ROI. What did AI coding cost, what did it
  produce, is the trend improving, which projects convert spend into code best.
- ``engineering`` (技术 leader): adoption + delivery quality. Is usage growing,
  is rework/error trending down, who and which project is the weak spot.

Both reuse ``db._fetch_rows`` + ``db._agg_metrics`` so every number agrees with
the rest of the server (ratios are ratio-of-sums, score is recomputed, never a
mean over sessions). Period-over-period compares the selected window against the
immediately preceding window of equal length.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

_DAY = 86400


# --------------------------------------------------------------------------- #
# Window helpers
# --------------------------------------------------------------------------- #
def _resolve_window(rows_all: list[dict],
                    start_ts: int | None,
                    end_ts: int | None) -> tuple[int, int]:
    """Fill in a concrete [start, end] window.

    If the caller gave neither bound we anchor on the data's own max ts and look
    back 30 days, so "environment just seeded" still shows a sensible period
    instead of an empty one.
    """
    if end_ts is None:
        end_ts = max((int(r["ts"]) for r in rows_all if r.get("ts")), default=0)
    if start_ts is None:
        start_ts = end_ts - 30 * _DAY
    return start_ts, end_ts


def _slice(rows: list[dict], start_ts: int, end_ts: int) -> list[dict]:
    return [r for r in rows if r.get("ts") is not None
            and start_ts <= int(r["ts"]) <= end_ts]


def _delta(cur: float | None, prev: float | None) -> dict | None:
    """Relative change cur vs prev, plus the raw pair. None if not comparable."""
    if cur is None or prev is None or prev == 0:
        return {"cur": cur, "prev": prev, "rel": None, "abs": None}
    return {"cur": cur, "prev": prev,
            "rel": round((cur - prev) / abs(prev), 4),
            "abs": round(cur - prev, 4)}


def _daily(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Per-day aggregate of the given metric keys, chronological."""
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("ts") is None:
            continue
        d = (int(r["ts"]) // _DAY) * _DAY
        buckets.setdefault(d, []).append(r)
    out = []
    for d in sorted(buckets):
        m = db._agg_metrics(buckets[d])
        point = {"ts": d}
        for k in keys:
            point[k] = m.get(k)
        out.append(point)
    return out


# --------------------------------------------------------------------------- #
# Executive — ROI
# --------------------------------------------------------------------------- #
def executive(persons=None, projects=None, models=None,
              start_ts=None, end_ts=None) -> dict:
    """ROI dashboard: spend vs. output, trend, and per-project conversion."""
    rows_all = db.fetch_analysis_rows(persons, projects, models, None, None)
    start_ts, end_ts = _resolve_window(rows_all, start_ts, end_ts)
    span = max(end_ts - start_ts, _DAY)
    cur = _slice(rows_all, start_ts, end_ts)
    prev = _slice(rows_all, start_ts - span, start_ts - 1)

    a = db._agg_metrics(cur)
    b = db._agg_metrics(prev)

    people = sorted({r["c_person"] for r in cur})
    proj_names = sorted({r["c_project"] for r in cur})

    # Headline KPIs with period-over-period deltas. Each is a decision number:
    # what we spent, what we got, and the unit economics (cost per 1000 lines).
    kpis = {
        "cost_usd": {"value": a["cost_usd"], **_delta(a["cost_usd"], b["cost_usd"])},
        "net_loc": {"value": a["net_loc"], **_delta(a["net_loc"], b["net_loc"])},
        "cpe": {"value": a["cpe"], **_delta(a["cpe"], b["cpe"])},
        "tcer": {"value": a["tcer"], **_delta(a["tcer"], b["tcer"])},
        "total_tokens": {"value": a["total_tokens"],
                         **_delta(a["total_tokens"], b["total_tokens"])},
        "sessions": {"value": a["sessions"],
                     **_delta(a["sessions"], b["sessions"])},
    }

    # Cache economics: cache reads are billed far below fresh input. We can't
    # split the client's blended cost back out per token, so we report the
    # leverage honestly — hit ratio + read volume — and note savings are already
    # inside cost_usd rather than inventing a dollar figure.
    cache = {
        "chr": a["chr"],
        "cache_read_tokens": a["cache_read_tokens"],
        "cache_write_tokens": a["cache_write_tokens"],
        "input_tokens": a["input_tokens"],
    }

    # Per-project ROI ranking: who converts spend into code, and at what tier.
    by_project: dict[str, list[dict]] = {}
    for r in cur:
        by_project.setdefault(r["c_project"], []).append(r)
    projects_roi = []
    for name, grp in by_project.items():
        m = db._agg_metrics(grp)
        projects_roi.append({
            "project": name,
            "cost_usd": m["cost_usd"], "net_loc": m["net_loc"],
            "cpe": m["cpe"], "tcer": m["tcer"],
            "score": m["score"], "tier": m["tier"],
            "sessions": m["sessions"],
            "cost_share": round(m["cost_usd"] / a["cost_usd"], 4)
            if a["cost_usd"] else None,
        })
    projects_roi.sort(key=lambda x: (x["cost_usd"] or 0), reverse=True)

    trend = _daily(cur, ("cost_usd", "net_loc", "cpe"))

    return {
        "window": {"start": start_ts, "end": end_ts, "span_days": span // _DAY},
        "kpis": kpis,
        "cache": cache,
        "projects": projects_roi,
        "trend": trend,
        "coverage": {"people": len(people), "projects": len(proj_names),
                     "sessions": a["sessions"]},
    }


# --------------------------------------------------------------------------- #
# Engineering — adoption + delivery quality
# --------------------------------------------------------------------------- #
# A ratio counts as a "weak spot" only with enough sessions behind it — a single
# bad session shouldn't brand a person or project.
_MIN_SESSIONS_FOR_FLAG = 5


def engineering(persons=None, projects=None, models=None,
                start_ts=None, end_ts=None) -> dict:
    """Adoption + quality dashboard: trends, people matrix, project health."""
    rows_all = db.fetch_analysis_rows(persons, projects, models, None, None)
    start_ts, end_ts = _resolve_window(rows_all, start_ts, end_ts)
    span = max(end_ts - start_ts, _DAY)
    cur = _slice(rows_all, start_ts, end_ts)
    prev = _slice(rows_all, start_ts - span, start_ts - 1)

    a = db._agg_metrics(cur)
    b = db._agg_metrics(prev)

    # Adoption + quality headline, each with period-over-period movement.
    kpis = {
        "sessions": {"value": a["sessions"],
                     **_delta(a["sessions"], b["sessions"])},
        "active_people": {"value": len({r["c_person"] for r in cur}),
                          **_delta(len({r["c_person"] for r in cur}),
                                   len({r["c_person"] for r in prev}))},
        "score": {"value": a["score"], **_delta(a["score"], b["score"])},
        "churn_ratio": {"value": a["churn_ratio"],
                        **_delta(a["churn_ratio"], b["churn_ratio"])},
        "tool_error_rate": {"value": a["tool_error_rate"],
                            **_delta(a["tool_error_rate"], b["tool_error_rate"])},
        "read_before_write": {"value": a["read_before_write"],
                              **_delta(a["read_before_write"],
                                       b["read_before_write"])},
    }

    # Quality trend: rework + error over time is the leader's "is it getting
    # better" line. Adoption trend: sessions/day.
    quality_trend = _daily(cur, ("churn_ratio", "tool_error_rate", "score"))
    adoption_trend = _daily(cur, ("sessions", "net_loc"))

    people = _matrix(cur, "c_person", "person")
    projects_health = _matrix(cur, "c_project", "project")

    # Model dimension: which model produces more, at what unit cost. Uses the
    # same _matrix rollup, then attaches a friendly display label. Sorted by
    # TCER desc so the most productive model reads first.
    models_out = _matrix(cur, "c_model", "model")
    for m in models_out:
        try:
            m["display"] = db.model_display(m["model"])
        except Exception:
            m["display"] = m["model"]
    models_out = [m for m in models_out if (m["sessions"] or 0) > 0]
    models_out.sort(key=lambda m: (m["tcer"] is None, -(m["tcer"] or 0)))

    return {
        "window": {"start": start_ts, "end": end_ts, "span_days": span // _DAY},
        "kpis": kpis,
        "quality_trend": quality_trend,
        "adoption_trend": adoption_trend,
        "people": people,
        "projects": projects_health,
        "models": models_out,
        "weak_spots": _weak_spots(people, projects_health, a),
    }


def _matrix(rows: list[dict], key: str, label_field: str) -> list[dict]:
    """Per-entity effectiveness row, sorted worst-score first for triage."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get(key) or "未标注", []).append(r)
    out = []
    for name, grp in groups.items():
        m = db._agg_metrics(grp)
        out.append({
            label_field: name,
            "sessions": m["sessions"],
            "score": m["score"], "tier": m["tier"],
            "tcer": m["tcer"], "cpe": m["cpe"],
            "net_loc": m["net_loc"], "cost_usd": m["cost_usd"],
            "churn_ratio": m["churn_ratio"],
            "tool_error_rate": m["tool_error_rate"],
            "read_before_write": m["read_before_write"],
        })
    out.sort(key=lambda x: (x["score"] is None, x["score"] or 0))
    return out


def _weak_spots(people: list[dict], projects: list[dict], team: dict) -> list[dict]:
    """Flag the entities dragging team quality — grounded, thresholded, no L LLM.

    Only entities with enough sessions and a genuinely worse-than-team ratio are
    flagged, so this is a triage list a leader can act on, not noise.
    """
    flags = []
    t_churn = team.get("churn_ratio")
    t_err = team.get("tool_error_rate")

    def worst(rows, field, team_val, higher_bad=True, kind="", label_field=""):
        cand = [r for r in rows
                if (r["sessions"] or 0) >= _MIN_SESSIONS_FOR_FLAG
                and r.get(field) is not None]
        if not cand or team_val is None:
            return None
        w = max(cand, key=lambda r: r[field]) if higher_bad \
            else min(cand, key=lambda r: r[field])
        v = w[field]
        # Must be meaningfully worse than the team (20% relative), else skip.
        if higher_bad and v <= team_val * 1.2:
            return None
        if not higher_bad and v >= team_val * 0.8:
            return None
        return {"kind": kind, "subject": w[label_field], "field": field,
                "value": v, "team": team_val, "sessions": w["sessions"]}

    f = worst(people, "churn_ratio", t_churn, True, "person_churn", "person")
    if f:
        flags.append(f)
    f = worst(people, "tool_error_rate", t_err, True, "person_error", "person")
    if f:
        flags.append(f)
    f = worst(projects, "churn_ratio", t_churn, True, "project_churn", "project")
    if f:
        flags.append(f)
    f = worst(projects, "tool_error_rate", t_err, True, "project_error", "project")
    if f:
        flags.append(f)
    return flags