"""Reusable Tk widgets for the TCER GUI: Tooltip, ScrollFrame, Card, MetricCell.

Dumb, data-free components — they render what they're given and emit callbacks.
Selection state and data live in ``app`` / ``views``. Importing this module
imports tkinter (only happens when the GUI actually launches).
"""
from __future__ import annotations

import tkinter as tk

from . import theme
from .metric_defs import Metric, UNSUPPORTED_LABEL
from .platform import PLATFORM, CLICK_CURSOR


class Tooltip:
    """Lightweight hover tooltip for any widget (stdlib only)."""

    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip = None
        self.bind_widget(widget)
        try:
            widget.bind("<Destroy>", lambda _e: self._on_destroy(), add="+")
        except tk.TclError:
            pass

    def _on_destroy(self) -> None:
        self._cancel_pending()
        self._hide()

    def bind_widget(self, w) -> None:
        """将同一个 Tooltip 实例绑定到多个子部件（如容器 Frame 与其内部 Label）。"""
        w.bind("<Enter>", self._show, add="+")
        w.bind("<Leave>", self._hide, add="+")
        w.bind("<Button-1>", self._hide, add="+")  # 点击即隐，避免遮挡后续 UI

    # VS Code 式 hover 延迟（workbench.hover.delay 默认 300ms）：
    # 立即弹出会在扫读列表时闪烁（尤其多行 tooltip）。
    _DELAY_MS = 300

    def _show(self, _event=None) -> None:
        self._cancel_pending()  # 重入（在绑定链上滑过多个子件）不叠加定时器
        if self.tip or not self.text:
            return
        try:
            widget = self.widget
            self._after_id = widget.after(self._DELAY_MS, self._spawn)
        except tk.TclError:
            pass  # widget 已销毁

    def _cancel_pending(self) -> None:
        aid = getattr(self, "_after_id", None)
        if aid is not None:
            try:
                self.widget.after_cancel(aid)
            except tk.TclError:
                pass
            self._after_id = None

    def _spawn(self) -> None:
        self._after_id = None
        if self.tip or not self.text:
            return
        try:
            if not self.widget.winfo_exists():
                return
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg=theme.BORDER)  # 外层露 1px 作边框（深色主题）
        lbl = tk.Label(self.tip, text=self.text, justify="left",
                       bg=theme.PANEL_2, fg=theme.FG,
                       wraplength=460, font=theme.FONT_UI, padx=8, pady=5)
        lbl.pack(padx=1, pady=1)  # 1px 边框 = Toplevel(bg=BORDER) 透出
        self.tip.update_idletasks()
        tip_w = max(160, self.tip.winfo_reqwidth())
        tip_h = max(40, self.tip.winfo_reqheight())

        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        from .platform import get_monitor_work_area
        m_left, m_top, m_right, m_bottom = get_monitor_work_area(self.widget)

        # 屏幕右边缘避让：若向右展开越界，则翻转与 widget 右边缘对齐向左展开
        if x + tip_w > m_right - 10:
            x = self.widget.winfo_rootx() + self.widget.winfo_width() - tip_w
        if x < m_left + 4:
            x = m_left + 4
        # 屏幕下边缘避让：若向下展开越界，则翻转至 widget 上方
        if y + tip_h > m_bottom - 10:
            y = self.widget.winfo_rooty() - tip_h - 6
        if y < m_top + 4:
            y = m_top + 4
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        self._cancel_pending()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class CheckRow:
    """深色扁平勾选行：整行点击 toggle，选中=整行高亮（淡蓝底 + 白字），
    未选=普通行。**无传统 checkbox 方块**——靠行背景表达选中，现代一体，不再有
    「前面的方框与文字割裂」的老式感。

    ``var`` 为 BooleanVar；``on_toggle`` 在切换后回调（调用方 _redraw 统一刷新）。
    可选 ``icon``（文字左）、``hint``（文字右，淡色说明）。
    """

    _SEL_BG = theme.SEL_ROW_BG   # 选中行底色（淡蓝；多选多行高亮不刺眼）

    def __init__(self, parent, text, var, on_toggle=None, *, tooltip=None,
                 font=None, icon=None, hint=None) -> None:
        self.var = var
        self._on_toggle = on_toggle
        self._row = tk.Frame(parent, bg=theme.PANEL, cursor=CLICK_CURSOR)
        self._row.pack(fill="x", padx=2)
        self._members: list = []
        if icon is not None:
            il = tk.Label(self._row, image=icon, bg=theme.PANEL)
            il.pack(side="left", padx=(8, 4))
            self._members.append(il)
        if hint:
            hl = tk.Label(self._row, text=hint, bg=theme.PANEL, fg=theme.MUTED,
                          font=theme.FONT_UI_SMALL, anchor="e")
            hl.pack(side="right", padx=(4, 8))  # 先 pack 右侧，标题 expand 才不会挤掉它
            self._members.append(hl)
        self._lbl = tk.Label(self._row, text=text, bg=theme.PANEL, fg=theme.FG,
                             font=font or theme.FONT_UI, anchor="w")
        self._lbl.pack(side="left", fill="x", expand=True)
        self._members.append(self._lbl)
        self._apply()
        for w in (self._row, *self._members):
            w.bind("<Button-1>", lambda e: self.click(), add="+")
        self._row.bind("<Enter>", self._on_hover, add="+")
        self._row.bind("<Leave>", self._on_leave, add="+")
        if tooltip:
            for w in (self._row, self._lbl):
                Tooltip(w, tooltip)

    def click(self) -> None:
        """切换 var 并回调；不自行刷新（由调用方 _redraw 统一刷新所有行）。"""
        self.var.set(not self.var.get())
        if self._on_toggle:
            self._on_toggle()

    def _apply(self) -> None:
        on = self.var.get()
        bg = self._SEL_BG if on else theme.PANEL
        self._row.config(bg=bg)
        for w in self._members:
            try:
                w.config(bg=bg)
            except tk.TclError:
                pass
        self._lbl.config(fg=theme.FG_WHITE if on else theme.FG)

    def _on_hover(self, _e=None) -> None:
        if self.var.get():
            return  # 选中态保持高亮，不被 hover 覆盖
        bg = theme.HOVER_BG
        self._row.config(bg=bg)
        for w in self._members:
            try:
                w.config(bg=bg)
            except tk.TclError:
                pass

    def _on_leave(self, e=None) -> None:
        if e is not None:
            try:
                under = e.widget.winfo_containing(e.x_root, e.y_root)
                curr = under
                while curr is not None:
                    if curr == self._row:
                        return  # 鼠标仍在行内，不触发离开
                    curr = getattr(curr, "master", None)
            except Exception:
                pass
        self._apply()

    def _draw(self) -> None:
        """外部改 var 后刷新（兼容旧接口名，等价 _apply）。"""
        self._apply()


class AccordionSection:
    """Monokai Dimmed 手风琴折叠区块。

    表头高度固定 28px，底色 theme.CARD_HEADER_BG / theme.ACTIVITY_BG (#353535)；
    左侧折叠箭头（▾ / ▸），中间标题，右侧数量/操作胶囊；
    content 容器自动展开/折叠。
    """

    def __init__(self, parent, title: str, color: str | None = None, *,
                 badge: str = "", expand: bool = True, default_open: bool = True,
                 on_toggle=None) -> None:
        self._title = title
        self._badge = badge
        self._expand = expand
        self._collapsed = not default_open
        self._on_toggle = on_toggle
        self._color = color or theme.CARD_HEADER_BG
        self._header_bg = theme.PANEL_2
        rail_col = color if (color and color not in (theme.CARD_HEADER_BG, theme.BG)) else theme.BORDER

        self.frame = tk.Frame(parent, bg=theme.BG)
        self.frame.pack(fill="both" if expand else "x", expand=expand)

        # 26px 固定高度手风琴表头
        self.header = tk.Frame(self.frame, bg=self._header_bg, height=26, cursor=CLICK_CURSOR)
        self.header.pack(fill="x", pady=(1, 0))
        self.header.pack_propagate(False)

        # 3px 左侧强调色条
        self.rail = tk.Frame(self.header, bg=rail_col, width=3)
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)

        arrow_char = "▾" if default_open else "▸"
        self._arrow = tk.Label(self.header, text=arrow_char, bg=self._header_bg, fg=theme.FG_WHITE,
                               font=theme.FONT_UI_BOLD, cursor=CLICK_CURSOR, padx=4)
        self._arrow.pack(side="left", padx=(2, 2))

        self._title_lbl = tk.Label(self.header, text=title, bg=self._header_bg, fg=theme.FG_WHITE,
                                   font=theme.FONT_UI_BOLD, anchor="w", cursor=CLICK_CURSOR)
        self._title_lbl.pack(side="left", fill="x", expand=True)

        self._badge_lbl = tk.Label(self.header, text=badge, bg=self._header_bg, fg=theme.MUTED,
                                   font=theme.FONT_UI_SMALL, anchor="e", cursor=CLICK_CURSOR)
        if badge:
            self._badge_lbl.pack(side="right", padx=(0, 8))

        self.content = tk.Frame(self.frame, bg=theme.BG)
        if default_open:
            self.content.pack(fill="both" if expand else "x", expand=expand)

        for w in (self.header, self._arrow, self._title_lbl, self._badge_lbl):
            w.bind("<Button-1>", lambda e: self.toggle(), add="+")
            w.bind("<Enter>", lambda _e: self._on_header_hover(True), add="+")
            w.bind("<Leave>", lambda e: self._on_header_hover(False, e), add="+")

    def _on_header_hover(self, is_hover: bool, event=None) -> None:
        if not is_hover and event is not None:
            try:
                under = event.widget.winfo_containing(event.x_root, event.y_root)
                curr = under
                while curr is not None:
                    if curr == self.header:
                        return  # 仍在 header 内部，不触发取消
                    curr = getattr(curr, "master", None)
            except Exception:
                pass
        bg = theme.HOVER_BG if is_hover else self._header_bg
        self.header.configure(bg=bg)
        self._arrow.configure(bg=bg)
        self._title_lbl.configure(bg=bg)
        self._badge_lbl.configure(bg=bg)
    def set_title(self, title: str) -> None:
        """更新标题文字（保留当前折叠状态）。"""
        self._title = title
        self._title_lbl.configure(text=title)

    def set_badge(self, badge: str) -> None:
        """更新右侧徽标/数量胶囊。"""
        self._badge = badge
        self._badge_lbl.configure(text=badge)
        if badge and not self._badge_lbl.winfo_ismapped():
            self._badge_lbl.pack(side="right", padx=(0, 8))
        elif not badge and self._badge_lbl.winfo_ismapped():
            self._badge_lbl.pack_forget()

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        arrow_char = "▸" if self._collapsed else "▾"
        self._arrow.configure(text=arrow_char)
        if self._collapsed:
            self.content.pack_forget()
        else:
            self.content.pack(fill="both" if self._expand else "x",
                              expand=self._expand, after=self.header)
        if self._on_toggle:
            self._on_toggle(not self._collapsed)


# 兼容别名
CollapsibleSection = AccordionSection

class ScrollFrame:
    """A scrolled container. Pack children into ``self.inner``.

    Encapsulates the Canvas + mousewheel-on-enter/leave pattern that the old
    monolith duplicated for the project list, session list, and tool popup.
    """

    def __init__(self, parent, bg: str = theme.PANEL) -> None:
        from tkinter import ttk as _ttk

        self.canvas = tk.Canvas(parent, bg=bg, highlightthickness=0)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._reset_pending = False
        # 常驻极简滚动条：始终显示（内容未占满时滑块满槽=到底了），不再时隐时现。
        self.vbar = _ttk.Scrollbar(parent, orient="vertical",
                                   command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll_set)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_resize)
        self._unbind_wheel = None
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

    def _on_scroll_set(self, first, last) -> None:
        # 常驻：只更新滑块位置/长度，不再按需显隐。
        self.vbar.set(first, last)

    def _apply_scrollregion(self) -> None:
        """设 scrollregion；内容高仅略超 canvas（<=6px 容差）时钳到 canvas 高，
        yview 自然 (0,1) 锁定，避免滑块满槽却能滚几像素、把卡片带偏。"""
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        x0, y0, x1, y1 = bbox
        ch = self.canvas.winfo_height()
        if ch > 1 and (y1 - y0) <= ch + 6:
            y1 = y0 + ch  # scrollregion 高钳到 canvas 高 → yview (0,1) 锁定
        self.canvas.configure(scrollregion=(x0, y0, x1, y1))

    def _on_resize(self, event) -> None:
        self.canvas.itemconfig(self._win, width=event.width)

    def _on_inner_configure(self, _event=None) -> None:
        self._apply_scrollregion()
        if self._reset_pending:
            self.canvas.yview_moveto(0)

    def _on_enter(self, _event=None) -> None:
        from .platform import bind_mousewheel
        self._unbind_wheel = bind_mousewheel(self.canvas, self._wheel_scroll)

    def _wheel_scroll(self, units) -> None:
        # 内容未溢出时不滚，避免「没占满还能滑出空白」。
        first, last = self.canvas.yview()
        if last - first >= 1.0:
            return
        self.canvas.yview_scroll(units, "units")

    def _on_leave(self, _event=None) -> None:
        # Tk 陷阱：指针移入嵌入子窗（inner 里的卡片）时 canvas 也收到 <Leave>。
        # 只有指针真的离开 canvas 矩形才解绑滚轮，否则卡片正上方滚轮会失效。
        try:
            px, py = self.canvas.winfo_pointerx(), self.canvas.winfo_pointery()
            cx, cy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
            if cx <= px < cx + self.canvas.winfo_width() \
                    and cy <= py < cy + self.canvas.winfo_height():
                return
        except tk.TclError:
            pass
        if self._unbind_wheel:
            self._unbind_wheel()
            self._unbind_wheel = None
    def update_scroll(self, *, reset: bool = False) -> None:
        self._reset_pending = reset
        self.inner.update_idletasks()
        self._apply_scrollregion()
        if reset:
            self.canvas.yview_moveto(0)
            self.canvas.after_idle(self._finish_reset)

    def _finish_reset(self) -> None:
        self._apply_scrollregion()
        self.canvas.yview_moveto(0)
        self._reset_pending = False


class Card:
    """A selectable list card with solid elevation, state rail, and hover feedback.

    Build content into ``self.frame``; register any child widget that should
    also trigger selection and match hover/selection styling via ``bind_to``.
    """

    def __init__(self, parent, on_click, on_right_click=None,
                 bg: str = theme.PANEL_2, padx: int = 4, pady: int = 2, radius: int = 5) -> None:
        self._bg = bg
        self._parent_bg = parent.cget("bg") if hasattr(parent, "cget") else theme.PANEL
        self._on_click = on_click
        self._on_right_click = on_right_click
        self._selected = False
        self._hovered = False
        self._state_rail_color: str | None = None
        self._registered_widgets: list[tk.Widget] = []
        self._label_fgs: dict[tk.Widget, str] = {}
        self._radius = radius

        # 自绘抗锯齿圆角底 Canvas 替代生硬直角
        self.frame = tk.Canvas(parent, bg=self._parent_bg, highlightthickness=0, bd=0, cursor=CLICK_CURSOR)
        self.frame.pack(fill="x", padx=padx, pady=pady)
        self.frame.bind("<Configure>", self._redraw_bg)

        # 3px 垂直状态高光条 (State Rail)
        self.rail = tk.Frame(self.frame, width=theme.RAIL_W, bg=bg)
        self.rail.pack(side="left", fill="y", padx=(2, 0), pady=3)
        self.rail.pack_propagate(False)

        self.frame.bind("<Button-1>", lambda e: on_click(self))
        self.frame.bind("<Enter>", self._on_hover, add="+")
        self.frame.bind("<Leave>", self._on_unhover, add="+")
        if on_right_click:
            self.frame.bind("<Button-3>", on_right_click)

    def _redraw_bg(self, _event=None) -> None:
        w = self.frame.winfo_width()
        h = self.frame.winfo_height()
        if w < 10 or h < 10:
            return
        self.frame.delete("card_bg")
        fill_col = theme.SEL_ROW_ACTIVE if self._selected else (theme.HOVER_BG if self._hovered else self._bg)
        self._bg_img = get_rounded_rect_img(self.frame, w, h, self._radius, fill_col, self._parent_bg)
        if self._bg_img is not None:
            self.frame.create_image(0, 0, anchor="nw", image=self._bg_img, tags="card_bg")
        else:
            self.frame.create_rectangle(0, 0, w, h, fill=fill_col, outline="", tags="card_bg")
        self.frame.tag_lower("card_bg")

    def set_state_rail(self, color: str | None) -> None:
        """设置左侧状态条颜色（例如收敛向心绿、逃逸橙红等）。"""
        self._state_rail_color = color
        rail_col = color if color else (theme.HOVER_BG if self._hovered else self._bg)
        if self._selected:
            rail_col = color or theme.ACCENT
        self.rail.configure(bg=rail_col)

    def _on_hover(self, _e=None) -> None:
        self._hovered = True
        if not self._selected:
            self._apply_bg(theme.HOVER_BG)

    def _on_unhover(self, e=None) -> None:
        if e is not None:
            try:
                under = e.widget.winfo_containing(e.x_root, e.y_root)
                curr = under
                while curr is not None:
                    if curr == self.frame:
                        return  # 鼠标仍在卡片范围内，不触发 unhover
                    curr = getattr(curr, "master", None)
            except Exception:
                pass
        self._hovered = False
        if not self._selected:
            self._apply_bg(self._bg)

    def _apply_bg(self, bg_color: str) -> None:
        self._redraw_bg()
        rail_col = self._state_rail_color or (theme.ACCENT if self._selected else bg_color)
        try:
            self.rail.configure(bg=rail_col)
        except tk.TclError:
            pass
        for w in self._registered_widgets:
            try:
                w.configure(bg=bg_color)
            except tk.TclError:
                pass

    def bind_to(self, widget) -> tk.Widget:
        """Register a child widget to share click, hover, and selection styles."""
        self._registered_widgets.append(widget)
        if isinstance(widget, tk.Label):
            try:
                self._label_fgs[widget] = widget.cget("fg")
            except tk.TclError:
                pass
        curr_bg = theme.SEL_ROW_ACTIVE if self._selected else (theme.HOVER_BG if self._hovered else self._bg)
        try:
            widget.configure(bg=curr_bg)
        except tk.TclError:
            pass
        if self._selected and isinstance(widget, tk.Label):
            try:
                orig_fg = self._label_fgs.get(widget, "")
                if orig_fg in (theme.FG, theme.MUTED):
                    widget.configure(fg=theme.FG_WHITE)
            except tk.TclError:
                pass
        widget.bind("<Button-1>", lambda e: self._on_click(self), add="+")
        if self._on_right_click:
            widget.bind("<Button-3>", self._on_right_click, add="+")
        return widget

    def track_bg(self, widget) -> tk.Widget:
        """Register a child widget to share hover/selection *background only*.

        Unlike ``bind_to``: no click/hover bindings — for widgets that handle
        their own events (e.g. mark icons toggling state without selecting
        the card) but must recolor together with the card.
        """
        self._registered_widgets.append(widget)
        widget.configure(bg=theme.SEL_ROW_ACTIVE if self._selected
                         else (theme.HOVER_BG if self._hovered else self._bg))
        return widget

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if selected:
            self._apply_bg(theme.SEL_ROW_ACTIVE)
            for w, orig_fg in self._label_fgs.items():
                try:
                    if orig_fg in (theme.FG, theme.MUTED):
                        w.configure(fg=theme.FG_WHITE)
                except tk.TclError:
                    pass
        else:
            self._apply_bg(self._bg)
            for w, orig_fg in self._label_fgs.items():
                try:
                    w.configure(fg=orig_fg)
                except tk.TclError:
                    pass

class MetricCell:
    """One metric tile: colored title + value (StringVar) + unit + tooltip.

    Holds ``self.var`` so the panel can update the value without rebuilding.
    Value color reflects sentiment: green=good direction, red=bad, gray=neutral.
    ``approx=True`` 时数值右侧追加中文量级近似小字（如 41,041,696 ≈ 0.4亿，
    小号灰白），帮助快速读出大数——精确数照常展示。
    """

    def __init__(self, parent, metric: Metric, on_click=None, *,
                 approx: bool = False) -> None:
        self.metric = metric
        self._approx = approx
        self.frame = tk.Frame(parent, bg=theme.PANEL, padx=8, pady=4)  # 与 deck chip 同节奏（8px 网格）
        color = theme.LEVEL_COLORS.get(metric.level, theme.LEVEL_BASIC)
        title_text = f"{metric.name}（{metric.unit}）" if metric.unit else metric.name
        # 标题按指标层级着色（core/compound 黄色信号等）；此前算了没用、恒灰
        title_fg = theme.LEVEL_COLORS.get(metric.level, theme.LEVEL_BASIC)
        # 指标名是用户主扫读对象：9pt（8pt 黄/白小字在暗底可读性差）
        self.title = tk.Label(self.frame, text=title_text, bg=theme.PANEL, fg=title_fg,
                              font=theme.FONT_UI, anchor="w")
        self.title.pack(anchor="w")

        self.var = tk.StringVar(value="-")
        value_fg = theme.VALUE_NEUTRAL
        if approx:
            row = tk.Frame(self.frame, bg=theme.PANEL)
            row.pack(anchor="w", pady=(1, 0))
            self.value = tk.Label(row, textvariable=self.var, bg=theme.PANEL,
                                  fg=value_fg, font=theme.FONT_VALUE, anchor="w")
            self.value.pack(side="left")
            self.approx_lbl = tk.Label(row, text="", bg=theme.PANEL, fg=theme.MUTED,
                                       font=theme.FONT_UI_SMALL, anchor="w")
            self.approx_lbl.pack(side="left", padx=(4, 0))
            widgets = (self.frame, self.title, self.value, self.approx_lbl)
        else:
            self.value = tk.Label(self.frame, textvariable=self.var, bg=theme.PANEL,
                                  fg=value_fg, font=theme.FONT_VALUE, anchor="w")
            self.value.pack(anchor="w", pady=(1, 0))
            self.approx_lbl = None
            widgets = (self.frame, self.title, self.value)

        if on_click:
            self.value.config(cursor=CLICK_CURSOR)
            self.title.config(cursor=CLICK_CURSOR)
            self.value.bind("<Button-1>", lambda e: on_click())
            self.title.bind("<Button-1>", lambda e: on_click())

            def _on_enter(_e):
                self.title.configure(fg=theme.ACCENT)

            def _on_leave(_e):
                self.title.configure(fg=color)

            for _w in (self.frame, self.title, self.value):
                _w.bind("<Enter>", _on_enter, add="+")
                _w.bind("<Leave>", _on_leave, add="+")
            if self.approx_lbl:
                self.approx_lbl.bind("<Enter>", _on_enter, add="+")
                self.approx_lbl.bind("<Leave>", _on_leave, add="+")

        tip = f"{metric.name}\n{metric.tip}"
        for w in widgets:
            Tooltip(w, tip)

    @property
    def key(self) -> str:
        return self.metric.key

    def set_value(self, text: str) -> None:
        """Update displayed value and apply sentiment-based coloring."""
        self.var.set(text)
        if self.approx_lbl is not None:
            try:
                num = float(text.replace(",", "").replace("%", "").replace("$", ""))
                from tcer.core.format import fmt_approx_cn
                self.approx_lbl.config(text=fmt_approx_cn(num) or "")
            except (ValueError, TypeError):
                self.approx_lbl.config(text="")
        if text == UNSUPPORTED_LABEL:
            # 数据源不提供该字段 — 弱化显示，与「无数据 -」区分。
            self.value.config(fg=theme.MUTED)
            return
        if text in ("-", "0", "0.0", "0.00", "0.000"):
            fg = theme.VALUE_NEUTRAL
        else:
            try:
                # 注意 num 是显示标度：pct/pct4 格式已 ×100（"35.2%" → 35.2）。
                # 「异常才着色」纪律：无绿色奖励、无非零即色；只有真异常阈值
                # （高返工 / 工具报错）才允许警示色，其余一律中性。
                num = float(text.replace(",", "").replace("%", "").replace("$", ""))
                if self.metric.key == "churn":
                    fg = theme.ERROR if num >= 35 else (theme.WARNING if num >= 15 else theme.VALUE_NEUTRAL)
                elif self.metric.key == "tool_error_rate":
                    fg = theme.ERROR if num >= 10 else (theme.WARNING if num > 0 else theme.VALUE_NEUTRAL)
                else:
                    fg = theme.VALUE_NEUTRAL
            except (ValueError, TypeError):
                fg = theme.VALUE_NEUTRAL
        self.value.config(fg=fg)


class SelectableLabel(tk.Text):
    """Label 外观的可选中文本：``state="disabled"`` 的 tk.Text 仍可拖选 + Ctrl+C 复制。

    tk.Label 无法选中复制；用户消息这类长文本需要可拷出，故用 Text 伪装成
    Label（flat / 无边框 / 同 bg-fg-font / 按 ``width`` 字符列自动换行）。仅用于
    静态展示文本 —— 插入后置 disabled，不可编辑但可选中复制。

    换行由 ``width``（字符列）决定，与像素宽度无关 —— **不要从像素反推字符列**
    （未布局时 ``winfo_width()=1`` 会把行数算爆）。定宽容器里给一个显式 width，
    再用 ``count(displaylines)`` 一次性算出高度即可，无需 ``<Configure>`` 动态重算。
    """

    def __init__(self, parent, *, text="", bg=theme.PANEL, fg=theme.FG,
                 font=None, justify="left", width=60, wraplength=None,
                 padx=0, pady=0, **kw):
        # wraplength 仅作 API 兼容：吸收被替换的 tk.Label 残留的 wraplength= 参数。
        # 本控件换行由 widget 实际像素宽度决定（pack(fill="x") + <Configure> 重算），
        # 不依赖 tk.Text 不支持的 wraplength，故此处显式忽略。
        del wraplength
        super().__init__(parent, wrap="word", bg=bg, fg=fg,
                         font=font or theme.FONT_UI, relief="flat", bd=0,
                         highlightthickness=0, padx=padx, pady=pady,
                         width=width, height=1, cursor="arrow",
                         selectbackground=theme.HOVER_ACCENT,
                         selectforeground=theme.FG_WHITE,
                         inactiveselectbackground=theme.HOVER_ACCENT,
                         exportselection=True, **kw)
        self.tag_configure("all", justify=justify)
        self.insert("1.0", text)
        self.tag_add("all", "1.0", "end")
        self.configure(state="disabled")
        # 右键「复制全文」兜底：长消息拖选不便时一键复制。
        self.bind("<Button-3>", self._copy_menu, add="+")
        # 构造时尚未布局(pack 前 winfo_width=1 → count 把每字算成一行)，
        # 首次 pack 获得真实宽度时由 <Configure> 重算修正。
        self._last_width = -1
        self.bind("<Configure>", self._on_configure, add="+")
        self._auto_height()

    def set_text(self, text: str) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.tag_add("all", "1.0", "end")
        self.configure(state="disabled")
        self._auto_height()

    def _on_configure(self, event) -> None:
        # 仅宽度变化时重算;height 自身变化触发的 Configure 被忽略,避免递归。
        if event.width != self._last_width:
            self._last_width = event.width
            self._auto_height()

    # ---- 高度测量合并结算 ----------------------------------------------- #
    # 每个实例各自 ``update_idletasks()`` 再 count 是灾难：一次强制全量布局
    # ~40ms，效率榜一次刷新建 47 个洞察条 → 47 次全量布局 ≈ 2s 主线程卡顿。
    # 改为类级待测队列 + 单个 after_idle：布局只结算一次，逐个 count（便宜）。
    # 定时器锚定到长命的 toplevel：tkinter 的 after 回调注册在调度 widget 名下，
    # 该 widget 在 idle 前被销毁（重建流程常见）会连 tcl 命令一起删掉、定时器
    # 永不触发——待测队列随之永久挂起。每次登记时校验锚存活自愈。
    _PENDING: "set[SelectableLabel]" = set()
    _PENDING_AFTER: str | None = None
    _ANCHOR: "tk.Misc | None" = None
    _RETRY_MS = 30     # 未布局 widget 的重试间隔
    _MAX_TRIES = 10    # 重试上限，超限走旧式强制测量兜底

    def _auto_height(self) -> None:
        """登记待测高；实际测量合并到下一个空闲点（见 _flush_pending_heights）。"""
        cls = type(self)
        self._h_tries = 0
        cls._PENDING.add(self)
        cls._arm()

    @classmethod
    def _arm(cls, delay: int | None = None) -> None:
        """（重新）武装合并测量定时器；锚死自愈。"""
        anchor = cls._ANCHOR
        try:
            if anchor is not None and not anchor.winfo_exists():
                anchor = None
                cls._PENDING_AFTER = None  # 锚已死：旧定时器必失效，重排
        except tk.TclError:
            anchor = None
            cls._PENDING_AFTER = None
        if anchor is None:
            if not cls._PENDING:
                return
            w = next(iter(cls._PENDING))
            try:
                cls._ANCHOR = anchor = w.winfo_toplevel()
            except tk.TclError:
                cls._PENDING.clear()
                return
        if cls._PENDING_AFTER is None:
            if delay is None:
                cls._PENDING_AFTER = anchor.after_idle(cls._flush_pending_heights)
            else:
                cls._PENDING_AFTER = anchor.after(delay, cls._flush_pending_heights)

    @classmethod
    def _flush_pending_heights(cls) -> None:
        cls._PENDING_AFTER = None
        if not cls._PENDING:
            return
        pending = [w for w in cls._PENDING if w.winfo_exists()]
        cls._PENDING.clear()
        stuck: list = []
        for w in pending:
            try:
                if w.winfo_width() <= 1:
                    # 尚未布局（宽度仍为初始 1）：此刻测量必错。短暂重试等
                    # 布局；一直不布局（折叠区等未映射容器）超限后按旧式
                    # update_idletasks 强制结算兜底。
                    w._h_tries = getattr(w, "_h_tries", 0) + 1
                    if w._h_tries <= cls._MAX_TRIES:
                        stuck.append(w)
                        continue
                    w.update_idletasks()
                n = w.count("1.0", "end-1c", "displaylines")
                lines = n[0] if n and n[0] else 1
                w.configure(height=max(1, lines + 1))
                w._h_tries = 0
            except tk.TclError:
                pass  # 竞态销毁：放弃该实例
        if stuck:
            cls._PENDING.update(stuck)
            cls._arm(cls._RETRY_MS)

    def _copy_menu(self, event=None) -> None:
        menu = FlatMenu(self)
        menu.add_command(label="复制", command=self._copy_all)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_all(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.get("1.0", "end-1c"))


class _MacButton(tk.Label):
    """mac 上模拟扁平按钮：``tk.Label`` + 点击/hover 绑定。

    macOS Tk 的 Aqua 主题忽略 ``tk.Button`` 的 ``bg``（bpo-44243 → 白按钮）；
    视角切换 pill 早已用 tk.Label 绕开此限制，flat_button 在 mac 上同此处理。
    兼容 tk.Button 的 ``command``（构造时传入与 ``.config(command=)`` 重设），
    供依赖该 API 的 flat_button 调用方（弹窗按钮等）使用。
    """

    def __init__(self, master, *, command=None, base_bg, hover_bg, **kw):
        super().__init__(master, **kw)
        self._base_bg = base_bg
        self._hover_bg = hover_bg
        self._command = command
        self._click_id = None
        self._rebind_click()
        self.bind("<Enter>", lambda _e: self.config(bg=self._hover_bg), add="+")
        self.bind("<Leave>", lambda _e: self.config(bg=self._base_bg), add="+")

    def _rebind_click(self) -> None:
        if self._click_id is not None:
            self.unbind("<Button-1>", self._click_id)
            self._click_id = None
        if self._command is not None:
            self._click_id = self.bind(
                "<Button-1>", lambda _e: self._command(), add="+")

    def configure(self, cnf=None, **kw):
        # tk.Label 无 command 选项：拦截后自行处理（构造与 .config(command=) 通用）。
        if "command" in kw:
            self._command = kw.pop("command")
            self._rebind_click()
        return super().configure(cnf, **kw)

    config = configure


def flat_button(parent, text, command=None, *, primary=False, padx=None, pady=None, **kw):
    """统一扁平按钮：一致的配色/内边距/hover 反馈（按钮效果一致性的单一来源）。

    ``primary=True`` 用主题强调色（主操作），否则面板灰（普通操作）。
    padx/pady 默认 PAD_M/PAD_XS；传值可放大主操作按钮（如「立即更新」）。
    macOS 下用 ``_MacButton``（tk.Label）绕开 Aqua 主题 tk.Button 的白背景 bug。
    """
    base_bg = theme.ACCENT if primary else theme.PANEL
    hover_bg = theme.HOVER_ACCENT if primary else theme.HOVER_BG
    fg = theme.FG_WHITE if primary else theme.FG
    pad_x = theme.PAD_M if padx is None else padx
    pad_y = theme.PAD_XS if pady is None else pady
    if PLATFORM == "darwin":
        return _MacButton(parent, command=command, base_bg=base_bg, hover_bg=hover_bg,
                          text=text, bg=base_bg, fg=fg, font=theme.FONT_UI,
                          padx=pad_x, pady=pad_y, cursor=CLICK_CURSOR,
                          highlightthickness=0, **kw)
    btn = tk.Button(parent, text=text, command=command, relief="flat",  # style-exempt: flat_button 本体
                    bg=base_bg, fg=fg, bd=0, cursor=CLICK_CURSOR,
                    activebackground=hover_bg, activeforeground=fg,
                    padx=pad_x, pady=pad_y, font=theme.FONT_UI,
                    highlightthickness=0,  # tk 层聚焦带（mac 分支同款，规范⑥）
                    takefocus=0,           # Windows 经典按钮持焦时的 DrawFocusRect
                                          # 蚂蚁线无法用 highlight 选项关闭，只能
                                          # 不抢焦点（点击后焦点留在原处，规范⑥）
                    **kw)
    btn.bind("<Enter>", lambda _e: btn.config(bg=hover_bg), add="+")
    btn.bind("<Leave>", lambda _e: btn.config(bg=base_bg), add="+")
    return btn


class FlatMenu:
    """无边框深色弹出菜单——用 ``overrideredirect`` Toplevel 取代 ``tk.Menu``。

    Why: Windows 的 ``tk.Menu`` 弹出会带一圈原生系统白边（#f0f0f0），且
    ``borderwidth``/``relief`` 都管不了它（菜单窗口是 OS 画的），只能自绘。
    API 对齐 ``tk.Menu`` 常用子集（add_command / add_separator / tk_popup），
    调用方改动最小；1px 外框走 ``theme.BORDER``，悬停高亮走 ``theme.ACCENT``。
    """

    _active_menu: FlatMenu | None = None

    def __init__(self, parent):
        self._closed = False
        self._top = tk.Toplevel(parent)
        self._top.overrideredirect(True)
        self._top.configure(bg=theme.BORDER_HOVER)               # 1px 浮起微亮外框
        self._body = tk.Frame(self._top, bg=theme.PANEL_2)      # 抬升菜单底色，与工作台层级清晰区分
        self._body.pack(fill="both", expand=True, padx=1, pady=1)  # 1px 露出外框
        self._top.withdraw()

    def add_command(self, label="", command=None, image=None, compound=None,
                    state="normal", **_kw):
        disabled = (state == "disabled")
        row = tk.Frame(self._body, bg=theme.PANEL_2)
        row.pack(fill="x")
        fg = theme.MUTED if disabled else theme.FG
        lbl = tk.Label(row, text=label, image=image, compound="left",
                       bg=theme.PANEL_2, fg=fg, font=theme.FONT_UI,
                       padx=14, pady=4, anchor="w")
        lbl.pack(fill="x")
        if not disabled:
            def enter(_e):
                row.configure(bg=theme.ACCENT)
                lbl.configure(bg=theme.ACCENT, fg=theme.FG_WHITE)
            def leave(_e):
                row.configure(bg=theme.PANEL_2)
                lbl.configure(bg=theme.PANEL_2, fg=theme.FG)
            def click(_e):
                self._close()
                if command is not None:
                    command()
            for w in (row, lbl):
                w.configure(cursor=CLICK_CURSOR)
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)
                w.bind("<Button-1>", click)
        return row

    def add_radiobutton(self, label="", variable=None, value=None, command=None, **_kw):
        selected = variable is not None and variable.get() == value
        prefix = "●  " if selected else "    "
        row = tk.Frame(self._body, bg=theme.PANEL_2)
        row.pack(fill="x")
        lbl = tk.Label(row, text=prefix + label, bg=theme.PANEL_2, fg=theme.FG,
                       font=theme.FONT_UI, padx=14, pady=4, anchor="w")
        lbl.pack(fill="x")
        def enter(_e):
            row.configure(bg=theme.ACCENT)
            lbl.configure(bg=theme.ACCENT, fg=theme.FG_WHITE)

        def leave(_e):
            row.configure(bg=theme.PANEL_2)
            lbl.configure(bg=theme.PANEL_2, fg=theme.FG)

        def click(_e):
            if variable is not None:
                variable.set(value)
            self._close()
            if command is not None:
                command()

        for w in (row, lbl):
            w.configure(cursor=CLICK_CURSOR)
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
            w.bind("<Button-1>", click)
        return row

    def add_separator(self):
        tk.Frame(self._body, bg=theme.BORDER, height=1).pack(fill="x", padx=4, pady=3)

    def tk_popup(self, x, y, *_args):
        if FlatMenu._active_menu is not None and FlatMenu._active_menu != self:
            try:
                FlatMenu._active_menu._close()
            except Exception:
                pass
        FlatMenu._active_menu = self

        self._top.deiconify()
        self._top.update_idletasks()
        w, h = self._top.winfo_reqwidth(), self._top.winfo_reqheight()
        from .platform import get_monitor_work_area
        m_left, m_top, m_right, m_bottom = get_monitor_work_area(x, y)

        # 计算垂直与水平安全边界：优先约束在宿主窗口内，外层兜底所属显示器工作区
        max_y = m_bottom - 10
        min_y = m_top + 10
        try:
            top_win = self._top.master.winfo_toplevel()
            win_top = top_win.winfo_rooty()
            win_bot = win_top + top_win.winfo_height() - 6
            if win_bot > win_top + 100:
                max_y = min(max_y, win_bot)
                min_y = max(min_y, win_top + 10)
        except Exception:
            pass

        if x + w > m_right - 10:
            x = max(m_left + 10, m_right - w - 10)
        if x < m_left + 10:
            x = m_left + 10
        if y + h > max_y:
            # 向下溢出时自动向上翻转（底对齐，防窗口下边缘与任务栏截断）
            y = max(min_y, max_y - h)
        if y < min_y:
            y = min_y
        self._top.geometry(f"+{x}+{y}")
        self._top.grab_set_global()
        for btn in ("<Button-1>", "<Button-2>", "<Button-3>"):
            self._top.bind(btn, self._on_top_click, add="+")
        self._top.bind("<Escape>", lambda _e: self._close())
        self._top.focus_set()

    def _on_top_click(self, e):
        if self._closed:
            return
        # 命中项时由项的 Button-1 先处理（并 close）；落在菜单外才由这里关。
        x0, y0 = self._top.winfo_rootx(), self._top.winfo_rooty()
        x1, y1 = x0 + self._top.winfo_width(), y0 + self._top.winfo_height()
        if not (x0 <= e.x_root <= x1 and y0 <= e.y_root <= y1):
            is_right_click = getattr(e, "num", None) in (2, 3)
            rx, ry = e.x_root, e.y_root
            num = getattr(e, "num", 1)
            master = getattr(self._top, "master", None)
            self._close()
            if is_right_click and master is not None:
                def _forward():
                    try:
                        under = master.winfo_containing(rx, ry)
                        if under:
                            ux = rx - under.winfo_rootx()
                            uy = ry - under.winfo_rooty()
                            under.event_generate(f"<Button-{num}>", x=ux, y=uy, rootx=rx, rooty=ry)
                    except Exception:
                        pass
                try:
                    master.after_idle(_forward)
                except Exception:
                    pass

    def _close(self):
        if self._closed:
            return
        self._closed = True
        if FlatMenu._active_menu is self:
            FlatMenu._active_menu = None
        try:
            self._top.grab_release()
            self._top.destroy()
        except tk.TclError:
            pass


class CalendarPopup:
    """轻量日历选择弹窗（纯标准库，深色主题）。

    无边框 ``Toplevel``：◀ 年月 ▶ 头部 + 周一首日 7×6 日期网格 + ✕ 清除。
    点选某日回调 ``on_select("YYYY-MM-DD")`` 后关闭；失焦 / Esc / 点外部亦关闭。
    用于上栏日期过滤的「点选代替手输」。``anchor`` 决定弹窗定位锚点。
    """

    _WD = ("一", "二", "三", "四", "五", "六", "日")  # 周一首日

    def __init__(self, parent, on_select, *, anchor=None, initial: str = "") -> None:
        from datetime import datetime

        self._on_select = on_select
        self.win = tk.Toplevel(parent)
        self.win.wm_overrideredirect(True)          # 无标题栏
        self.win.configure(bg=theme.BORDER)

        today = datetime.now()
        if initial:
            try:
                d = datetime.strptime(initial, "%Y-%m-%d")
                self._year, self._month = d.year, d.month
            except ValueError:
                self._year, self._month = today.year, today.month
        else:
            self._year, self._month = today.year, today.month
        self._today = today

        self._build()
        self._locate(anchor)
        self.win.bind("<Escape>", lambda _e: self.close())
        # 延迟 arm FocusOut：窗口刚建时的焦点抖动会误触发立即关闭。
        self.win.after(150, lambda: self.win.bind("<FocusOut>", lambda _e: self.close()))

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        head = tk.Frame(self.win, bg=theme.PANEL)
        head.pack(fill="x", padx=1, pady=1)
        prev = tk.Label(head, text=" ◀ ", bg=theme.PANEL, fg=theme.MUTED,
                        font=theme.FONT_UI, cursor=CLICK_CURSOR)
        prev.pack(side="left", padx=2, pady=3)
        prev.bind("<Button-1>", lambda _e: self._shift(-1))
        self._title = tk.Label(head, text="", bg=theme.PANEL, fg=theme.FG,
                               font=theme.FONT_UI_BOLD, width=9)
        self._title.pack(side="left", expand=True, fill="x", pady=3)
        nxt = tk.Label(head, text=" ▶ ", bg=theme.PANEL, fg=theme.MUTED,
                       font=theme.FONT_UI, cursor=CLICK_CURSOR)
        nxt.pack(side="left", padx=2, pady=3)
        nxt.bind("<Button-1>", lambda _e: self._shift(1))
        clr = tk.Label(head, text=" ✕ ", bg=theme.PANEL, fg=theme.MUTED,
                       font=theme.FONT_UI, cursor=CLICK_CURSOR)
        clr.pack(side="left", padx=2, pady=3)
        Tooltip(clr, "清除日期")
        clr.bind("<Button-1>", lambda _e: self._clear(), add="+")

        body = tk.Frame(self.win, bg=theme.PANEL)
        body.pack(padx=1, pady=(0, 1))
        # 星期标头与日期网格各占独立子 frame —— body 内一律 pack，网格内一律 grid，
        # 避免「同一 parent 混用 pack/grid」的 TclError。
        wd_row = tk.Frame(body, bg=theme.PANEL)
        wd_row.pack()
        for i, name in enumerate(self._WD):
            tk.Label(wd_row, text=name, bg=theme.PANEL, fg=theme.MUTED, width=3,
                     font=theme.FONT_UI_SMALL).grid(row=0, column=i, padx=1, pady=1)
        self._grid = tk.Frame(body, bg=theme.PANEL)
        self._grid.pack()
        self._render()

    def _render(self) -> None:
        import calendar as _cal
        for c in self._grid.winfo_children():
            c.destroy()
        self._title.config(text=f"{self._year}年{self._month}月")
        first_wd, n_days = _cal.monthrange(self._year, self._month)  # Monday=0
        r, col = 0, first_wd
        for day in range(1, n_days + 1):
            is_today = (self._year == self._today.year
                        and self._month == self._today.month
                        and day == self._today.day)
            cell = tk.Label(self._grid, text=str(day), bg=theme.PANEL_2, fg=theme.FG,
                            width=3, font=theme.FONT_UI_BOLD if is_today else theme.FONT_UI,
                            cursor=CLICK_CURSOR)
            if is_today:
                cell.config(fg=theme.SUCCESS)
            cell.bind("<Enter>", lambda _e, c=cell, t=is_today:
                      c.config(bg=theme.ACCENT, fg=theme.FG_WHITE), add="+")
            cell.bind("<Leave>", lambda _e, c=cell, t=is_today:
                      c.config(bg=theme.PANEL_2,
                               fg=(theme.SUCCESS if t else theme.FG)), add="+")
            cell.bind("<Button-1>", lambda _e, d=day: self._pick(d), add="+")
            cell.grid(row=r, column=col, padx=1, pady=1)
            col += 1
            if col > 6:
                col, r = 0, r + 1

    # -- actions ----------------------------------------------------------
    def _shift(self, delta: int) -> None:
        m, y = self._month + delta, self._year
        if m < 1:
            m, y = 12, y - 1
        elif m > 12:
            m, y = 1, y + 1
        self._year, self._month = y, m
        self._render()

    def _pick(self, day: int) -> None:
        from datetime import datetime
        self._on_select(datetime(self._year, self._month, day).strftime("%Y-%m-%d"))
        self.close()

    def _clear(self) -> None:
        self._on_select("")
        self.close()

    def _locate(self, anchor) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        if anchor is not None:
            x = anchor.winfo_rootx()
            y = anchor.winfo_rooty() + anchor.winfo_height() + 2
        else:
            x, y = self.win.winfo_pointerx() - w // 2, self.win.winfo_pointery() - h // 2
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = min(max(0, x), max(0, sw - w))
        y = min(max(0, y), max(0, sh - h))
        self.win.wm_geometry(f"+{int(x)}+{int(y)}")
        self.win.focus_force()

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class WorkbenchTab(tk.Frame):
    """单窗口工作台页签内容容器（代理 Toplevel 常用生命周期方法）。"""

    def __init__(self, parent, manager, tab_id: str, title: str, icon_name: str = "tools"):
        super().__init__(parent, bg=theme.BG)
        self.manager = manager
        self.tab_id = tab_id
        self._title = title
        self._icon_name = icon_name
        self._closing = False

    def title(self, new_title: str | None = None) -> str:
        if new_title is not None:
            self._title = new_title
            self.manager.update_tab_title(self.tab_id, new_title)
        return self._title

    def geometry(self, geom_str: str | None = None) -> None:
        pass  # 工作台页签自适应布局，忽略尺寸命令

    def transient(self, master=None) -> None:
        pass

    def resizable(self, w=None, h=None) -> None:
        pass

    def grab_set(self) -> None:
        pass

    def grab_release(self) -> None:
        pass

    def lift(self, aboveThis=None) -> None:
        self.manager.select_tab(self.tab_id)

    def focus_set(self) -> None:
        super().focus_set()

    def destroy(self) -> None:
        if self._closing:
            return
        self._closing = True
        if hasattr(self, "manager") and self.manager and self.manager.winfo_exists():
            try:
                self.manager.close_tab(self.tab_id)
            except Exception:
                pass
        super().destroy()


_ROUNDED_IMG_CACHE: dict[tuple, object] = {}


def get_rounded_rect_img(master, w: int, h: int, r: int, fill: str, bg: str,
                         outline: str | None = None, outline_width: int = 1):
    """生成并缓存 2x 超采样抗锯齿圆角矩形背景图。"""
    tk_id = id(getattr(master, "tk", None))
    key = (tk_id, w, h, r, fill, bg, outline, outline_width)
    if key in _ROUNDED_IMG_CACHE:
        return _ROUNDED_IMG_CACHE[key]
    try:
        from PIL import Image, ImageDraw, ImageTk
    except ImportError:
        return None
    ss = 2
    W, H, R = max(1, w * ss), max(1, h * ss), max(1, r * ss)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if outline:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill,
                            outline=outline, width=outline_width * ss)
    else:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill)
    im = im.resize((w, h), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(im, master=master)
    _ROUNDED_IMG_CACHE[key] = photo
    return photo


class RoundedPill(tk.Canvas):
    """带抗锯齿圆角的胶囊按钮/标签：自绘圆角底，支持图标与文字。"""

    def __init__(self, parent, text: str = "", icon=None, command=None, *,
                 width: int = 76, height: int = 22, radius: int = 5,
                 fill: str = theme.CONTROL_BG, hover_fill: str = theme.HOVER_BG,
                 bg: str = theme.PANEL, fg: str = theme.FG,
                 font=theme.FONT_UI_SMALL, **kw) -> None:
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor=CLICK_CURSOR if command else "arrow", **kw)
        self._width_px = width
        self._height_px = height
        self._radius = radius
        self._fill = fill
        self._normal_fill = fill
        self._hover_fill = hover_fill
        self._bg_col = bg
        self._fg = fg
        self._font = font
        self._text = text
        self._icon = icon
        self._command = command

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        if command:
            def _on_click(_e):
                try:
                    command(self)
                except TypeError:
                    command()
            self.bind("<Button-1>", _on_click)
        self._redraw()

    def set_state(self, *, fill: str | None = None, fg: str | None = None, text: str | None = None) -> None:
        if fill is not None:
            self._fill = fill
            self._normal_fill = fill
        if fg is not None:
            self._fg = fg
        if text is not None:
            self._text = text
            import tkinter.font as tkfont
            txt_w = tkfont.Font(font=self._font).measure(text)
            ico_extra = (getattr(self._icon, "width", lambda: 16)() + 4) if self._icon else 0
            needed_w = txt_w + ico_extra + 20
            if needed_w > self._width_px:
                self._width_px = needed_w
                super().configure(width=needed_w)
        self._redraw()

    def set_fill(self, fill: str) -> None:
        self._fill = fill
        self._normal_fill = fill
        self._redraw()

    def set_fg(self, fg: str) -> None:
        self._fg = fg
        self._redraw()

    def set_text(self, text: str) -> None:
        self._text = text
        import tkinter.font as tkfont
        txt_w = tkfont.Font(font=self._font).measure(text)
        ico_extra = (getattr(self._icon, "width", lambda: 16)() + 4) if self._icon else 0
        needed_w = txt_w + ico_extra + 20
        if needed_w > self._width_px:
            self._width_px = needed_w
            super().configure(width=needed_w)
        self._redraw()

    def cget(self, attr: str):
        if attr in ("bg", "background"):
            return self._fill
        if attr in ("fg", "foreground"):
            return self._fg
        if attr == "text":
            return self._text
        if attr == "font":
            return self._font
        return super().cget(attr)

    def configure(self, cnf=None, **kw):
        if cnf in ("bg", "background"):
            return self._fill
        if cnf in ("fg", "foreground"):
            return self._fg
        if cnf == "text":
            return self._text
        if cnf == "font":
            return self._font
        if "bg" in kw or "background" in kw:
            self._fill = kw.pop("bg", kw.pop("background", None))
            self._normal_fill = self._fill
        if "fg" in kw or "foreground" in kw:
            self._fg = kw.pop("fg", kw.pop("foreground", None))
        if "text" in kw:
            self._text = kw.pop("text")
        if "font" in kw:
            self._font = kw.pop("font")
        self._redraw()
        if kw:
            super().configure(**kw)
    config = configure

    def _on_enter(self, _e) -> None:
        if self._fill == self._normal_fill and self._hover_fill and self._command:
            self._fill = self._hover_fill
            self._redraw()

    def _on_leave(self, _e) -> None:
        if self._hover_fill and self._command:
            self._fill = self._normal_fill
            self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        self._img = get_rounded_rect_img(self, self._width_px, self._height_px,
                                          self._radius, self._fill, self._bg_col)
        if self._img is not None:
            self.create_image(0, 0, anchor="nw", image=self._img)
        else:
            self.create_rectangle(0, 0, self._width_px, self._height_px, fill=self._fill, outline="")
        if self._icon and self._text:
            import tkinter.font as tkfont
            ico_w = getattr(self._icon, "width", lambda: 16)()
            gap = 4
            text_w = tkfont.Font(font=self._font).measure(self._text)
            start_x = max(5, (self._width_px - (ico_w + gap + text_w)) // 2)
            self.create_image(start_x, self._height_px // 2, anchor="w", image=self._icon)
            self.create_text(start_x + ico_w + gap, self._height_px // 2, anchor="w",
                             text=self._text, fill=self._fg, font=self._font)
        elif self._text:
            self.create_text(self._width_px // 2, self._height_px // 2, anchor="center",
                             text=self._text, fill=self._fg, font=self._font)

class RoundedSearchBox(tk.Canvas):
    """带抗锯齿圆角的搜索框：底板为圆角底，内嵌搜索图标与扁平 Entry。"""

    def __init__(self, parent, textvariable, *, width: int = 110, height: int = 22,
                 radius: int = 5, fill: str = theme.CONTROL_BG,
                 bg: str = theme.SECTION_HEADER_BG, fg: str = theme.FG,
                 icon=None, **kw) -> None:
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, **kw)
        self._width_px = width
        self._height_px = height
        self._radius = radius
        self._fill = fill
        self._bg_col = bg
        self._fg = fg
        self._icon = icon

        self.entry = tk.Entry(self, textvariable=textvariable, bg=fill, fg=fg,
                              insertbackground=fg, relief="flat", borderwidth=0,
                              highlightthickness=0, font=theme.FONT_UI)
        def _clear(_e=None):
            textvariable.set("")
            self.entry.delete(0, "end")
            return "break"
        self.entry.bind("<Escape>", _clear)
        self.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self._redraw()

    def _redraw(self) -> None:
        self.delete("bg")
        self.delete("icon")
        self._img = get_rounded_rect_img(self, self._width_px, self._height_px,
                                          self._radius, self._fill, self._bg_col)
        if self._img is not None:
            self.create_image(0, 0, anchor="nw", image=self._img, tags="bg")
        else:
            self.create_rectangle(0, 0, self._width_px, self._height_px, fill=self._fill, outline="", tags="bg")
        self.tag_lower("bg")
        x_entry = 8
        if self._icon:
            self.create_image(6, self._height_px // 2, anchor="w", image=self._icon, tags="icon")
            ico_w = getattr(self._icon, "width", lambda: 16)()
            x_entry = 6 + ico_w + 4
        entry_w = self._width_px - x_entry - 6
        if not self.find_withtag("entry_win"):
            self.create_window(x_entry, self._height_px // 2, anchor="w",
                               window=self.entry, width=entry_w, tags="entry_win")
        else:
            self.coords("entry_win", x_entry, self._height_px // 2)
            self.itemconfigure("entry_win", width=entry_w)
        self.tag_bind("bg", "<Button-1>", lambda _e: self.entry.focus_set())
        self.tag_bind("icon", "<Button-1>", lambda _e: self.entry.focus_set())

class RoundedKpiChip(tk.Canvas):
    """带抗锯齿圆角的头部 KPI 徽标胶囊：标签灰度小字 + 数值粗体，支持悬停提亮与点击回调。"""

    def __init__(self, parent, label: str, value: str = "-", *, command=None,
                 tip: str = "", width: int = 95, height: int = 24, radius: int = 5,
                 fill: str = theme.CONTROL_BG, hover_fill: str = theme.HOVER_BG,
                 bg: str = theme.BG, fg: str = theme.FG, **kw) -> None:
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0,
                         cursor=CLICK_CURSOR if command else "arrow", **kw)
        self._width_px = width
        self._height_px = height
        self._radius = radius
        self._fill = fill
        self._normal_fill = fill
        self._hover_fill = hover_fill
        self._bg_col = bg
        self._fg = fg
        self._label = label
        self._value = value
        self._extra = ""
        self._command = command

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        if command:
            self.bind("<Button-1>", lambda _e: command(self))
        if tip:
            Tooltip(self, tip)
        self._redraw()

    def set_value(self, value: str, extra: str = "", fg: str | None = None) -> None:
        self._value = value
        self._extra = extra
        if fg is not None:
            self._fg = fg
        self._redraw()

    def cget(self, attr: str):
        if attr in ("bg", "background"):
            return self._fill
        if attr in ("fg", "foreground"):
            return self._fg
        if attr == "text":
            return f"{self._value} {self._extra}".strip()
        return super().cget(attr)

    def configure(self, cnf=None, **kw):
        if cnf == "text":
            return f"{self._value} {self._extra}".strip()
        if "text" in kw:
            self._value = kw.pop("text")
        if "fg" in kw:
            self._fg = kw.pop("fg")
        self._redraw()
        if kw:
            super().configure(**kw)

    config = configure

    def _on_enter(self, _e) -> None:
        if self._hover_fill and self._command:
            self._fill = self._hover_fill
            self._redraw()

    def _on_leave(self, _e) -> None:
        self._fill = self._normal_fill
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        self._img = get_rounded_rect_img(self, self._width_px, self._height_px,
                                          self._radius, self._fill, self._bg_col)
        if self._img is not None:
            self.create_image(0, 0, anchor="nw", image=self._img)
        else:
            self.create_rectangle(0, 0, self._width_px, self._height_px, fill=self._fill, outline="")
        self.create_text(8, self._height_px // 2, text=self._label, fill=theme.MUTED,
                         font=theme.FONT_UI_SMALL, anchor="w")
        # 右侧数值（等宽/粗体 FG）
        val_full = f"{self._value} {self._extra}".strip()
        self.create_text(self._width_px - 8, self._height_px // 2, text=val_full,
                         fill=self._fg, font=theme.FONT_VALUE, anchor="e")


def new_window(parent, title, size, bg=theme.BG):
    """创建居中 Toplevel 子窗口（原生弹窗，支持深色标题栏与 Esc 快捷关闭）。"""
    import tkinter as tk

    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=bg)
    parent.update_idletasks()
    wpx, hpx = (int(x) for x in size.split("x"))

    from .platform import get_monitor_work_area
    m_left, m_top, m_right, m_bottom = get_monitor_work_area(parent)
    m_width = m_right - m_left
    m_height = m_bottom - m_top

    # 屏幕工作区最大高度限制（自适应当前显示器可用高）
    max_h = max(300, m_height - 60)
    hpx = min(hpx, max_h)

    top_win = parent.winfo_toplevel()
    top_win.update_idletasks()
    pw = top_win.winfo_width()
    ph = top_win.winfo_height()
    px = top_win.winfo_rootx()
    py = top_win.winfo_rooty()

    x = px + (pw - wpx) // 2
    y = py + (ph - hpx) // 2

    # 屏缘防溢出校正（确保弹窗底边绝不穿透任务栏、顶栏绝不出界，跨多屏安全）
    if x + wpx > m_right - 10:
        x = max(m_left + 10, m_right - wpx - 10)
    if x < m_left + 10:
        x = m_left + 10
    if y + hpx > m_bottom - 10:
        y = max(m_top + 25, m_bottom - 10 - hpx)
    if y < m_top + 25:
        y = m_top + 25
    win.geometry(f"{wpx}x{hpx}+{int(x)}+{int(y)}")
    from .platform import apply_dark_titlebar
    apply_dark_titlebar(win)   # 创建即设
    win.bind("<Map>", lambda e: apply_dark_titlebar(win), add="+")
    win.bind("<Escape>", lambda e: win.destroy(), add="+")
    return win


class ModalShell:
    """Monokai Dimmed 规范模态弹窗外壳基类。

    约束 3 档规范几何：
    - compact: 520x360 （配置/向导/简易输入）
    - standard: 740x540 （单会话钻取/明细/时间线）
    - wide: 1040x680 （多维矩阵/对比/雷达/全景）

    结构：
    - 头部 Header：40px 高度，ACTIVITY_BG (#353535) 底色，11pt 粗体标题 + 副标 + ✕ 关闭
    - 内容容器 Content：BG (#1e1e1e) 底色，内边距 PAD_L (12px)
    - 底部操作栏 Action Bar（可选）：40px 高度，PANEL 底色，1px 顶边 BORDER
    """

    GEOMETRIES = {
        "compact": (520, 360),
        "standard": (740, 540),
        "wide": (1040, 680),
    }

    def __init__(self, parent, title: str, subtitle: str = "", *,
                 tier: str = "standard", geometry: str | None = None,
                 with_action_bar: bool = False, bg: str = theme.BG) -> None:
        self.parent = parent
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.configure(bg=bg)

        if geometry is not None:
            wpx, hpx = (int(x) for x in geometry.split("x"))
        else:
            wpx, hpx = self.GEOMETRIES.get(tier, self.GEOMETRIES["standard"])

        parent.update_idletasks()
        from .platform import get_monitor_work_area
        m_left, m_top, m_right, m_bottom = get_monitor_work_area(parent)
        top_win = parent.winfo_toplevel()
        top_win.update_idletasks()
        pw, ph = top_win.winfo_width(), top_win.winfo_height()
        px, py = top_win.winfo_rootx(), top_win.winfo_rooty()
        x = px + (pw - wpx) // 2
        y = py + (ph - hpx) // 2
        if x + wpx > m_right - 10:
            x = max(m_left + 10, m_right - wpx - 10)
        if x < m_left + 10:
            x = m_left + 10
        if y + hpx > m_bottom - 10:
            y = max(m_top + 25, m_bottom - 10 - hpx)
        if y < m_top + 25:
            y = m_top + 25
        self.win.geometry(f"{wpx}x{hpx}+{int(x)}+{int(y)}")

        from .platform import apply_dark_titlebar
        apply_dark_titlebar(self.win)
        self.win.bind("<Map>", lambda e: apply_dark_titlebar(self.win), add="+")
        self.win.bind("<Escape>", lambda _e: self.close())

        # 1. 顶部 Header (40px)
        self.header = tk.Frame(self.win, bg=theme.ACTIVITY_BG, height=40)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        title_box = tk.Frame(self.header, bg=theme.ACTIVITY_BG)
        title_box.pack(side="left", fill="both", expand=True, padx=theme.PAD_L)

        self.title_lbl = tk.Label(title_box, text=title, bg=theme.ACTIVITY_BG,
                                  fg=theme.FG_WHITE, font=(theme.FONT_CJK, 11, "bold"), anchor="w")
        if subtitle:
            self.title_lbl.pack(side="top", anchor="w", pady=(3, 0))
            self.sub_lbl = tk.Label(title_box, text=subtitle, bg=theme.ACTIVITY_BG,
                                    fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
            self.sub_lbl.pack(side="top", anchor="w", pady=(0, 2))
        else:
            self.title_lbl.pack(side="left", fill="both", expand=True)
            self.sub_lbl = None

        close_btn = tk.Label(self.header, text=" ✕ ", bg=theme.ACTIVITY_BG,
                             fg=theme.MUTED, font=theme.FONT_UI_BOLD, cursor=CLICK_CURSOR)
        close_btn.pack(side="right", padx=theme.PAD_M)
        close_btn.bind("<Button-1>", lambda _e: self.close())
        close_btn.bind("<Enter>", lambda _e: close_btn.configure(fg=theme.FG_WHITE, bg=theme.HOVER_BG))
        close_btn.bind("<Leave>", lambda _e: close_btn.configure(fg=theme.MUTED, bg=theme.ACTIVITY_BG))

        tk.Frame(self.win, bg=theme.BORDER, height=1).pack(fill="x")

        # 2. 底部 Action Bar (可选)
        if with_action_bar:
            self.action_bar = tk.Frame(self.win, bg=theme.PANEL, height=40)
            self.action_bar.pack(side="bottom", fill="x")
            self.action_bar.pack_propagate(False)
            tk.Frame(self.action_bar, bg=theme.BORDER, height=1).pack(side="top", fill="x")
            self.action_inner = tk.Frame(self.action_bar, bg=theme.PANEL)
            self.action_inner.pack(fill="both", expand=True, padx=theme.PAD_L, pady=theme.PAD_S)
        else:
            self.action_bar = None
            self.action_inner = None

        # 3. 中部 Content 容器
        self.content = tk.Frame(self.win, bg=bg, padx=theme.PAD_L, pady=theme.PAD_L)
        self.content.pack(fill="both", expand=True)

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class ProgressBarWidget:
    """统一紧凑比例条组件。

    高度固定 6px（可定制），槽底色 theme.BORDER (#303030)；
    监听 <Configure> 动态响应宽度；
    支持 set_segments([(weight/ratio, hex_color), ...])。
    """

    def __init__(self, parent, *, height: int = 6, bg: str = theme.BORDER) -> None:
        self._height = height
        self._bg = bg
        self._segments: list[tuple[float, str]] = []
        self.canvas = tk.Canvas(parent, height=height, bg=bg, highlightthickness=0)
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._draw())

    def set_segments(self, segments: list[tuple[float, str]]) -> None:
        """Set segments as [(weight/ratio, hex_color), ...]."""
        self._segments = segments
        self._draw()

    def _draw(self) -> None:
        self.canvas.delete("all")
        w = self.canvas.winfo_width()
        h = self._height
        if w <= 1 or not self._segments:
            return

        total = sum(max(0.0, s[0]) for s in self._segments)
        if total <= 0:
            return

        x = 0.0
        for weight, color in self._segments:
            if weight <= 0:
                continue
            seg_w = (weight / total) * w
            x1 = min(float(w), x + seg_w)
            if x1 > x:
                self.canvas.create_rectangle(int(x), 0, int(x1), h, fill=color, outline="")
            x = x1
