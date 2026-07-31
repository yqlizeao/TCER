"""趋势页图表组件：趋势图 / 散点图 / 仪表板 / 时段热力图。

从 views.py 拆出的纯展示组件（MetricTrendSelector / _ChartTooltip / TrendChart /
ScatterChart / DashboardChart / HeatmapChart 及其配色与刻度工具）。指标取值与
格式化仍走 metric_defs SSOT（``raw_value`` / ``format_plot``），与指标网格一致。
"""
from __future__ import annotations

import math
import statistics
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

from tcer.core import metrics
from tcer.core.format import FMT_SHORT_MINUTE, fmt_dt
from . import theme
from .metric_defs import GROUPS, Metric, METRIC_BY_KEY, format_plot, raw_value
from .widgets import CheckRow, FlatMenu, ScrollFrame, Tooltip, flat_button

# 兼容旧名：图表代码里沿用 views 时代的别名。
metric_raw_value = raw_value
_metric_by_key = METRIC_BY_KEY

# Metrics that cannot be plotted (metadata / categorical / constant).
_NON_PLOTTABLE = frozenset({
    "models", "tools", "started", "last_time", "entrypoint",
    "task_type", "grade", "bl_tcer", "bl_cpe",
})

# Baseline reference lines (key → metrics module constant name).
_METRIC_BASELINE: dict[str, str] = {
    "tcer": "TCER_BASELINE",
    "cpe": "CPE_BASELINE",
}

# Fixed palette for multi-metric overlay (up to 4 lines).
_OVERLAY_COLORS = ["#007acc", "#4ec9b0", "#ce9178", "#c586c0"]

# CTEI grade background bands (lo, hi, fill_color, label) — names + thresholds
# derived from the metric SSOT (metrics.GRADE_BANDS); only the dark trend-fill
# colours are presentation-local here.
_BAND_FILL = {
    "优秀": "#142814", "良好": "#14202e", "中等": "#2e2a14",
    "低效": "#2e1e14", "极端低效": "#2e1414",
}

# 仪表盘折线色 = theme.GROUP_COLORS 各分类的「提亮同色相版」（展示局部色）。
# 分类原色是为大块表头填充设计的暗色，直接画细折线在深色单元格(#2d2d30)上会显得
# 灰蒙蒙；这里亮度提一档、饱和度补一点，保持色相识别（按色识组）又清晰精神。
# 表头条仍用原 GROUP_COLORS，二者同色相、深浅呼应。
_DASHBOARD_LINE_COLORS = {
    "G1": "#9d9da5",   # 中性灰 ← #3a3a3e
    "G2": "#4aa8ec",   # 亮蓝   ← #1e4a6f
    "G3": "#2fc4c4",   # 亮青   ← #1e5c5c
    "G4": "#4cc96a",   # 亮绿   ← #1e5c2b
    "G5": "#e2a23c",   # 亮橙   ← #6f4a1e
    "G6": "#b65bd6",   # 亮紫   ← #5a1e6f
}


def _build_ctei_bands() -> list[tuple[float, float, str, str]]:
    gb = metrics.GRADE_BANDS  # [(label, lower_bound)] best→worst
    bands = []
    for i, (label, lo) in enumerate(gb):
        hi = gb[i - 1][1] if i > 0 else 999
        if i == 0:
            rng = f">{lo:g}"
        elif i == len(gb) - 1:
            rng = f"<{gb[i - 1][1]:g}"
        else:
            rng = f"{lo:g}–{hi:g}"
        bands.append((lo, hi, _BAND_FILL[label], f"{label} {rng}"))
    return bands


_CTEI_BANDS: list[tuple[float, float, str, str]] = _build_ctei_bands()


def _units_compatible(overlays: list[_OverlayLine]) -> bool:
    """True if all overlays share the same non-empty unit (same-scale OK)."""
    units = {ol.unit for ol in overlays if ol.unit}
    return len(units) <= 1


# Raw numeric extraction for charts now lives in the metric SSOT (metric_defs).
# Kept as a module-level alias so existing call sites (and popups importing it
# from here) keep working.
metric_raw_value = raw_value


def _nice_ticks(v_min: float, v_max: float, n: int = 5) -> list[float]:
    """Compute *n* 'nice' (round-number) tick values between *v_min* and *v_max*."""
    span = v_max - v_min
    if span <= 0:
        return [v_min]
    raw_step = span / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / mag
    if residual <= 1.5:
        nice_step = 1 * mag
    elif residual <= 3.5:
        nice_step = 2 * mag
    elif residual <= 7.5:
        nice_step = 5 * mag
    else:
        nice_step = 10 * mag
    start = math.ceil(v_min / nice_step) * nice_step
    ticks = []
    v = start
    while v <= v_max + nice_step * 0.01:
        ticks.append(round(v, 10))
        v += nice_step
    # Always include v_min if no tick is close
    if ticks and ticks[0] > v_min + nice_step * 0.5:
        ticks.insert(0, round(v_min, 10))
    elif not ticks:
        ticks = [round(v_min, 10)]
    return ticks


def _fmt_num(v: float) -> str:
    """图表数值定点格式（杜绝科学计数法）。

    Python ``f"{v:g}"`` 对 |v|≥1e6 会输出 ``1.2e+06``，在趋势/散点/仪表盘的轴
    刻度与统计栏里既丑又难读。这里大数走紧凑 K/M/B（与模型对比页 Token 紧凑显示
    一致），小数定点保留有效位——全程不产生科学计数法。
    """
    av = abs(v)
    if av >= 1e9:
        return f"{v / 1e9:.1f}B"
    if av >= 1e6:
        return f"{v / 1e6:.1f}M"
    if av >= 1e3:
        return f"{v / 1e3:.1f}K"
    if av == 0:
        return "0"
    if av >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _aa_layer(c, items, store, ss: int = 2, pad: int = 3, tag: str | None = None):
    """抗锯齿数据层：把线/点渲染到透明图（ss× 超采样 + LANCZOS 缩回）贴回 canvas。

    tk.Canvas 不抗锯齿，对角线和圆点锯齿明显（HiDPI 位图拉伸更糟）。这里用 PIL
    超采样画出真正平滑的线/点。透明背景 → 下层坐标轴/网格透出，上层标记(极值/选中)
    盖在上面，z 序天然正确、无需重排。items 用 canvas 坐标；图幅自适应 items 的 bbox，
    无需 plot 尺寸。store 持有 PhotoImage 引用防 GC（每帧 _draw 重置）。

    items: ("line", pts, color, width) | ("dot", x, y, r, fill, outline)
    """
    from PIL import Image, ImageDraw, ImageTk
    if not items:
        return
    xs, ys = [], []
    for it in items:
        if it[0] == "line":
            for x, y in it[1]:
                xs.append(x); ys.append(y)
        else:  # dot
            xs.append(it[1] - it[3]); ys.append(it[2] - it[3])
            xs.append(it[1] + it[3]); ys.append(it[2] + it[3])
    if not xs:
        return
    x0, y0 = min(xs) - pad, min(ys) - pad
    x1, y1 = max(xs) + pad, max(ys) + pad
    w, h = max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for it in items:
        if it[0] == "line":
            pts = [(round((x - x0) * ss), round((y - y0) * ss)) for x, y in it[1]]
            if len(pts) >= 2:
                d.line(pts, fill=it[2], width=max(1, it[3] * ss), joint="curve")
        else:
            _, x, y, r, fill, outline = it[:6]
            lw = it[6] if len(it) > 6 else 1
            cx, cy = round((x - x0) * ss), round((y - y0) * ss)
            rr = max(1, r) * ss
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=fill, outline=outline,
                      width=max(1, lw * ss))
    im = im.resize((w, h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(im)
    store.append(photo)
    c.create_image(x0, y0, image=photo, anchor="nw", tags=(tag,) if tag else ())


class MetricTrendSelector:
    """Grouped metric picker with single/multi-select modes for the trend chart.

    Built from ``GROUPS`` (metric_defs), filtering out non-plottable keys.
    Each group gets a colored header; metrics are Checkbuttons.
    An "叠加模式" toggle switches between single-select (default) and
    multi-select (up to 4 metrics). Calls *on_change()* on every toggle.
    """

    MAX_OVERLAY = 4

    def __init__(self, parent, on_change) -> None:
        self._on_change = on_change
        self._overlay_mode = True   # 默认开启叠加（整框即开关，点击切换）
        self._vars: dict[str, tk.BooleanVar] = {}
        self._rows: dict[str, CheckRow] = {}

        # 叠加模式：整个框就是开关——点击切换。开启=ACCENT 蓝框（默认），
        # 关闭=灰框。框内只有标题+状态+说明，不再有内嵌勾选行，避免「框已蓝
        # 还得再点里面」的双层操作。tk 点击不冒泡，整框（含所有子）递归绑定。
        from .views import ui_icon as _ui_icon
        self._overlay_box = tk.Frame(parent, bg=theme.PANEL, highlightthickness=2,
                                     highlightbackground=theme.ACCENT, cursor="hand2")
        self._overlay_box.pack(fill="x", padx=2, pady=(4, 6))
        head = tk.Frame(self._overlay_box, bg=theme.PANEL)
        head.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(head, image=_ui_icon(head, "layers"), bg=theme.PANEL).pack(side="left", padx=(0, 4))
        tk.Label(head, text="叠加模式", bg=theme.PANEL, fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD).pack(side="left")
        self._overlay_state_lbl = tk.Label(head, text="已开启", bg=theme.PANEL,
                                           fg=theme.SUCCESS, font=theme.FONT_UI_SMALL)
        self._overlay_state_lbl.pack(side="right")
        tk.Label(self._overlay_box, text="点击切换。开启时可同时勾选最多 4 个指标叠加对比。",
                 bg=theme.PANEL, fg=theme.MUTED, font=theme.FONT_UI_SMALL,
                 anchor="w", justify="left", wraplength=160).pack(
                     fill="x", padx=10, pady=(2, 5))

        def _bind_toggle(node):
            node.bind("<Button-1>", lambda e: self._toggle_overlay(), add="+")
            for ch in node.winfo_children():
                _bind_toggle(ch)
        _bind_toggle(self._overlay_box)

        sf = ScrollFrame(parent, bg=theme.PANEL)
        sf.canvas.pack(fill="both", expand=True)
        self._scroll = sf
        inner = sf.inner

        for group in GROUPS:
            hdr = tk.Frame(inner, bg=theme.GROUP_COLORS.get(group.id, theme.PANEL),
                           padx=4, pady=2)
            hdr.pack(fill="x", pady=(2, 0))
            tk.Label(hdr, text=f"▼ {group.name}",
                     bg=hdr["bg"], fg=theme.FG,
                     font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(fill="x")

            for m in group.metrics:
                if m.key in _NON_PLOTTABLE:
                    continue
                var = tk.BooleanVar(value=(m.key == "tcer"))
                self._vars[m.key] = var
                label = m.name + (f"（{m.unit}）" if m.unit else "")
                self._rows[m.key] = CheckRow(
                    inner, label, var,
                    on_toggle=lambda k=m.key: self._on_toggle(k),
                    tooltip=m.tip,
                )

        self._scroll.update_scroll(reset=True)

    def _redraw(self) -> None:
        """var 可能被外部改动（单选取消其他、select 重置）→ 重画所有指标行。"""
        for row in self._rows.values():
            row._draw()

    def _update_overlay_box(self) -> None:
        """同步叠加框视觉：开启=ACCENT 蓝框 + 绿色「已开启」；关闭=灰框 + 灰「已关闭」。"""
        on = self._overlay_mode
        self._overlay_box.config(highlightbackground=theme.ACCENT if on else theme.BORDER)
        self._overlay_state_lbl.config(text="已开启" if on else "已关闭",
                                       fg=theme.SUCCESS if on else theme.MUTED)

    def _toggle_overlay(self) -> None:
        self._overlay_mode = not self._overlay_mode
        if not self._overlay_mode:
            # 关闭叠加：只留第一个选中
            selected = [k for k, v in self._vars.items() if v.get()]
            if len(selected) > 1:
                for k in selected[1:]:
                    self._vars[k].set(False)
        self._update_overlay_box()
        self._redraw()
        self._on_change()

    def _on_toggle(self, key: str) -> None:
        if not self._overlay_mode:
            # Single-select: uncheck all others
            for k, v in self._vars.items():
                if k != key:
                    v.set(False)
        else:
            # Multi-select: enforce MAX_OVERLAY limit
            selected = [k for k, v in self._vars.items() if v.get()]
            if len(selected) > self.MAX_OVERLAY:
                self._vars[key].set(False)
        # Ensure at least one is selected
        if not any(v.get() for v in self._vars.values()):
            self._vars["tcer"].set(True)
        self._redraw()
        self._on_change()

    def selected_keys(self) -> list[str]:
        return [k for k, v in self._vars.items() if v.get()]

    def select(self, key: str) -> None:
        for k, v in self._vars.items():
            v.set(k == key)
        self._redraw()

    @property
    def overlay_mode(self) -> bool:
        return self._overlay_mode


class _ChartTooltip:
    """Lightweight Toplevel tooltip that follows the mouse on a Canvas."""

    def __init__(self, canvas: tk.Canvas) -> None:
        self._canvas = canvas
        self._win: tk.Toplevel | None = None
        self._sig: tuple | None = None  # content signature last rendered

    def _place(self, x: int, y: int) -> None:
        """Compute a screen position with edge detection and move the window."""
        cx = self._canvas.winfo_rootx() + x + 16
        cy = self._canvas.winfo_rooty() + y - 10
        # Edge detection: flip if near screen edge
        sw = self._canvas.winfo_screenwidth()
        sh = self._canvas.winfo_screenheight()
        if cx + 260 > sw:
            cx = self._canvas.winfo_rootx() + x - 270
        if cy + 80 > sh:
            cy = self._canvas.winfo_rooty() + y - 80
        if cx < 0:
            cx = 4
        if cy < 0:
            cy = 4
        self._win.wm_geometry(f"+{cx}+{cy}")

    def show(self, x: int, y: int, lines: list[str],
             colors: list[str] | None = None) -> None:
        # The tooltip tracks the cursor every pixel, but its CONTENT only changes
        # when the hovered data point changes. Rebuilding a Toplevel + N Labels
        # per mouse-motion event is expensive, so when the content is unchanged
        # we reuse the existing window and just reposition it.
        sig = (tuple(lines), tuple(colors or ()))
        if self._win is not None and self._sig == sig:
            self._place(x, y)
            return
        self.hide()
        self._win = tk.Toplevel(self._canvas)
        self._win.wm_overrideredirect(True)
        self._place(x, y)
        fr = tk.Frame(self._win, bg=theme.PANEL_2, relief="solid",
                      borderwidth=1, padx=8, pady=5)
        fr.pack()
        for i, line in enumerate(lines):
            color = (colors[i] if colors and i < len(colors) else theme.FG)
            tk.Label(fr, text=line, bg=theme.PANEL_2, fg=color,
                     font=theme.FONT_UI, anchor="w").pack(anchor="w")
        self._sig = sig

    def hide(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None
        self._sig = None


@dataclass
class _OverlayLine:
    """Cached geometry for one metric's trend line."""
    key: str
    name: str
    unit: str
    color: str
    values: list[float | None] = field(default_factory=list)
    timestamps: list[int | None] = field(default_factory=list)
    screen_pts: list[tuple[float, float]] = field(default_factory=list)
    report_indices: list[int] = field(default_factory=list)
    y_min: float = 0.0
    y_max: float = 0.0


class TrendChart:
    """Tab 3: multi-metric interactive time-series chart with statistics.

    Sub-components:
    - ``MetricTrendSelector`` (left, 180px) for metric selection
    - ``tk.Canvas`` for the chart
    - ``_ChartTooltip`` for hover details
    - Statistics summary at the bottom

    Controller callbacks:
    - ``on_select_session(sid)``: fired when user clicks a data point
    """

    _PAD_L = 62
    _PAD_R = 20
    _PAD_T = 30
    _PAD_B = 36
    _HIT_RADIUS = 8

    def __init__(self, parent, controller=None) -> None:
        self._controller = controller
        self._reports: list = []
        self._all_reports: list = []
        self._overlay: list[_OverlayLine] = []
        self._selected_idx: int | None = None
        self._tooltip = None
        self._resize_after: str | None = None
        self._mode = tk.StringVar(value="trend")
        # Zoom state
        self._zoom_active = False
        self._zoom_sel_start: int | None = None
        self._zoom_offset = 0  # index offset into _all_reports when zoomed
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_moved = False

        self._build(parent)

    # -- layout -----------------------------------------------------------
    def _build(self, parent) -> None:
        self._body = tk.Frame(parent, bg=theme.BG)
        self._body.pack(fill="both", expand=True)

        # Dynamic content area (rebuilt on mode switch)
        self._content = tk.Frame(self._body, bg=theme.BG)
        self._content.pack(fill="both", expand=True)
        self._build_trend_content()

    def _clear_content(self) -> None:
        for w in self._content.winfo_children():
            w.destroy()

    def _add_mode_buttons(self, parent) -> tk.Frame:
        """Build the shared '趋势分析' group header with a segmented mode switcher
        (趋势图 / 散点图 / 仪表板 / 时段) — 深色 pill，选中 ACCENT，替代老式
        Radiobutton。各子模式重建 _content，调用方把额外控件塞进返回的 header。"""
        gc = theme.GROUP_COLORS["G_NEUTRAL"]
        header = tk.Frame(parent, bg=gc, padx=6, pady=3)
        header.pack(fill="x", pady=(1, 0))
        tk.Label(header, text="▼ 趋势分析", bg=gc, fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")
        from .views import ui_icon
        seg = tk.Frame(header, bg=theme.CONTROL_BG, padx=2, pady=2)
        seg.pack(side="left", padx=(10, 0))
        self._mode_pills: dict[str, tk.Frame] = {}
        for label, val in (("趋势图", "trend"), ("散点图", "scatter"),
                           ("仪表板", "dashboard"), ("时段", "heatmap")):
            pill = tk.Frame(seg, bg=theme.CONTROL_BG)
            pill.pack(side="left", padx=1)
            click = lambda e, v=val: self._set_mode(v)
            members: list = []
            icon = ui_icon(seg, val)
            if icon is not None:
                il = tk.Label(pill, image=icon, bg=theme.CONTROL_BG, cursor="hand2")
                il.pack(side="left", padx=(4, 0))
                il.bind("<Button-1>", click)
                members.append(il)
            lbl = tk.Label(pill, text=label, bg=theme.CONTROL_BG, fg=theme.MUTED,
                           cursor="hand2", font=theme.FONT_UI_SMALL, padx=3)
            lbl.pack(side="left")
            lbl.bind("<Button-1>", click)
            members.append(lbl)
            pill._members = members
            self._mode_pills[val] = pill
        self._update_mode_pills()
        return header

    def _set_mode(self, val: str) -> None:
        """分段按钮点击：切 _mode、更新 pill 视觉、重建内容。"""
        if self._mode.get() == val:
            return
        self._mode.set(val)
        self._update_mode_pills()
        self._switch_mode()

    def _update_mode_pills(self) -> None:
        """按 self._mode 同步分段按钮选中态：选中 ACCENT+白字，未选 #333333+MUTED。"""
        cur = self._mode.get()
        for val, pill in self._mode_pills.items():
            on = val == cur
            bg = theme.ACCENT if on else theme.CONTROL_BG
            fg = theme.FG_WHITE if on else theme.MUTED
            pill.config(bg=bg)
            for w in getattr(pill, "_members", ()):
                try:
                    w.config(bg=bg)
                    if isinstance(w, tk.Label) and w.cget("text"):
                        w.config(fg=fg)
                except tk.TclError:
                    pass

    def _build_trend_content(self) -> None:
        self._clear_content()
        # Left: metric selector
        left = tk.Frame(self._content, bg=theme.PANEL, width=180)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self._selector = MetricTrendSelector(left, on_change=self._on_selection_change)

        # Separator
        sep = tk.Frame(self._content, bg=theme.BORDER, width=2)
        sep.pack(side="left", fill="y")

        # Right: header + canvas + stats
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True)

        # Mode buttons in group header
        mode_header = self._add_mode_buttons(right)
        self._legend_frame = tk.Frame(mode_header, bg=theme.GROUP_COLORS["G_NEUTRAL"])
        self._legend_frame.pack(side="right")
        from .views import ui_icon as _ui_icon
        self._zoom_reset_btn = flat_button(mode_header, "重置缩放",
                                           self._reset_zoom, padx=theme.PAD_M,
                                           image=_ui_icon(mode_header, "zoom"), compound="left")
        # Hidden by default; shown when zoom is active

        self.canvas = tk.Canvas(right, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._tooltip.hide())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Destroy>", lambda e: self._tooltip.hide())
        self.canvas.bind("<Left>", self._on_key_prev)
        self.canvas.bind("<Right>", self._on_key_next)
        self.canvas.focus_set()

        # Stats in group header
        stats_header = tk.Frame(right, bg=theme.GROUP_COLORS["G6"], padx=6, pady=3)
        stats_header.pack(fill="x", pady=(1, 0))
        tk.Label(stats_header, text="▼ 统计", bg=theme.GROUP_COLORS["G6"], fg=theme.FG,
                 font=theme.FONT_UI_SMALL_BOLD, anchor="w").pack(side="left")
        self._stats_frame = tk.Frame(right, bg=theme.PANEL, padx=6, pady=3)
        self._stats_frame.pack(fill="x")
        self._stats_labels = []

    def _build_dashboard_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        # Mode buttons in group header (same as trend)
        mode_header = self._add_mode_buttons(right)
        tk.Label(mode_header, text="6 组代表指标总览", bg=theme.GROUP_COLORS["G_NEUTRAL"],
                 fg=theme.MUTED, font=theme.FONT_UI_SMALL).pack(side="left", padx=8)

        self._dashboard = DashboardChart(right)
        self._dashboard.update(self._reports)

    def _build_scatter_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        # Mode buttons in group header (same as trend)
        self._add_mode_buttons(right)

        self._scatter_chart = ScatterChart(right)
        self._scatter_chart.update(self._reports)

    def _build_heatmap_content(self) -> None:
        self._clear_content()
        right = tk.Frame(self._content, bg=theme.BG)
        right.pack(fill="both", expand=True)

        # Mode buttons in group header (same as trend)
        self._add_mode_buttons(right)

        self._heatmap_chart = HeatmapChart(right, controller=self._controller)
        self._heatmap_chart.update(self._reports)

    def _switch_mode(self) -> None:
        # Cancel any pending resize redraw
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
            self._resize_after = None
        # Save selector state before teardown
        saved_keys = self._selector.selected_keys() if hasattr(self, '_selector') else ["tcer"]
        self._tooltip.hide()
        mode = self._mode.get()
        if mode == "scatter":
            self._build_scatter_content()
        elif mode == "dashboard":
            self._build_dashboard_content()
        elif mode == "heatmap":
            self._build_heatmap_content()
        else:
            self._build_trend_content()
            # Restore selector state
            if saved_keys and hasattr(self, '_selector'):
                for k in saved_keys:
                    if k in self._selector._vars:
                        self._selector._vars[k].set(True)
            self._draw()

    # -- public API -------------------------------------------------------
    def update(self, reports) -> None:
        """Update the chart with new reports, restoring the selected session by sid.

        Zoom is intentionally NOT preserved: the zoom window is a pair of
        indices into the previous report list, which is meaningless once the
        data changes. Only the selected data point is carried over.
        """
        # Save current state
        old_selected_sid = None
        if self._selected_idx is not None and self._selected_idx < len(self._reports):
            r = self._reports[self._selected_idx]
            old_selected_sid = r.meta.session_id or r.meta.path.stem

        # Update data
        self._all_reports = sorted(reports,
                                   key=lambda r: r.usage.started_at or r.usage.ended_at or 0)
        self._reports = list(self._all_reports)
        self._zoom_active = False
        self._zoom_offset = 0
        self._selected_idx = None
        self._tooltip.hide()

        # Restore selection if the session still exists
        if old_selected_sid:
            for i, r in enumerate(self._reports):
                if (r.meta.session_id or r.meta.path.stem) == old_selected_sid:
                    self._selected_idx = i
                    break

        # 按当前模式刷新对应子图。非趋势模式下 trend canvas 已被销毁，
        # 直接 _draw() 会 TclError（此前 scatter/dashboard 模式即有此问题）。
        mode = self._mode.get()
        if mode == "scatter" and hasattr(self, "_scatter_chart"):
            self._scatter_chart.update(self._reports)
        elif mode == "dashboard" and hasattr(self, "_dashboard"):
            self._dashboard.update(self._reports)
        elif mode == "heatmap" and hasattr(self, "_heatmap_chart"):
            self._heatmap_chart.update(self._reports)
        else:
            self._draw()

    # -- event handlers ---------------------------------------------------
    def _on_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def _on_selection_change(self) -> None:
        self._selected_idx = None
        self._draw()

    def _on_motion(self, event) -> None:
        idx = self._hit_test(event.x, event.y)
        if idx is None:
            self._tooltip.hide()
            return
        if idx < len(self._reports):
            self._show_tooltip(idx, event.x, event.y)

    def _on_press(self, event) -> None:
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._drag_moved = False

    def _on_drag(self, event) -> None:
        if abs(event.x - self._drag_start_x) > 8:
            self._drag_moved = True
            # Only redraw the selection rectangle (tag-based, no full redraw)
            self.canvas.delete("sel_rect")
            c = self.canvas
            c.create_rectangle(self._drag_start_x, self._PAD_T,
                               event.x, c.winfo_height() - self._PAD_B,
                               outline=theme.ACCENT, dash=(3, 3), width=1,
                               tags="sel_rect")

    def _on_release(self, event) -> None:
        self.canvas.delete("sel_rect")
        if self._drag_moved:
            # Zoom: find report indices at start and end X positions
            self._apply_zoom(self._drag_start_x, event.x)
        else:
            # Click: select data point
            idx = self._hit_test(event.x, event.y)
            if idx is not None and self._controller:
                self._selected_idx = idx
                self._draw()
                r = self._reports[idx]
                sid = r.meta.session_id or r.meta.path.stem
                self._controller.on_select_session(sid)
        self._drag_moved = False

    def _apply_zoom(self, x_start: int, x_end: int) -> None:
        """Zoom to the report index range between x_start and x_end."""
        x_lo, x_hi = min(x_start, x_end), max(x_start, x_end)
        # Find report indices closest to the X positions
        if not self._overlay:
            return
        ol = self._overlay[0]
        if not ol.screen_pts:
            return
        idx_lo, idx_hi = None, None
        for j, (px, _py) in enumerate(ol.screen_pts):
            ri = ol.report_indices[j]
            if px >= x_lo and idx_lo is None:
                idx_lo = ri
            if px <= x_hi:
                idx_hi = ri
        if idx_lo is None or idx_hi is None or idx_lo >= idx_hi:
            return
        # Map back to _all_reports using the current zoom offset
        abs_lo = self._zoom_offset + idx_lo
        abs_hi = self._zoom_offset + idx_hi
        self._reports = self._all_reports[abs_lo:abs_hi + 1]
        self._zoom_offset = abs_lo
        self._zoom_active = True
        self._selected_idx = None
        self._draw()

    def _reset_zoom(self) -> None:
        self._reports = list(self._all_reports)
        self._zoom_active = False
        self._zoom_offset = 0
        self._selected_idx = None
        self._draw()

    def _on_double_click(self, event) -> None:
        """Double-click: show radar popup for the nearest data point."""
        idx = self._hit_test(event.x, event.y)
        if idx is not None and idx < len(self._reports):
            from . import popups
            popups.RadarPopup(self.canvas, self._reports[idx], self._reports)

    def _on_right_click(self, event) -> None:
        """Right-click: context menu for the nearest data point."""
        if not self._controller:
            return
        idx = self._hit_test(event.x, event.y)
        if idx is None or idx >= len(self._reports):
            return
        r = self._reports[idx]
        sid = r.meta.session_id or r.meta.path.stem
        menu = FlatMenu(self.canvas)
        menu.add_command(
            label=f"查看会话详情 · {sid[:16]}…",
            command=lambda: self._controller.show_session_detail(sid)
                            if self._controller else None,
        )
        menu.add_command(
            label="查看雷达图",
            command=lambda: self._show_radar_for(idx),
        )
        menu.add_separator()
        menu.add_command(
            label=f"选中此会话（第 {idx + 1} 个）",
            command=lambda: self._select_point(idx),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _show_radar_for(self, idx: int) -> None:
        if idx < len(self._reports):
            from . import popups
            popups.RadarPopup(self.canvas, self._reports[idx], self._reports)

    def _select_point(self, idx: int) -> None:
        self._selected_idx = idx
        self._draw()
        if self._controller and idx < len(self._reports):
            r = self._reports[idx]
            sid = r.meta.session_id or r.meta.path.stem
            self._controller.on_select_session(sid)

    def select_session_by_sid(self, sid: str) -> None:
        """Public API: find and highlight a session by its ID without rebuilding
        the chart (preserves zoom); only the selection overlay is refreshed."""
        for i, r in enumerate(self._reports):
            if (r.meta.session_id or r.meta.path.stem) == sid:
                if self._selected_idx == i:
                    return  # already highlighted
                self._selected_idx = i
                # 非趋势模式下 trend canvas 已销毁；只记录索引，切回趋势图时生效。
                if self._mode.get() == "trend":
                    self._refresh_selection()
                return

    def _on_key_prev(self, _event=None) -> None:
        """Left arrow: select previous data point."""
        if not self._overlay:
            return
        ol = self._overlay[0]
        if not ol.report_indices:
            return
        if self._selected_idx is None:
            new_idx = ol.report_indices[-1]
        else:
            prev = [i for i in ol.report_indices if i < self._selected_idx]
            new_idx = prev[-1] if prev else ol.report_indices[0]
        self._select_point(new_idx)

    def _on_key_next(self, _event=None) -> None:
        """Right arrow: select next data point."""
        if not self._overlay:
            return
        ol = self._overlay[0]
        if not ol.report_indices:
            return
        if self._selected_idx is None:
            new_idx = ol.report_indices[0]
        else:
            nxt = [i for i in ol.report_indices if i > self._selected_idx]
            new_idx = nxt[0] if nxt else ol.report_indices[-1]
        self._select_point(new_idx)

    # -- hit testing ------------------------------------------------------
    def _hit_test(self, mx: int, my: int) -> int | None:
        """Return report index of the nearest data point within _HIT_RADIUS."""
        best_idx = None
        best_dist = self._HIT_RADIUS + 1.0
        for ol in self._overlay:
            for pt_i, (px, py) in enumerate(ol.screen_pts):
                d = math.hypot(mx - px, my - py)
                if d < best_dist:
                    best_dist = d
                    best_idx = ol.report_indices[pt_i]
        return best_idx

    # -- tooltip ----------------------------------------------------------
    @staticmethod
    def _fmt_metric(key: str, raw: float, m: 'Metric | None') -> str:
        """Format a single metric value for tooltip display (SSOT: format_plot)."""
        return format_plot(key, raw, m)

    def _show_tooltip(self, idx: int, mx: int, my: int) -> None:
        r = self._reports[idx]
        title = (r.meta.title or r.meta.session_id or r.meta.path.stem
                 or "(无标题)")[:30]
        ts = fmt_dt(r.usage.started_at, FMT_SHORT_MINUTE)
        lines = [title, f"时间: {ts}"]
        colors = [theme.ACCENT, theme.MUTED]
        for ol in self._overlay:
            raw = metric_raw_value(r, ol.key)
            if raw is not None:
                m = _metric_by_key.get(ol.key)
                disp = self._fmt_metric(ol.key, raw, m)
                lines.append(f"{ol.name}: {disp}")
                colors.append(ol.color)
        self._tooltip.show(mx, my, lines, colors)

    # -- drawing ----------------------------------------------------------
    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._aa_imgs = []   # PIL 抗锯齿数据层图像引用（防 GC，每帧重置）
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return

        # Show/hide zoom reset button
        if hasattr(self, '_zoom_reset_btn'):
            if self._zoom_active:
                self._zoom_reset_btn.pack(side="right", padx=4)
            else:
                self._zoom_reset_btn.pack_forget()

        keys = self._selector.selected_keys()
        self._build_overlay(keys)
        self._update_legend()

        if not self._overlay:
            c.create_text(w / 2, h / 2, text="无有效数据",
                          fill=theme.MUTED, font=theme.FONT_UI, justify="center")
            self._update_stats([])
            return

        pad_l, pad_r, pad_t, pad_b = self._PAD_L, self._PAD_R, self._PAD_T, self._PAD_B
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b

        n_overlays = len(self._overlay)
        use_multi = (n_overlays >= 2)

        if use_multi:
            # Multi-metric rendering
            if n_overlays == 2 and not _units_compatible(self._overlay):
                self._draw_multi_dual_axis(c, w, h, pad_l, pad_r, pad_t, pad_b,
                                           plot_w, plot_h)
            else:
                self._draw_multi_normalized(c, w, h, pad_l, pad_r, pad_t, pad_b,
                                            plot_w, plot_h)
        else:
            # Single-metric rendering (existing logic)
            ol = self._overlay[0]
            valid = [(i, v) for i, v in enumerate(ol.values) if v is not None]
            if len(valid) < 1:
                c.create_text(w / 2, h / 2, text="该指标在当前时间范围内无有效数据",
                              fill=theme.MUTED, font=theme.FONT_UI, justify="center")
                self._update_stats([])
                return

            lo, hi = ol.y_min, ol.y_max
            bl_val = self._baseline_value(ol.key)
            if bl_val is not None:
                lo, hi = min(lo, bl_val), max(hi, bl_val)
            if hi - lo < 1e-12:
                lo -= 1
                hi += 1

            def xv(i):
                n = len(ol.values)
                return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w / 2

            def yv(v):
                return pad_t + plot_h * (1 - (v - lo) / (hi - lo))

            if ol.key == "ctei":
                self._draw_ctei_bands(c, yv, pad_l, plot_w, lo, hi)

            ticks = _nice_ticks(lo, hi, 5)
            for tv in ticks:
                ty = yv(tv)
                c.create_line(pad_l, ty, pad_l + plot_w, ty,
                              fill=theme.CONTROL_BG, dash=(2, 4))
                c.create_text(pad_l - 6, ty, text=_fmt_num(tv), anchor="e",
                              fill=theme.MUTED, font=theme.FONT_UI_SMALL)

            if bl_val is not None and lo <= bl_val <= hi:
                by = yv(bl_val)
                c.create_line(pad_l, by, pad_l + plot_w, by,
                              fill=theme.WARNING, dash=(4, 3))
                c.create_text(pad_l + plot_w - 2, by, text="基准", anchor="e",
                              fill=theme.WARNING, font=theme.FONT_UI_SMALL)

            c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill=theme.BORDER)
            c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                          fill=theme.BORDER)

            self._draw_x_axis(c, ol.timestamps, pad_l, plot_w, pad_t, plot_h,
                              len(ol.values))

            self._draw_overlay_line(c, ol, xv, yv,
                                    draw_extrema=True, draw_selection=True)

            # Prediction line (linear extrapolation)
            if len(ol.screen_pts) >= 3:
                self._draw_prediction(c, ol, xv, yv, pad_l, plot_w)

            label = f"{ol.name}"
            if ol.unit:
                label += f"（{ol.unit}）"
            c.create_text(pad_l + plot_w / 2, 6, text=f"{label} · 趋势",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")

            valid_vals = [v for _, v in valid]
            ts_list = [ol.timestamps[i] for i, _ in valid]
            self._update_stats([(ol.key, ol.name, ol.unit, ol.color,
                                 valid_vals, ts_list)])

    def _build_overlay(self, keys: list[str]) -> None:
        """Build _OverlayLine objects for the given metric keys."""
        self._overlay = []
        for ki, key in enumerate(keys):
            metric = _metric_by_key.get(key)
            if metric is None:
                continue
            values = [metric_raw_value(r, key) for r in self._reports]
            timestamps = [r.usage.started_at or r.usage.ended_at
                          for r in self._reports]
            valid_vals = [v for v in values if v is not None]
            if not valid_vals:
                continue
            self._overlay.append(_OverlayLine(
                key=key, name=metric.name,
                unit=metric.unit, color=_OVERLAY_COLORS[ki % len(_OVERLAY_COLORS)],
                values=values, timestamps=timestamps,
                y_min=min(valid_vals), y_max=max(valid_vals),
            ))

    def _baseline_value(self, key: str) -> float | None:
        bl_name = _METRIC_BASELINE.get(key)
        if bl_name:
            return getattr(metrics, bl_name, None)
        return None

    def _draw_x_axis(self, c, timestamps, pad_l, plot_w, pad_t, plot_h,
                     n_pts) -> None:
        """Draw X-axis date labels with smart density."""
        valid_ts = [t for t in timestamps if t is not None]
        if not valid_ts:
            return
        min_ts, max_ts = min(valid_ts), max(valid_ts)
        span_ms = max_ts - min_ts
        # Choose format based on span
        if span_ms <= 24 * 3600_000:
            dt_fmt = "%H:%M"
        else:
            dt_fmt = "%m-%d"

        max_labels = max(2, int(plot_w / 55))
        step = max(1, len(timestamps) // max_labels)

        for i in range(n_pts):
            ts = timestamps[i]
            if ts is None:
                continue
            if i % step != 0 and i != n_pts - 1:
                continue
            px = pad_l + (plot_w * i / (n_pts - 1)) if n_pts > 1 else pad_l + plot_w / 2
            # Tick mark
            c.create_line(px, pad_t + plot_h, px, pad_t + plot_h + 4,
                          fill=theme.BORDER)
            label = fmt_dt(ts, dt_fmt)
            if label == "-":
                continue
            # Stagger alternating labels to reduce overlap
            y_off = 10 if (i // step) % 2 == 0 else 20
            c.create_text(px, pad_t + plot_h + y_off, text=label,
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")

    def _draw_ctei_bands(self, c, yv, pad_l, plot_w, lo, hi) -> None:
        """Draw grade background bands when CTEI is selected."""
        for lo_b, hi_b, color_b, _label in _CTEI_BANDS:
            if hi_b < lo or lo_b > hi:
                continue
            y_top = yv(min(hi_b, hi))
            y_bot = yv(max(lo_b, lo))
            c.create_rectangle(pad_l, y_top, pad_l + plot_w, y_bot,
                               fill=color_b, outline="")
            # Right-edge label
            c.create_text(pad_l + plot_w - 4, (y_top + y_bot) / 2,
                          text=_label, anchor="e",
                          fill="#555555", font=theme.FONT_UI_SMALL)

    @staticmethod
    def _find_extrema(values: list[float]) -> tuple[list[int], list[int]]:
        """Return (peak_indices, valley_indices) for a numeric series."""
        peaks, valleys = [], []
        for i in range(1, len(values) - 1):
            if values[i] > values[i - 1] and values[i] > values[i + 1]:
                peaks.append(i)
            elif values[i] < values[i - 1] and values[i] < values[i + 1]:
                valleys.append(i)
        # Limit to top 3 each by value prominence
        if len(peaks) > 3:
            peaks = sorted(peaks, key=lambda i: values[i], reverse=True)[:3]
            peaks.sort()
        if len(valleys) > 3:
            valleys = sorted(valleys, key=lambda i: values[i])[:3]
            valleys.sort()
        return peaks, valleys

    @staticmethod
    def _draw_marker(c, px: float, py: float, is_peak: bool, color: str) -> None:
        """Draw a small triangle marker at an extremum."""
        s = 5
        if is_peak:  # upward triangle
            pts = [px, py - s - 2, px - s, py - 2, px + s, py - 2]
        else:  # downward triangle
            pts = [px, py + s + 2, px - s, py + 2, px + s, py + 2]
        c.create_polygon(pts, fill=color, outline=theme.FG)

    # -- multi-metric rendering -------------------------------------------
    def _draw_overlay_line(self, c, ol, xv_fn, yv_fn,
                           draw_extrema: bool = False,
                           draw_selection: bool = False) -> None:
        """Shared: build screen coords, draw polyline + dots, optionally extrema/selection."""
        ol.screen_pts = []
        ol.report_indices = []
        for i, v in enumerate(ol.values):
            if v is None:
                continue
            ol.screen_pts.append((xv_fn(i), yv_fn(v)))
            ol.report_indices.append(i)

        color = ol.color
        _items = []
        if len(ol.screen_pts) >= 2:
            _items.append(("line", list(ol.screen_pts), color, 2))
        for px, py in ol.screen_pts:
            _items.append(("dot", px, py, 3, color, theme.FG))
        _aa_layer(c, _items, self._aa_imgs)

        if draw_extrema and len(ol.screen_pts) >= 5:
            raw_vals = [ol.values[ol.report_indices[i]]
                        for i in range(len(ol.report_indices))]
            peaks, valleys = self._find_extrema(raw_vals)
            for pi in peaks:
                if pi < len(ol.screen_pts):
                    px, py = ol.screen_pts[pi]
                    self._draw_marker(c, px, py, True, color)
            for vi in valleys:
                if vi < len(ol.screen_pts):
                    px, py = ol.screen_pts[vi]
                    self._draw_marker(c, px, py, False, color)

        if draw_selection and self._selected_idx is not None:
            self._draw_selection(c, ol)

    def _draw_selection(self, c, ol) -> None:
        """Draw the crosshair + ring + label for the selected point on one
        overlay. Items carry the ``sel_overlay`` tag so they can be wiped and
        redrawn incrementally (see ``_refresh_selection``) without a full chart
        redraw. 选中环走 ``_aa_layer`` 抗锯齿（create_oval 在深色下锯齿明显）。"""
        if self._selected_idx is None:
            return
        for j, ri in enumerate(ol.report_indices):
            if ri == self._selected_idx:
                px, py = ol.screen_pts[j]
                # Vertical crosshair line (dashed；直线锯齿不明显，保留 create_line)
                c.create_line(px, self._PAD_T, px,
                              c.winfo_height() - self._PAD_B,
                              fill=theme.ACCENT, dash=(4, 3), width=1,
                              tags="sel_overlay")
                # 选中环 + 内点：_aa_layer 抗锯齿，白色（比 ACCENT 蓝更醒目、不混数据色）
                ring = theme.FG_WHITE
                _aa_layer(c, [
                    ("dot", px, py, 10, (0, 0, 0, 0), ring, 2),   # 外环：透明填充 + 白边
                    ("dot", px, py, 3, ring, ring),               # 内点
                ], self._aa_imgs, tag="sel_overlay")
                # 标签：会话标题（非 session_id）
                sel_r = self._reports[ri] if ri < len(self._reports) else None
                if sel_r:
                    sel_title = (sel_r.meta.title or sel_r.meta.path.stem
                                 or "(无标题)")[:24]
                    c.create_text(px, py - 16, text=f"▸ {sel_title}",
                                  fill=ring, font=theme.FONT_UI_SMALL_BOLD,
                                  anchor="s", tags="sel_overlay")
                break

    def _refresh_selection(self) -> None:
        """Incrementally redraw just the selection overlay (crosshair + ring +
        label) without rebuilding the whole chart, so picking a session from the
        list doesn't re-walk every data point. Mirrors the tag-based drag
        rectangle; only drawn in single-metric mode (matching full ``_draw``)."""
        c = self.canvas
        c.delete("sel_overlay")
        if self._selected_idx is None or len(self._overlay) != 1:
            return
        self._draw_selection(c, self._overlay[0])

    def _draw_prediction(self, c, ol, xv, yv, pad_l, plot_w) -> None:
        """Draw a 3-point linear extrapolation as a dashed line."""
        pts = [(i, v) for i, v in enumerate(ol.values) if v is not None]
        if len(pts) < 3:
            return
        n = len(pts)
        # Use last N points for regression (at most 10)
        window = pts[-min(10, n):]
        xs_w = [p[0] for p in window]
        ys_w = [p[1] for p in window]
        mx_ = sum(xs_w) / len(xs_w)
        my_ = sum(ys_w) / len(ys_w)
        ss = sum((x - mx_) ** 2 for x in xs_w)
        if ss == 0:
            return
        slope = sum((xs_w[i] - mx_) * (ys_w[i] - my_) for i in range(len(xs_w))) / ss
        intercept = my_ - slope * mx_
        # Clamp range: use data min/max as soft bounds
        all_vals = [v for _, v in pts]
        v_min, v_max = min(all_vals), max(all_vals)
        v_margin = (v_max - v_min) * 0.3 if v_max > v_min else abs(v_max) * 0.3 or 1.0
        clamp_lo, clamp_hi = v_min - v_margin, v_max + v_margin
        # Extrapolate 3 points beyond the last data point
        last_i = pts[-1][0]
        pred_pts = []
        for step in range(1, 4):
            pi = last_i + step
            pv = max(clamp_lo, min(clamp_hi, slope * pi + intercept))
            pred_pts.append((xv(pi), yv(pv)))
        # Connect last actual point to first prediction
        last_actual = (xv(last_i), yv(pts[-1][1]))
        all_pred = [last_actual] + pred_pts
        c.create_line(all_pred, fill=theme.WARNING, width=1, dash=(4, 4))
        # Mark prediction points with hollow circles
        for px, py in pred_pts:
            c.create_oval(px - 2, py - 2, px + 2, py + 2,
                          outline=theme.WARNING, fill="")
        # Label
        mid = pred_pts[1]
        c.create_text(mid[0], mid[1] - 10, text="预测", fill=theme.WARNING,
                      font=theme.FONT_UI_SMALL)

    def _draw_multi_normalized(self, c, w, h, pad_l, pad_r, pad_t, pad_b,
                               plot_w, plot_h) -> None:
        """Draw 2+ metrics on a normalized 0–1 Y scale."""
        n = len(self._overlay[0].values)

        def xv(i):
            return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w / 2

        # Draw each line (normalized per-overlay)
        for ol in self._overlay:
            span = ol.y_max - ol.y_min if ol.y_max != ol.y_min else 1.0
            lo_ = ol.y_min
            def yv(v, _s=span, _lo=lo_):
                return pad_t + plot_h * (1 - (v - _lo) / _s)
            self._draw_overlay_line(c, ol, xv, yv)

        # Axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill=theme.BORDER)
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                      fill=theme.BORDER)
        c.create_text(pad_l - 6, pad_t, text="1.0", anchor="e",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL)
        c.create_text(pad_l - 6, pad_t + plot_h, text="0.0", anchor="e",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL)
        c.create_text(pad_l - 6, pad_t + plot_h / 2, text="0.5", anchor="e",
                      fill="#444444", font=theme.FONT_UI_SMALL)
        c.create_line(pad_l, pad_t + plot_h / 2, pad_l + plot_w,
                      pad_t + plot_h / 2, fill="#2a2a2a", dash=(2, 4))

        # X-axis
        self._draw_x_axis(c, self._overlay[0].timestamps,
                          pad_l, plot_w, pad_t, plot_h, n)

        # Title
        c.create_text(pad_l + plot_w / 2, 6, text="多指标归一化对比（0–1）",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")

        # Stats for all metrics
        stats_items = []
        for ol in self._overlay:
            valid = [v for v in ol.values if v is not None]
            if valid:
                stats_items.append((ol.key, ol.name, ol.unit, ol.color,
                                    valid, ol.timestamps))
        self._update_stats(stats_items)

    def _draw_multi_dual_axis(self, c, w, h, pad_l, pad_r, pad_t, pad_b,
                              plot_w, plot_h) -> None:
        """Draw 2 metrics with independent left/right Y axes."""
        ol_l, ol_r = self._overlay[0], self._overlay[1]
        n = len(ol_l.values)

        lo_l, hi_l = ol_l.y_min, ol_l.y_max
        lo_r, hi_r = ol_r.y_min, ol_r.y_max
        if hi_l - lo_l < 1e-12:
            lo_l -= 1; hi_l += 1
        if hi_r - lo_r < 1e-12:
            lo_r -= 1; hi_r += 1

        def xv(i):
            return pad_l + (plot_w * i / (n - 1)) if n > 1 else pad_l + plot_w / 2

        def y_l(v):
            return pad_t + plot_h * (1 - (v - lo_l) / (hi_l - lo_l))

        def y_r(v):
            return pad_t + plot_h * (1 - (v - lo_r) / (hi_r - lo_r))

        # Left grid lines
        for tv in _nice_ticks(lo_l, hi_l, 4):
            ty = y_l(tv)
            c.create_line(pad_l, ty, pad_l + plot_w, ty, fill="#2a2a2a", dash=(2, 4))
            c.create_text(pad_l - 6, ty, text=_fmt_num(tv), anchor="e",
                          fill=ol_l.color, font=theme.FONT_UI_SMALL)

        # Right grid lines
        for tv in _nice_ticks(lo_r, hi_r, 4):
            ty = y_r(tv)
            c.create_text(pad_l + plot_w + 6, ty, text=_fmt_num(tv), anchor="w",
                          fill=ol_r.color, font=theme.FONT_UI_SMALL)

        # Axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill=theme.BORDER)
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                      fill=theme.BORDER)
        c.create_line(pad_l + plot_w, pad_t, pad_l + plot_w, pad_t + plot_h,
                      fill=ol_r.color, dash=(3, 3))

        # Left line
        self._draw_overlay_line(c, ol_l, xv, y_l)

        # Right line
        self._draw_overlay_line(c, ol_r, xv, y_r)

        # X-axis
        self._draw_x_axis(c, ol_l.timestamps, pad_l, plot_w, pad_t, plot_h, n)

        # Axis labels
        c.create_text(pad_l, pad_t - 10,
                      text=f"← {ol_l.name}" + (f"（{ol_l.unit}）" if ol_l.unit else ""),
                      fill=ol_l.color, font=theme.FONT_UI_SMALL, anchor="w")
        c.create_text(pad_l + plot_w, pad_t - 10,
                      text=f"{ol_r.name}" + (f"（{ol_r.unit}）" if ol_r.unit else "") + " →",
                      fill=ol_r.color, font=theme.FONT_UI_SMALL, anchor="e")

        # Stats for both
        stats = []
        for ol in (ol_l, ol_r):
            valid = [v for v in ol.values if v is not None]
            if valid:
                stats.append((ol.key, ol.name, ol.unit, ol.color,
                              valid, ol.timestamps))
        self._update_stats(stats)

    # -- legend & stats ---------------------------------------------------
    def _update_legend(self) -> None:
        # 图例与所在头部同底色：旧值 theme.BG（近黑）会与头部的 G_NEUTRAL 灰底冲突，
        # 在图例四周显出一圈更深的矩形色块，与主题不一致。
        bg = theme.GROUP_COLORS["G_NEUTRAL"]
        for w in self._legend_frame.winfo_children():
            w.destroy()
        for i, ol in enumerate(self._overlay):
            wrap = tk.Frame(self._legend_frame, bg=bg)
            wrap.pack(side="left", padx=(8 if i else 0, 0))
            swatch = tk.Frame(wrap, bg=ol.color, width=10, height=10)
            # 1×1 高光描边收一下锯齿，让色块边缘更干净。
            swatch.config(highlightthickness=0, borderwidth=0)
            swatch.pack(side="left", padx=(0, 4))
            # 不让子 Frame 随内容收缩，固定为 10×10 色块。
            swatch.pack_propagate(False)
            lbl = tk.Label(wrap, text=ol.name, fg=theme.FG,
                           bg=bg, font=theme.FONT_UI_SMALL)
            lbl.pack(side="left")

    def _update_stats(self, items: list[tuple]) -> None:
        """Update the statistics bar. Each item: (key, name, unit, color, values, timestamps)."""
        for w in self._stats_frame.winfo_children():
            w.destroy()
        if not items:
            tk.Label(self._stats_frame, text="暂无统计信息",
                     bg=theme.PANEL_2, fg=theme.MUTED,
                     font=theme.FONT_UI_SMALL).pack(anchor="w")
            return
        for key, name, _unit, color, vals, _ts in items:
            if len(vals) < 1:
                continue
            mean_v = statistics.mean(vals)
            median_v = statistics.median(vals)
            lo_v, hi_v = min(vals), max(vals)
            # Trend direction: compare first-half mean to second-half mean
            mid = len(vals) // 2
            if mid > 0:
                first_half = statistics.mean(vals[:mid])
                second_half = statistics.mean(vals[mid:])
                ratio = (second_half - first_half) / (abs(first_half) or 1)
                if ratio > 0.1:
                    trend = "↑ 上升"
                elif ratio < -0.1:
                    trend = "↓ 下降"
                else:
                    trend = "→ 平稳"
            else:
                trend = "—"
            # Moving average (last 3)
            ma3 = statistics.mean(vals[-3:]) if len(vals) >= 3 else mean_v

            text = (f"●{name}: "
                    f"均值{_fmt_num(mean_v)} · 中位{_fmt_num(median_v)} · "
                    f"{trend} · 近3期{_fmt_num(ma3)} · "
                    f"极值 {_fmt_num(lo_v)}~{_fmt_num(hi_v)}")
            lbl = tk.Label(self._stats_frame, text=text, bg=theme.PANEL_2,
                           fg=color, font=theme.FONT_UI_SMALL, anchor="w")
            lbl.pack(fill="x", anchor="w")



def _pearson_r(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient between two equal-length series."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / (n - 1))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / (n - 1))
    if sx == 0 or sy == 0:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)
    try:
        return cov / (sx * sy)
    except (ZeroDivisionError, ValueError):
        return 0.0


class ScatterChart:
    """Dual-metric scatter plot for correlation analysis.

    X-axis = metric A, Y-axis = metric B. Each dot = one session.
    Displays Pearson r value and optional regression line.
    """

    _PAD = 60

    def __init__(self, parent) -> None:
        self._reports: list = []
        self._frame = tk.Frame(parent, bg=theme.BG)
        self._frame.pack(fill="both", expand=True)
        self._tooltip = None
        self._point_coords: list[tuple[int, int, int]] = []  # (px, py, report_idx)
        self._resize_after: str | None = None
        self._build(self._frame)

    def _build(self, parent) -> None:
        ctrl = tk.Frame(parent, bg=theme.BG)
        ctrl.pack(fill="x", padx=6, pady=4)
        # X/Y 指标：选项用 metric_defs 的中文名（name+unit，与指标分类/趋势选择器一致）；
        # 选中后经 _label_to_key 映射回 key 供 raw_value 取数（key 仍是数据层唯一键）。
        self._label_to_key: dict[str, str] = {}
        _labels: list[str] = []
        for _g in GROUPS:
            for m in _g.metrics:
                if m.key in _NON_PLOTTABLE:
                    continue
                lbl = f"{m.name}（{m.unit}）" if m.unit else m.name
                self._label_to_key.setdefault(lbl, m.key)
                _labels.append(lbl)

        def _label_for(key: str) -> str:
            return next((l for l, k in self._label_to_key.items() if k == key), _labels[0])

        # X metric
        tk.Label(ctrl, text="X轴:", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_UI_SMALL).pack(side="left")
        self._x_var = tk.StringVar(value=_label_for("cost"))
        ttk.Combobox(ctrl, textvariable=self._x_var, width=18, state="readonly",
                     values=_labels).pack(side="left", padx=4)
        self._x_var.trace_add("write", lambda *_: self._on_change())
        # Y metric
        tk.Label(ctrl, text="Y轴:", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=(12, 0))
        self._y_var = tk.StringVar(value=_label_for("tcer"))
        ttk.Combobox(ctrl, textvariable=self._y_var, width=18, state="readonly",
                     values=_labels).pack(side="left", padx=4)
        self._y_var.trace_add("write", lambda *_: self._on_change())
        # Info
        self._info = tk.Label(ctrl, text="", bg=theme.BG, fg=theme.FG,
                              font=theme.FONT_UI)
        self._info.pack(side="right", padx=6)

        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._tooltip.hide())

    def _on_canvas_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def update(self, reports) -> None:
        self._reports = list(reports)
        self._draw()

    def _on_change(self) -> None:
        self._draw()

    def _on_motion(self, event) -> None:
        for px, py, ri in self._point_coords:
            if math.hypot(event.x - px, event.y - py) <= 8 and ri < len(self._reports):
                r = self._reports[ri]
                sid = r.meta.session_id or r.meta.path.stem
                xk = self._label_to_key.get(self._x_var.get(), "cost")
                yk = self._label_to_key.get(self._y_var.get(), "tcer")
                xm = _metric_by_key.get(xk)
                ym = _metric_by_key.get(yk)
                xn = xm.name if xm else xk
                yn = ym.name if ym else yk
                xv = metric_raw_value(r, xk)
                yv = metric_raw_value(r, yk)
                x_disp = TrendChart._fmt_metric(xk, xv, xm) if xv is not None else "?"
                y_disp = TrendChart._fmt_metric(yk, yv, ym) if yv is not None else "?"
                lines = [
                    f"会话: {sid[:20]}…",
                    f"{xn}: {x_disp}",
                    f"{yn}: {y_disp}",
                ]
                self._tooltip.show(event.x, event.y, lines,
                                   [theme.ACCENT, theme.FG, theme.FG])
                return
        self._tooltip.hide()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._point_coords = []
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return

        xk = self._label_to_key.get(self._x_var.get(), "cost")
        yk = self._label_to_key.get(self._y_var.get(), "tcer")
        xs, ys, ris = [], [], []
        for i, r in enumerate(self._reports):
            xv = metric_raw_value(r, xk)
            yv = metric_raw_value(r, yk)
            if xv is not None and yv is not None:
                xs.append(xv)
                ys.append(yv)
                ris.append(i)

        if len(xs) < 2:
            c.create_text(w / 2, h / 2, text="需要 ≥2 个有效数据点",
                          fill=theme.MUTED, font=theme.FONT_UI, justify="center")
            self._info.config(text="")
            return

        pad = self._PAD
        plot_w = w - pad * 2
        plot_h = h - pad * 2
        lo_x, hi_x = min(xs), max(xs)
        lo_y, hi_y = min(ys), max(ys)
        if hi_x - lo_x < 1e-12:
            lo_x -= 1; hi_x += 1
        if hi_y - lo_y < 1e-12:
            lo_y -= 1; hi_y += 1

        def xv(v):
            return pad + plot_w * (v - lo_x) / (hi_x - lo_x)

        def yv(v):
            return pad + plot_h * (1 - (v - lo_y) / (hi_y - lo_y))

        # Grid lines (Y)
        for tv in _nice_ticks(lo_y, hi_y, 4):
            ty = yv(tv)
            c.create_line(pad, ty, pad + plot_w, ty, fill="#2a2a2a", dash=(2, 4))
            c.create_text(pad - 6, ty, text=_fmt_num(tv), anchor="e",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        # Grid lines (X)
        for tv in _nice_ticks(lo_x, hi_x, 4):
            tx = xv(tv)
            c.create_line(tx, pad, tx, pad + plot_h, fill="#2a2a2a", dash=(2, 4))
            c.create_text(tx, pad + plot_h + 6, text=_fmt_num(tv), anchor="n",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL)

        # Axes
        c.create_line(pad, pad, pad, pad + plot_h, fill=theme.BORDER)
        c.create_line(pad, pad + plot_h, pad + plot_w, pad + plot_h, fill=theme.BORDER)

        # Dots（PIL 超采样抗锯齿）
        self._aa_imgs = []
        _items = []
        for xi, yi, ri in zip(xs, ys, ris):
            px, py = xv(xi), yv(yi)
            color = theme.ACCENT
            grade = self._reports[ri].grade
            if grade:
                color = theme.GRADE_HEX.get(grade, theme.ACCENT)
            _items.append(("dot", px, py, 4, color, theme.FG))
            self._point_coords.append((int(px), int(py), ri))
        _aa_layer(c, _items, self._aa_imgs)

        # Regression line
        r_val = _pearson_r(xs, ys)
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        ss_xx = sum((x - mx) ** 2 for x in xs)
        if ss_xx > 0:
            slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / ss_xx
            intercept = my - slope * mx
            x0, x1 = lo_x, hi_x
            y0, y1 = slope * x0 + intercept, slope * x1 + intercept
            c.create_line(xv(x0), yv(y0), xv(x1), yv(y1),
                          fill=theme.WARNING, width=1, dash=(4, 3))

        # Info
        if abs(r_val) >= 0.7:
            strength = "强"
        elif abs(r_val) >= 0.4:
            strength = "中等"
        elif abs(r_val) >= 0.2:
            strength = "弱"
        else:
            strength = "极弱/无"
        xn = _metric_by_key.get(xk, Metric(xk, xk, "", "", "basic")).name
        yn = _metric_by_key.get(yk, Metric(yk, yk, "", "", "basic")).name
        self._info.config(text=f"Pearson r = {r_val:.3f}（{strength}相关） · n={n}")

        # Axis labels
        c.create_text(pad + plot_w / 2, pad + plot_h + 24, text=xn,
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")
        c.create_text(8, pad + plot_h / 2, text=yn,
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w",
                      angle=90)

        # Title
        c.create_text(pad + plot_w / 2, 6,
                      text=f"{xn} vs {yn} 散点图 · r={r_val:.3f}",
                      fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="n")


class DashboardChart:
    """6-group sparkline dashboard — one representative metric per G-group.

    Each sparkline shows the trend of one metric across sessions, with
    min/max/mean annotations and trend direction arrows.
    """

    # One representative metric per G-group.
    _GROUP_METRICS = [
        ("G1", "turns"),
        ("G2", "total_tokens"),
        ("G3", "chr"),
        ("G4", "net_loc"),
        ("G5", "cost"),
        ("G6", "ctei"),
    ]

    # 按日聚合时可直接求和的代表指标；其余按日取均值。
    _ADDITIVE = frozenset({"turns", "total_tokens", "net_loc", "cost"})

    def __init__(self, parent) -> None:
        self._reports: list = []
        self._resize_after: str | None = None
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x")
        self._daily = tk.BooleanVar(value=False)
        CheckRow(
            bar, "按日聚合（会话密度不均时曲线更真实）", self._daily,
            on_toggle=self._draw, font=theme.FONT_UI_SMALL,
            tooltip="开启后按日聚合：会话密度不均时曲线更真实（计数/金额求和，比率求均值）",
        )
        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_configure)

    def _series(self, key: str) -> list[float | None]:
        """会话序列或按日聚合序列（计数/金额求和，比率求均值）。"""
        if not self._daily.get():
            return [metric_raw_value(r, key) for r in self._reports]
        from datetime import datetime
        buckets: dict[str, list[float]] = {}
        for r in self._reports:
            ts = r.usage.started_at
            v = metric_raw_value(r, key)
            if ts is None or v is None:
                continue
            day = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            buckets.setdefault(day, []).append(v)
        additive = key in self._ADDITIVE
        return [
            (sum(vs) if additive else sum(vs) / len(vs))
            for _day, vs in sorted(buckets.items())
        ]

    def _on_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def update(self, reports) -> None:
        self._reports = sorted(reports,
                               key=lambda r: r.usage.started_at or r.usage.ended_at or 0)
        self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        n_groups = len(self._GROUP_METRICS)
        cols = 3
        rows = (n_groups + cols - 1) // cols
        cell_w = w // cols
        cell_h = h // rows

        for gi, (gid, key) in enumerate(self._GROUP_METRICS):
            col = gi % cols
            row = gi // cols
            x0 = col * cell_w
            y0 = row * cell_h

            metric = _metric_by_key.get(key)
            name = metric.name if metric else key
            unit = metric.unit if metric else ""
            group_color = theme.GROUP_COLORS.get(gid, theme.PANEL)

            # Cell background
            c.create_rectangle(x0, y0, x0 + cell_w, y0 + cell_h,
                               fill=theme.PANEL_2, outline=theme.CONTROL_BG)

            # Header bar
            c.create_rectangle(x0, y0, x0 + cell_w, y0 + 20,
                               fill=group_color, outline="")
            label = name
            if unit:
                label += f"（{unit}）"
            c.create_text(x0 + 6, y0 + 10, text=label, fill=theme.FG,
                          font=theme.FONT_UI_SMALL_BOLD, anchor="w")

            # Extract values（会话序列或按日聚合，见 _series）
            vals = self._series(key)
            valid = [(i, v) for i, v in enumerate(vals) if v is not None]
            if len(valid) < 1:
                c.create_text(x0 + cell_w / 2, y0 + cell_h / 2,
                              text="无数据", fill=theme.MUTED,
                              font=theme.FONT_UI_SMALL)
                continue

            indices = [i for i, _ in valid]
            values = [v for _, v in valid]
            lo_v, hi_v = min(values), max(values)
            mean_v = statistics.mean(values)
            pad = 10
            plot_x = x0 + pad
            plot_w = cell_w - pad * 2
            plot_y = y0 + 24
            plot_h = cell_h - 44

            if hi_v - lo_v < 1e-12:
                hi_v = lo_v + 1

            def xv(i, _indices=indices, _plot_x=plot_x, _plot_w=plot_w, _n=len(vals)):
                return _plot_x + _plot_w * i / max(_n - 1, 1)

            def yv(v, _lo=lo_v, _hi=hi_v, _plot_y=plot_y, _plot_h=plot_h):
                return _plot_y + _plot_h * (1 - (v - _lo) / (_hi - _lo))

            # Mean line
            ym = yv(mean_v)
            c.create_line(plot_x, ym, plot_x + plot_w, ym,
                          fill="#444444", dash=(2, 3))

            # Sparkline（cell 极小，用 canvas 原生即可；PIL 抗锯齿留给大图）
            # 折线用分类色的提亮版（_DASHBOARD_LINE_COLORS）：原 GROUP_COLORS 偏暗，
            # 作细折线会灰蒙蒙；提亮后与表头同色相、按色识组，且在深色格上清晰。
            pts = [(xv(i), yv(v)) for i, v in valid]
            color = _DASHBOARD_LINE_COLORS.get(gid, group_color)
            if len(pts) >= 2:
                c.create_line(pts, fill=color, width=2, smooth=True)
            for px, py in pts:
                c.create_oval(px - 2, py - 2, px + 2, py + 2,
                              fill=color, outline="")

            # Min/Max markers — 用 max()/min() 原值定位索引：上方 hi_v 在「全相等」
            # 时会被改成 lo_v+1 防除零，此时 values.index(hi_v) 会抛 ValueError 中断绘制，
            # 表现为仪表盘整片空白。max()/min() 永远返回列表内的真实元素，查找必中。
            max_idx = indices[values.index(max(values))]
            min_idx = indices[values.index(min(values))]
            mx_px, mx_py = xv(max_idx), yv(hi_v)
            mn_px, mn_py = xv(min_idx), yv(lo_v)
            c.create_text(mx_px, mx_py - 6, text="▲", fill=theme.SUCCESS,
                          font=theme.FONT_UI_SMALL)
            c.create_text(mn_px, mn_py + 8, text="▼", fill=theme.ERROR,
                          font=theme.FONT_UI_SMALL)

            # Current value + trend arrow
            current = values[-1]
            if len(values) >= 2:
                prev_mean = statistics.mean(values[:len(values) // 2]) if len(values) > 2 else values[0]
                if current > prev_mean * 1.1:
                    trend = "↑"
                    trend_color = theme.SUCCESS
                elif current < prev_mean * 0.9:
                    trend = "↓"
                    trend_color = theme.ERROR
                else:
                    trend = "→"
                    trend_color = theme.MUTED
            else:
                trend = "—"
                trend_color = theme.MUTED

            footer_y = y0 + cell_h - 14
            c.create_text(x0 + 6, footer_y,
                          text=f"当前:{_fmt_num(current)} {trend}",
                          fill=trend_color, font=theme.FONT_UI_SMALL, anchor="w")
            c.create_text(x0 + cell_w - 6, footer_y,
                          text=f"均值:{_fmt_num(mean_v)}",
                          fill=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="e")


# ============================================================
# 时段效率分析 — 日历热力图（周×星期）/ 周×小时热力图（趋势页第 4 模式）
# ============================================================

class HeatmapChart:
    """按会话开始时间（本地时区）聚合的时段热力图，两种视图可切换：

    - 日历（GitHub 风）：列=周，行=星期，按天聚合；
    - 周×小时：列=0–23 时，行=星期，回答「一天中几点效率高」。

    单元格取所选指标在该时段全部会话上的均值（会话数=计数、成本/Token=合计），
    四分位分档取色；坏方向指标（sentiment="down"）走橙阶。悬停显示时段与数值，
    **点击格子下钻**该时段会话列表（FlatMenu → 跳转会话详情）。指标取值走
    metric_defs ``raw_value``（SSOT），与趋势/散点图完全一致。
    """

    _WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    # (下拉标签, metric key or None=会话数)
    _MODES = [("会话数", None), ("TCER 均值", "tcer"), ("综合效率分均值", "ctei"),
              ("成本合计", "cost"), ("总 Token 合计", "total_tokens"),
              ("返工率均值", "churn")]
    _SUM_KEYS = {"cost", "total_tokens"}  # 合计而非均值
    _PAD_L = 46
    _PAD_T = 26
    _PAD_B = 78          # 月份/小时标签 + 底部边际条
    _PAD_R = 14
    _MARGIN_R = 96       # 右侧星期边际条预留宽
    _MARGIN_B = 30       # 底部边际条高

    def __init__(self, parent, controller=None) -> None:
        self._reports: list = []
        self._controller = controller
        self._resize_after: str | None = None
        self._view = "calendar"          # "calendar" | "hours"

        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", pady=(2, 0))
        tk.Label(bar, text="指标:", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=(6, 2))
        self._mode_var = tk.StringVar(value="总 Token 合计")
        cb = ttk.Combobox(bar, textvariable=self._mode_var, state="readonly",
                          values=[label for label, _ in self._MODES], width=16)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._draw())

        # 视图切换（日历 / 周×小时）——§11 深色 pill 分段样式
        seg = tk.Frame(bar, bg=theme.CONTROL_BG, padx=2, pady=2)
        seg.pack(side="left", padx=(10, 0))
        self._view_pills: dict[str, tk.Label] = {}
        for label, val in (("日历", "calendar"), ("周×小时", "hours")):
            holder = tk.Frame(seg, bg=theme.CONTROL_BG)
            holder.pack(side="left", padx=1)
            lbl = tk.Label(holder, text=label, bg=theme.CONTROL_BG, fg=theme.MUTED,
                           cursor="hand2", font=theme.FONT_UI_SMALL, padx=6)
            lbl.pack()
            lbl.bind("<Button-1>", lambda _e, v=val: self._set_view(v))
            self._view_pills[val] = lbl
        self._update_view_pills()

        # 图例（固定工具栏右侧）：低→高色块 + 动态档位边界
        leg = tk.Frame(bar, bg=theme.BG)
        leg.pack(side="right", padx=(0, 10))
        tk.Label(leg, text="低", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left")
        self._legend_swatches: list[tk.Frame] = []
        for lvl in (theme.HEATMAP_EMPTY, *theme.HEATMAP_RAMP):
            sw = tk.Frame(leg, bg=lvl, width=11, height=11, bd=0)
            sw.pack(side="left", padx=1)
            sw.pack_propagate(False)
            self._legend_swatches.append(sw)
        tk.Label(leg, text="高", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left")
        self._legend_var = tk.StringVar(value="")
        tk.Label(leg, textvariable=self._legend_var, bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_UI_SMALL).pack(side="left", padx=(6, 0))

        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self._hbar = ttk.Scrollbar(parent, orient="horizontal",
                                   command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self._hbar.set)
        self._hbar_visible = False
        self._tooltip = _ChartTooltip(self.canvas)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Leave>", lambda e: self._tooltip.hide())
        self.canvas.bind("<Destroy>", lambda e: self._tooltip.hide())
        self._cells: dict = {}
        self._start = None

    def update(self, reports) -> None:
        self._reports = list(reports)
        self._draw()

    # -- view toggle --------------------------------------------------------
    def _set_view(self, val: str) -> None:
        if self._view == val:
            return
        self._view = val
        self._update_view_pills()
        self._draw()

    def _update_view_pills(self) -> None:
        for val, lbl in self._view_pills.items():
            sel = val == self._view
            lbl.config(bg=theme.ACCENT if sel else theme.CONTROL_BG,
                       fg=theme.FG_WHITE if sel else theme.MUTED)

    # -- data ---------------------------------------------------------------
    def _on_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.canvas.after_cancel(self._resize_after)
        self._resize_after = self.canvas.after(120, self._draw)

    def _metric_key(self) -> str | None:
        label = self._mode_var.get()
        for lbl, key in self._MODES:
            if lbl == label:
                return key
        return None

    def _ramp(self):
        """当前指标的色阶：down 型（越低越好，如返工率）→ 橙阶，其余蓝阶。"""
        key = self._metric_key()
        m = _metric_by_key.get(key) if key else None
        if m is not None and m.sentiment == "down":
            return theme.HEATMAP_RAMP_BAD
        return theme.HEATMAP_RAMP

    def _iter_sessions(self):
        """yield (report, local datetime)——所有带时间戳的会话。"""
        from datetime import datetime
        for r in self._reports:
            ts = r.usage.started_at
            if ts:
                yield r, datetime.fromtimestamp(ts / 1000)

    def _aggregate(self, bucket_of) -> dict:
        """通用聚合：bucket_of(dt)→桶键；返回 桶键→(会话数, 聚合值 or None)。"""
        key = self._metric_key()
        counts: dict = {}
        sums: dict = {}
        nvals: dict = {}
        for r, dt in self._iter_sessions():
            b = bucket_of(dt)
            counts[b] = counts.get(b, 0) + 1
            if key is not None:
                v = raw_value(r, key)
                if v is not None:
                    sums[b] = sums.get(b, 0.0) + v
                    nvals[b] = nvals.get(b, 0) + 1
        out: dict = {}
        for b, n in counts.items():
            if key is None:
                out[b] = (n, float(n))
            elif nvals.get(b):
                agg = sums[b]
                if key not in self._SUM_KEYS:
                    agg /= nvals[b]
                out[b] = (n, agg)
            else:
                out[b] = (n, None)
        return out

    def _date_range(self):
        from datetime import timedelta
        dates = [d for d, (n, _v) in self._cells.items() if n > 0]
        if not dates:
            return None, 0
        dmin, dmax = min(dates), max(dates)
        start = dmin - timedelta(days=dmin.weekday())
        return start, (dmax - start).days // 7 + 1

    def _set_quartiles(self, vals: list) -> None:
        """四分位分档 + 同步图例（档位边界 + 色阶换色）。"""
        vals = sorted(vals)
        if vals:
            n = len(vals)
            self._q = tuple(vals[min(n - 1, int(q * n))] for q in (0.25, 0.5, 0.75))
        else:
            self._q = (0.0, 0.0, 0.0)
        ramp = self._ramp()
        for sw, color in zip(self._legend_swatches, (theme.HEATMAP_EMPTY, *ramp)):
            sw.config(bg=color)
        if vals and self._q[0] != self._q[2]:
            a, b, cc = (_fmt_num(x) for x in self._q)
            self._legend_var.set(f"≤{a} · ≤{b} · ≤{cc} · >{cc}")
        else:
            self._legend_var.set("")

    def _bin_color(self, v: float) -> str:
        ramp = self._ramp()
        q1, q2, q3 = self._q
        if q1 == q3:                 # 全部同值：取次亮档保证可见
            return ramp[2]
        if v <= q1:
            return ramp[0]
        if v <= q2:
            return ramp[1]
        if v <= q3:
            return ramp[2]
        return ramp[3]

    # -- draw ---------------------------------------------------------------
    def _draw(self) -> None:
        c = self.canvas
        if not c.winfo_exists():
            return
        c.delete("all")
        self._tooltip.hide()
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        if self._view == "hours":
            self._draw_hours(w, h)
        else:
            self._draw_calendar(w, h)

    def _no_data(self, w, h) -> None:
        self.canvas.create_text(w / 2, h / 2, text="无带时间戳的会话数据",
                                fill=theme.MUTED, font=theme.FONT_UI)

    def _weekday_rows(self, ox, oy, step, gap, grid_w) -> None:
        """星期标签 + 周末分隔线（周五/周六之间一条细线，工作日/周末节奏）。"""
        c = self.canvas
        for d in range(7):
            c.create_text(ox - 6, oy + (d + 0.5) * step - gap / 2,
                          text=self._WEEKDAYS[d], fill=theme.MUTED,
                          font=theme.FONT_UI_SMALL, anchor="e")
        y_sep = oy + 5 * step - gap / 2
        c.create_line(ox, y_sep, ox + grid_w, y_sep, fill=theme.BORDER)

    def _margin_bars_right(self, per_row: dict, ox, oy, step, grid_w) -> None:
        """右侧星期边际条：一眼看出「周几最高」。"""
        c = self.canvas
        vals = [v for v in per_row.values() if v is not None]
        if not vals:
            return
        vmax = max(vals) or 1.0
        bar_color = self._ramp()[2]
        x0 = ox + grid_w + 12
        for d in range(7):
            v = per_row.get(d)
            if v is None:
                continue
            bw = max(2, (v / vmax) * (self._MARGIN_R - 40))
            yc = oy + (d + 0.5) * step
            c.create_rectangle(x0, yc - 4, x0 + bw, yc + 4,
                               fill=bar_color, outline="")
            c.create_text(x0 + bw + 4, yc, text=_fmt_num(v), fill=theme.MUTED,
                          font=theme.FONT_UI_SMALL, anchor="w")

    def _margin_bars_bottom(self, per_col: dict, n_cols, ox, y_top, step) -> None:
        """底部边际条：日历视图=每周合计，小时视图=每小时合计。"""
        c = self.canvas
        vals = [v for v in per_col.values() if v is not None]
        if not vals:
            return
        vmax = max(vals) or 1.0
        bar_color = self._ramp()[1]
        hmax = self._MARGIN_B - 6
        for col in range(n_cols):
            v = per_col.get(col)
            if v is None:
                continue
            bh = max(2, (v / vmax) * hmax)
            x0 = ox + col * step
            c.create_rectangle(x0, y_top + hmax - bh,
                               x0 + max(4, step - 8), y_top + hmax,
                               fill=bar_color, outline="")

    def _draw_calendar(self, w, h) -> None:
        from datetime import date, timedelta
        c = self.canvas
        self._cells = self._aggregate(lambda dt: dt.date())
        start, n_weeks = self._date_range()
        self._start = start
        if start is None or n_weeks == 0:
            self._no_data(w, h)
            return
        gap = 3
        avail_w = w - self._PAD_L - self._PAD_R - self._MARGIN_R
        avail_h0 = h - self._PAD_T - self._PAD_B
        fit = min((avail_w + gap) / max(n_weeks, 1), (avail_h0 + gap) / 7) - gap
        cell = int(max(22, min(44, fit)))
        step = cell + gap
        grid_w = step * n_weeks - gap
        need_scroll = grid_w > avail_w
        self._toggle_hbar(need_scroll)
        c.update_idletasks()
        h2 = c.winfo_height()
        avail_h = h2 - self._PAD_T - self._PAD_B
        oy = self._PAD_T + max(0, (avail_h - (step * 7 - gap)) / 2)
        ox = self._PAD_L + max(0, avail_w - grid_w) / 2
        self._cell, self._step, self._ox, self._oy = cell, step, ox, oy
        total_w = (self._PAD_L + grid_w + self._MARGIN_R + self._PAD_R
                   if need_scroll else w)
        c.configure(scrollregion=(0, 0, total_w, h2))

        self._set_quartiles([v for _n, v in self._cells.values() if v is not None])
        self._weekday_rows(ox, oy, step, gap, grid_w)

        # 月份标签：碰撞时删「前一个」（月末部分月），年份信息随删则转移给新标签
        last_ym = None
        prev = None
        for col in range(n_weeks):
            d0 = start + timedelta(weeks=col)
            ym = d0.year * 12 + d0.month
            if ym != last_ym:
                with_year = last_ym is None or last_ym // 12 != d0.year
                label = (f"{d0.year % 100:02d}年{d0.month}月" if with_year
                         else f"{d0.month}月")
                tid = c.create_text(ox + col * step, oy + 7 * step + 2,
                                    text=label, fill=theme.MUTED,
                                    font=theme.FONT_UI_SMALL, anchor="w")
                bbox = c.bbox(tid)
                if prev and bbox and bbox[0] < prev[1][2] + 8:
                    c.delete(prev[0])
                    if prev[2] and not with_year:
                        c.itemconfigure(
                            tid, text=f"{d0.year % 100:02d}年{d0.month}月")
                        bbox = c.bbox(tid)
                        with_year = True
                prev = (tid, bbox, with_year) if bbox else prev
                last_ym = ym

        today = date.today()
        for col in range(n_weeks):
            for d in range(7):
                day = start + timedelta(weeks=col, days=d)
                cell_v = self._cells.get(day)
                if cell_v is None or cell_v[0] == 0 or cell_v[1] is None:
                    fill = theme.HEATMAP_EMPTY
                else:
                    fill = self._bin_color(cell_v[1])
                x0 = ox + col * step
                y0 = oy + d * step
                if day == today:      # 今日格白描边定位「现在」
                    c.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill=fill,
                                       outline=theme.FG_WHITE, width=1)
                else:
                    c.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                       fill=fill, outline="")

        # 边际汇总：右=星期几合计，底=每周合计
        per_row = self._aggregate(lambda dt: dt.weekday())
        self._margin_bars_right({d: v for d, (_n, v) in per_row.items()},
                                ox, oy, step, grid_w)
        per_week = self._aggregate(
            lambda dt: (dt.date() - timedelta(days=dt.weekday()) - start).days // 7)
        self._margin_bars_bottom({col: v for col, (_n, v) in per_week.items()},
                                 n_weeks, ox, oy + 7 * step + 16, step)

    def _draw_hours(self, w, h) -> None:
        c = self.canvas
        self._start = None
        self._cells = self._aggregate(lambda dt: (dt.weekday(), dt.hour))
        if not self._cells:
            self._no_data(w, h)
            return
        self._toggle_hbar(False)
        gap = 3
        avail_w = w - self._PAD_L - self._PAD_R - self._MARGIN_R
        avail_h = h - self._PAD_T - self._PAD_B
        fit = min((avail_w + gap) / 24, (avail_h + gap) / 7) - gap
        cell = int(max(10, min(44, fit)))
        step = cell + gap
        grid_w = step * 24 - gap
        oy = self._PAD_T + max(0, (avail_h - (step * 7 - gap)) / 2)
        ox = self._PAD_L + max(0, avail_w - grid_w) / 2
        self._cell, self._step, self._ox, self._oy = cell, step, ox, oy
        c.configure(scrollregion=(0, 0, w, h))

        self._set_quartiles([v for _n, v in self._cells.values() if v is not None])
        self._weekday_rows(ox, oy, step, gap, grid_w)
        for hr in range(0, 24, 3):    # X 轴：每 3 小时标一次
            c.create_text(ox + hr * step + cell / 2, oy + 7 * step + 2,
                          text=f"{hr:02d}", fill=theme.MUTED,
                          font=theme.FONT_UI_SMALL, anchor="n")
        for d in range(7):
            for hr in range(24):
                cell_v = self._cells.get((d, hr))
                if cell_v is None or cell_v[0] == 0 or cell_v[1] is None:
                    fill = theme.HEATMAP_EMPTY
                else:
                    fill = self._bin_color(cell_v[1])
                x0 = ox + hr * step
                y0 = oy + d * step
                c.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                   fill=fill, outline="")

        per_row = self._aggregate(lambda dt: dt.weekday())
        self._margin_bars_right({d: v for d, (_n, v) in per_row.items()},
                                ox, oy, step, grid_w)
        per_hour = self._aggregate(lambda dt: dt.hour)
        self._margin_bars_bottom({hr: v for hr, (_n, v) in per_hour.items()},
                                 24, ox, oy + 7 * step + 18, step)

    def _toggle_hbar(self, show: bool) -> None:
        if show and not self._hbar_visible:
            self._hbar.pack(side="bottom", fill="x")
            self._hbar_visible = True
        elif not show and self._hbar_visible:
            self._hbar.pack_forget()
            self._hbar_visible = False

    # -- interaction --------------------------------------------------------
    def _hit_bucket(self, event):
        """事件坐标 → 桶键（日历=date，小时=(weekday,hour)）；未命中 None。"""
        from datetime import timedelta
        if not self._cells:
            return None
        x = self.canvas.canvasx(event.x)
        step, ox, oy = self._step, self._ox, self._oy
        col = int((x - ox) // step)
        d = int((event.y - oy) // step)
        if not (0 <= d < 7) or col < 0:
            return None
        if self._view == "hours":
            return (d, col) if col < 24 else None
        if self._start is None:
            return None
        return self._start + timedelta(weeks=col, days=d)

    def _bucket_label(self, bucket) -> str:
        if self._view == "hours":
            d, hr = bucket
            return f"{self._WEEKDAYS[d]} {hr:02d}:00–{hr + 1:02d}:00"
        return f"{bucket.strftime('%Y-%m-%d')} · {self._WEEKDAYS[bucket.weekday()]}"

    def _on_motion(self, event) -> None:
        bucket = self._hit_bucket(event)
        cell_v = self._cells.get(bucket) if bucket is not None else None
        if cell_v is None:
            self._tooltip.hide()
            return
        n, v = cell_v
        key = self._metric_key()
        if key is None or v is None:
            detail = f"{n} 个会话"
        else:
            detail = (f"{n} 个会话 · {self._mode_var.get()} "
                      f"{format_plot(key, v, _metric_by_key.get(key))}")
        self._tooltip.show(event.x, event.y,
                           [self._bucket_label(bucket), detail, "点击查看会话"])

    def _sessions_in(self, bucket) -> list:
        out = []
        for r, dt in self._iter_sessions():
            if self._view == "hours":
                hit = (dt.weekday(), dt.hour) == bucket
            else:
                hit = dt.date() == bucket
            if hit:
                out.append(r)
        return out

    def _on_click(self, event) -> None:
        """点击格子下钻：FlatMenu 列出该时段会话，选中跳转会话详情。"""
        bucket = self._hit_bucket(event)
        if bucket is None or self._controller is None:
            return
        sessions = self._sessions_in(bucket)
        if not sessions:
            return
        self._tooltip.hide()
        menu = FlatMenu(self.canvas)
        menu.add_command(label=self._bucket_label(bucket), state="disabled")
        menu.add_separator()
        MAX = 15
        for r in sessions[:MAX]:
            sid = r.meta.session_id or r.meta.path.stem
            title = r.meta.title or sid
            if len(title) > 36:
                title = title[:36] + "…"
            menu.add_command(
                label=title,
                command=lambda s=sid: self._controller.on_select_session(s))
        if len(sessions) > MAX:
            menu.add_command(label=f"…共 {len(sessions)} 个会话", state="disabled")
        menu.tk_popup(event.x_root, event.y_root)
