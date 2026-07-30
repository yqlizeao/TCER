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


# 统一日期/时间格式常量（本地时区，经 fmt_dt 渲染）。各显示点按需取用，
# 不再散落字面格式串；粒度差异是有意的（详情秒级、卡片分钟级）。
FMT_DATE = "%Y-%m-%d"
FMT_MINUTE = "%Y-%m-%d %H:%M"         # fmt_dt 默认；指标卡 / HTML / 导出
FMT_SECOND = "%Y-%m-%d %H:%M:%S"      # 详情弹窗
FMT_SHORT_MINUTE = "%m-%d %H:%M"      # 会话卡 / 趋势 tooltip / 对比下拉
FMT_SHORT_SECOND = "%m-%d %H:%M:%S"   # 逐回合时间线


def fmt_dt(ms: int | None, fmt: str = FMT_MINUTE) -> str:
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


def fmt_duration_ms(ms: int | None, *, short: bool = False) -> str:
    """毫秒时长 → 人可读串。``"-"`` on None / 非正。

    - ``short=True``（延迟/回合/审批/思考等短时长）：英文紧凑 ``<1s`` / ``4.3s`` / ``12m`` / ``2.4h``。
    - 默认（会话总时长）：中文 ``38 分钟`` / ``2.4 小时``（沿用旧 ``_duration_hours`` 阈值）。

    收口此前散落的 ``f"{ms/1000:.1f}s"`` 与几处无单位裸数字。
    """
    if not ms or ms <= 0:
        return "-"
    if short:
        if ms < 1000:
            return "<1s"
        if ms < 60_000:
            return f"{ms / 1000:.1f}s"
        if ms < 3_600_000:
            return f"{ms / 60_000:.0f}m"
        return f"{ms / 3_600_000:.1f}h"
    minutes = ms / 60_000
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def fmt_now(fmt: str = FMT_MINUTE) -> str:
    """当前本地时间串（「生成于」标签），收口重复的 ``datetime.now().strftime``。"""
    return _dt.datetime.now().strftime(fmt)


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
