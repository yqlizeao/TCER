"""Pure value formatters shared by the GUI and the export layer.

No Tkinter dependency — safe to import from anywhere. ``fmt_dt`` unifies what
was previously two separate timestamp formatters (``report.fmt_ms`` and the
GUI's private ``_fmt_dt``).
"""
from __future__ import annotations

import datetime as _dt

from tcer.core import pricing
from tcer.core.models import TokenUsage


def fmt_int(x: int | None) -> str:
    return f"{x:,}" if x is not None else "-"


def fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "-"


def fmt_float(x: float | None, p: str = "0.00") -> str:
    """Format a float with a printf-style precision string, e.g. ``"0.0"``."""
    if x is None:
        return "-"
    width, _, prec = p.partition(".")
    return f"{x:{int(width) if width else 0}.{len(prec) if prec else 0}f}"


def fmt_money(x: float | None) -> str:
    return f"${x:.4f}" if x is not None else "-"


def fmt_dt(ms: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Epoch-milliseconds → local-time string. ``"-"`` on None / non-positive / bad range.

    Treats ``0`` and negatives as missing: Unix epoch display (1970-…) is almost
    never a real session start and confuses the timeline UI.
    """
    if ms is None or ms <= 0:
        return "-"
    try:
        return _dt.datetime.fromtimestamp(ms / 1000).strftime(fmt)
    except (OSError, OverflowError, ValueError):
        return "-"


def models_label(u: TokenUsage, max_n: int = 2) -> str:
    """Friendly comma-joined model list (e.g. 'Claude Opus 4.8'), sorted by id.

    Shows at most *max_n* model names; any extra are replaced by trailing ``…``.
    Filters out non-real models like ``<synthetic>`` and empty strings.
    """
    _SKIP = {"<synthetic>", ""}
    labels = [pricing.label(m) for m in sorted(u.models) if m not in _SKIP]
    if not labels:
        return "-"
    if len(labels) > max_n:
        return ", ".join(labels[:max_n]) + ", …"
    return ", ".join(labels)
