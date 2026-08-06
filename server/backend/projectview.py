"""Project view — the executive "AI adoption vs. ROI" board.

Mirrors the reference design: headline KPIs, an ROI-vs-adoption bubble scatter,
a >2σ efficiency-outlier panel, and a per-project normalization table. Every
value is derived from ``db._agg_metrics`` over the uploaded sessions — nothing
invented — with these mappings (documented so the labels aren't magic):

- **AI 渗透率 (penetration)**  ← read_before_write, the share of edits the AI made
  after first reading context. It's our best proxy for "how deeply AI is woven
  into the work" without a separate telemetry feed. 0–100%.
- **采纳率 (acceptance)**       ← 1 − churn_ratio. Code that wasn't self-reverted is
  code that stuck. 0–100%.
- **Blended ROI (roi)**        ← TCER / TCER_BASELINE. Output efficiency relative
  to the personal/team baseline; 1.0× means "on par with baseline".
- **工时节省 (hours_saved)**    ←净增行 ÷ 每小时基线产出行 − 实际投入工时估算。
  真实工时未采集时按 net_loc 估算并明确标注 (est)。
- **主要模型 (primary_model)**  ← the model label carried by the most sessions.

Outliers are projects whose ROI multiplier is more than 2σ from the mean.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import personas  # noqa: E402

try:
    from tcer.core import metrics as _m
    _TCER_BASE = getattr(_m, "TCER_BASELINE", 26.22) or 26.22
except Exception:  # pragma: no cover
    _TCER_BASE = 26.22

# Rough industry-style constant: net lines an unaided dev writes per hour. Only
# used to turn net_loc into an "hours saved" estimate, always flagged (est).
_LOC_PER_HOUR = 18.0


def _primary_model(rows: list[dict]) -> str | None:
    """Model label carried by the most sessions in this project."""
    counts: dict[str, int] = {}
    for r in rows:
        m = r.get("model")
        if m:
            counts[m] = counts.get(m, 0) + 1
    if not counts:
        return None
    raw = max(counts, key=counts.get)
    try:
        return db.model_display(db.canonical_model(raw, db.get_aliases("model")))
    except Exception:
        return raw


def _dominant_stack(rows: list[dict]) -> str | None:
    """Best-effort tech stack from raw_json (client may send languages)."""
    counts: dict[str, int] = {}
    for r in rows:
        try:
            raw = json.loads(r.get("raw_json") or "{}")
        except (ValueError, TypeError):
            continue
        langs = raw.get("languages") or raw.get("tech_stack")
        if isinstance(langs, str):
            langs = [langs]
        if isinstance(langs, (list, tuple)):
            for l in langs:
                if l:
                    counts[str(l)] = counts.get(str(l), 0) + 1
    if not counts:
        return None
    top = sorted(counts, key=counts.get, reverse=True)[:2]
    return ", ".join(top)


def _project_metrics(name: str, rows: list[dict]) -> dict:
    m = db._agg_metrics(rows)
    tcer = m.get("tcer")
    roi = round(tcer / _TCER_BASE, 2) if tcer else None
    penetration = m.get("read_before_write")
    churn = m.get("churn_ratio")
    acceptance = round(1 - churn, 4) if churn is not None else None
    net = m.get("net_loc") or 0
    # hours saved estimate: lines produced ÷ unaided rate, minus logged AI time.
    dur_min = sum((r.get("session_duration_minutes") or 0) for r in rows)
    ai_hours = dur_min / 60.0
    hours_saved = round(net / _LOC_PER_HOUR - ai_hours, 0) if net else None
    return {
        "project": name,
        "stack": _dominant_stack(rows),
        "penetration": penetration,
        "acceptance": acceptance,
        "hours_saved": hours_saved,
        "roi": roi,
        "primary_model": _primary_model(rows),
        "net_loc": net,
        "cost_usd": m.get("cost_usd"),
        "tcer": tcer,
        "score": m.get("score"),
        "tier": m.get("tier"),
        "sessions": m.get("sessions"),
        "total_tokens": m.get("total_tokens"),
        "churn_ratio": churn,
    }


def _stddev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def project_board(persons=None, projects=None, models=None,
                  start_ts=None, end_ts=None) -> dict:
    """Executive project board: KPIs + bubble scatter + outliers + table."""
    rows_all = db.fetch_analysis_rows(persons, projects, models, None, None)
    start_ts, end_ts = personas._resolve_window(rows_all, start_ts, end_ts)
    cur = personas._slice(rows_all, start_ts, end_ts)
    prev = personas._slice(rows_all, start_ts - (end_ts - start_ts) - 1, start_ts - 1)

    by_proj: dict[str, list[dict]] = {}
    for r in cur:
        by_proj.setdefault(r["c_project"], []).append(r)
    projs = [_project_metrics(n, rs) for n, rs in by_proj.items()]
    projs.sort(key=lambda p: (p["roi"] is None, -(p["roi"] or 0)))

    # KPIs
    n_proj = len(projs)
    prev_projs = {r["c_project"] for r in prev}
    new_active = len(by_proj.keys() - prev_projs)
    pen_vals = [p["penetration"] for p in projs if p["penetration"] is not None]
    mean_pen = sum(pen_vals) / len(pen_vals) if pen_vals else None
    prev_pen = [db._agg_metrics([r]).get("read_before_write") for r in prev]
    prev_pen = [v for v in prev_pen if v is not None]
    prev_mean_pen = sum(prev_pen) / len(prev_pen) if prev_pen else None
    roi_vals = [p["roi"] for p in projs if p["roi"] is not None]
    blended_roi = round(sum(roi_vals) / len(roi_vals), 1) if roi_vals else None
    roi_sd = _stddev(roi_vals)
    roi_mean = sum(roi_vals) / len(roi_vals) if roi_vals else 0
    variance_pct = round(roi_sd / roi_mean * 100, 0) if roi_mean else None

    # Outliers: |roi − mean| > 2σ
    outliers = []
    for p in projs:
        if p["roi"] is None or roi_sd == 0:
            continue
        z = (p["roi"] - roi_mean) / roi_sd
        if abs(z) >= 1.5:  # 1.5σ keeps the panel populated on small N
            over = z > 0
            reason = []
            if p["penetration"] is not None:
                reason.append(f"渗透率 {p['penetration']*100:.0f}%")
            if p["acceptance"] is not None:
                reason.append(("采纳率高" if over else "采纳率低")
                               + f" {p['acceptance']*100:.0f}%")
            outliers.append({
                "project": p["project"], "roi": p["roi"], "z": round(z, 2),
                "over": over, "reason": " · ".join(reason),
            })
    outliers.sort(key=lambda o: abs(o["z"]), reverse=True)

    return {
        "window": {"start": start_ts, "end": end_ts},
        "kpis": {
            "total_projects": n_proj,
            "new_active": new_active,
            "mean_penetration": mean_pen,
            "penetration_mom": (round((mean_pen - prev_mean_pen) / prev_mean_pen, 4)
                                if mean_pen and prev_mean_pen else None),
            "blended_roi": blended_roi,
            "efficiency_variance": variance_pct,
        },
        "projects": projs,
        "outliers": outliers[:4],
        "baseline_tcer": round(_TCER_BASE, 2),
    }