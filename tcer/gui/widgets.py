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
                       wraplength=540, font=theme.FONT_UI, padx=10, pady=6)
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
    """圆角细描边手风琴折叠卡片（基于 RoundedFrame 9-slice 抗锯齿容器）。

    表头高度 30px，左侧 3px 胶囊色标 + 折叠箭头（▾ / ▸）+ 标题 + 右侧徽标；
    展开态下表头与内容以 1px DIVIDER 分隔，完美兼容旧版 CollapsibleSection。
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
        self._header_bg = theme.SURFACE_CARD
        rail_col = color if (color and color not in (theme.CARD_HEADER_BG, theme.BG)) else theme.ACCENT

        p_bg = str(parent.cget("bg")) if hasattr(parent, "cget") else theme.BG
        self.frame = RoundedFrame(parent, fill=theme.SURFACE_CARD, parent_bg=p_bg,
                                  outline=theme.BORDER_SUBTLE, radius=theme.RADIUS_L)
        self.frame.pack(fill="both" if expand else "x", expand=expand, padx=4, pady=3)

        # 28px 圆角内衬手风琴表头
        self.header = tk.Frame(self.frame, bg=self._header_bg, height=28, cursor=CLICK_CURSOR)
        self.header.pack(fill="x", padx=6, pady=(4, 4 if self._collapsed else 2))
        self.header.pack_propagate(False)

        # 3px 左侧强调色短柱
        self.rail = tk.Frame(self.header, bg=rail_col, width=3)
        self.rail.pack(side="left", fill="y", padx=(2, 4), pady=5)
        self.rail.pack_propagate(False)

        arrow_char = "▾" if default_open else "▸"
        self._arrow = tk.Label(self.header, text=arrow_char, bg=self._header_bg, fg=theme.FG_WHITE,
                               font=theme.FONT_UI_BOLD, cursor=CLICK_CURSOR, padx=2)
        self._arrow.pack(side="left", padx=(0, 2))

        self._title_lbl = tk.Label(self.header, text=title, bg=self._header_bg, fg=theme.FG_WHITE,
                                   font=theme.FONT_UI_BOLD, anchor="w", cursor=CLICK_CURSOR)
        self._title_lbl.pack(side="left", fill="x", expand=True)

        self._badge_lbl = tk.Label(self.header, text=badge, bg=self._header_bg, fg=theme.MUTED,
                                   font=theme.FONT_UI_SMALL, anchor="e", cursor=CLICK_CURSOR)
        if badge:
            self._badge_lbl.pack(side="right", padx=(0, 6))

        self._divider = tk.Frame(self.frame, bg=theme.DIVIDER, height=1)
        self.content = tk.Frame(self.frame, bg=theme.SURFACE_CARD)
        if default_open:
            self._divider.pack(fill="x", padx=6, pady=(0, 2))
            self.content.pack(fill="both" if expand else "x", expand=expand, padx=6, pady=(0, 6))

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
        bg = theme.PANEL_2 if is_hover else self._header_bg
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
            self._badge_lbl.pack(side="right", padx=(0, 6))
        elif not badge and self._badge_lbl.winfo_ismapped():
            self._badge_lbl.pack_forget()

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        arrow_char = "▸" if self._collapsed else "▾"
        self._arrow.configure(text=arrow_char)
        if self._collapsed:
            self._divider.pack_forget()
            self.content.pack_forget()
            self.header.pack_configure(pady=(4, 4))
        else:
            self.header.pack_configure(pady=(4, 2))
            self._divider.pack(fill="x", padx=6, pady=(0, 2), after=self.header)
            self.content.pack(fill="both" if self._expand else "x",
                              expand=self._expand, padx=6, pady=(0, 6), after=self._divider)
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
        region = (x0, y0, x1, y1)
        if getattr(self, "_last_scrollregion", None) != region:
            self._last_scrollregion = region
            self.canvas.configure(scrollregion=region)

    def _on_resize(self, event) -> None:
        new_w = event.width
        if getattr(self, "_last_win_width", None) == new_w:
            return
        self._target_win_width = new_w
        if getattr(self, "_resize_after", None) is not None:
            return
        self._resize_after = self.canvas.after(20, self._flush_win_width)

    def _flush_win_width(self) -> None:
        self._resize_after = None
        w = getattr(self, "_target_win_width", None)
        if w is not None and getattr(self, "_last_win_width", None) != w:
            self._last_win_width = w
            try:
                self.canvas.itemconfig(self._win, width=w)
            except tk.TclError:
                pass
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

    def see(self, widget) -> None:
        """Scroll canvas to ensure ``widget`` is visible."""
        try:
            self.canvas.update_idletasks()
            c_h = self.canvas.winfo_height()
            if c_h <= 1:
                return
            sr = str(self.canvas.cget("scrollregion")).split()
            if len(sr) < 4:
                return
            total_h = float(sr[3])
            if total_h <= c_h:
                return
            wy = widget.winfo_y()
            wh = widget.winfo_height()
            top_frac = wy / total_h
            bot_frac = (wy + wh) / total_h
            curr_top, curr_bot = self.canvas.yview()
            if top_frac < curr_top:
                self.canvas.yview_moveto(top_frac)
            elif bot_frac > curr_bot:
                self.canvas.yview_moveto(max(0.0, (wy + wh - c_h) / total_h))
        except Exception:
            pass


class Card:
    """A selectable list card with solid elevation, state rail, and hover feedback.

    Build content into ``self.frame``; register any child widget that should
    also trigger selection and match hover/selection styling via ``bind_to``.
    """

    def __init__(self, parent, on_click, on_right_click=None,
                 bg: str = theme.PANEL_2, padx: int = 4, pady: int = 2,
                 radius: int = theme.RADIUS_M) -> None:
        self._bg = bg
        self._parent_bg = str(parent.cget("bg")) if hasattr(parent, "cget") else theme.PANEL
        self._on_click = on_click
        self._on_right_click = on_right_click
        self._selected = False
        self._hovered = False
        self._state_rail_color: str | None = None
        self._registered_widgets: list[tk.Widget] = []
        self._label_fgs: dict[tk.Widget, str] = {}
        self._surface_bg = bg  # 子控件当前应有的卡片底色（_apply_bg 兜底换色用）
        self._radius = radius
        self._bg_img = None
        self._last_draw_key = None

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
        fill_col = theme.SEL_ROW_ACTIVE if self._selected else (theme.HOVER_BG if self._hovered else self._bg)
        draw_key = (w, h, fill_col)
        if draw_key == getattr(self, "_last_draw_key", None):
            return
        self._last_draw_key = draw_key
        # 1px BORDER 描边：卡面与容器的清晰分界（结构分隔 token，非状态表达）
        draw_rounded_box(self.frame, w, h, self._radius, fill_col, self._parent_bg,
                         outline=theme.BORDER, tag="card_bg")

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
        self._last_draw_key = None
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
        # 兜底：未经 bind_to / track_bg 登记、但与卡片同底的 Frame / Label（行容器
        # 最常漏）一并换色——否则悬停时露出一块块旧底色的色斑。只认「当前卡片
        # 底色」，刻意另配底色的子区（如展开明细的 PANEL 表格）不受影响。
        old_bg, self._surface_bg = self._surface_bg, bg_color
        if old_bg != bg_color:
            self._recolor_surface(self.frame, old_bg, bg_color)

    def _recolor_surface(self, parent, old_bg: str, new_bg: str) -> None:
        for w in parent.winfo_children():
            if w is self.rail:
                continue
            if type(w) in (tk.Frame, tk.Label):
                try:
                    if str(w.cget("bg")) == old_bg:
                        w.configure(bg=new_bg)
                except tk.TclError:
                    pass
            self._recolor_surface(w, old_bg, new_bg)

    def bind_to(self, widget) -> tk.Widget:
        """Register a child widget to share click, hover, and selection styles."""
        self._registered_widgets.append(widget)
        if isinstance(widget, tk.Label):
            try:
                self._label_fgs[widget] = str(widget.cget("fg"))
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

    def select(self) -> None:
        """Visual select shorthand."""
        self.set_selected(True)

    def deselect(self) -> None:
        """Visual deselect shorthand."""
        self.set_selected(False)

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
        p_bg = str(parent.cget("bg")) if hasattr(parent, "cget") else theme.SURFACE_CARD
        self.frame = RoundedFrame(
            parent, fill=theme.PANEL, parent_bg=p_bg,
            outline=theme.BORDER_SUBTLE, radius=theme.RADIUS_M,
            cursor=CLICK_CURSOR if on_click else "arrow",
        )
        color = theme.LEVEL_COLORS.get(metric.level, theme.LEVEL_BASIC)
        # 标题按指标层级着色（core/compound 黄色信号等）；此前算了没用、恒灰
        title_fg = theme.LEVEL_COLORS.get(metric.level, theme.LEVEL_BASIC)
        # 指标名是用户主扫读对象：9pt（8pt 黄/白小字在暗底可读性差）。
        # 名称与单位分两行（2026-10）：拼接式「名（单位）」在等宽网格窄格内
        # 会被裁断或词中断裂；分层排版后各自行内完整显示。
        self.title = tk.Label(self.frame, text=metric.name, bg=theme.PANEL, fg=title_fg,
                              font=theme.FONT_UI, anchor="w")
        self.title.pack(anchor="w", padx=10, pady=(6, 0))
        self._unit_lbl: tk.Label | None = None
        if metric.unit:
            self._unit_lbl = tk.Label(self.frame, text=metric.unit, bg=theme.PANEL,
                                      fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
            self._unit_lbl.pack(anchor="w", padx=10, pady=(0, 0))

        self.var = tk.StringVar(value="-")
        value_fg = theme.VALUE_NEUTRAL
        if approx:
            row = tk.Frame(self.frame, bg=theme.PANEL)
            row.pack(anchor="w", padx=10, pady=(1, 6))
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
            self.value.pack(anchor="w", padx=10, pady=(1, 6))
            self.approx_lbl = None
            widgets = (self.frame, self.title, self.value)

        if on_click:
            self.value.config(cursor=CLICK_CURSOR)
            self.title.config(cursor=CLICK_CURSOR)
            self.frame.bind("<Button-1>", lambda e: on_click())
            self.value.bind("<Button-1>", lambda e: on_click())
            self.title.bind("<Button-1>", lambda e: on_click())

            def _on_enter(_e):
                self.title.configure(fg=theme.ACCENT)
                self.frame.set_surface(outline=theme.ACCENT)

            def _on_leave(_e):
                self.title.configure(fg=color)
                self.frame.set_surface(outline=theme.BORDER_SUBTLE)

            for _w in (self.frame, self.title, self.value):
                _w.bind("<Enter>", _on_enter, add="+")
                _w.bind("<Leave>", _on_leave, add="+")
            if self.approx_lbl:
                self.approx_lbl.bind("<Enter>", _on_enter, add="+")
                self.approx_lbl.bind("<Leave>", _on_leave, add="+")

        tip = f"{metric.name}\n{metric.tip}"
        for w in widgets:
            Tooltip(w, tip)
        # 等宽网格里格宽固定：超长值按格宽折行（而非裁掉或撑宽整列）
        self.frame.bind("<Configure>", self._fit_wrap, add="+")

    def _fit_wrap(self, e) -> None:
        wl = max(60, e.width - 16)
        if to_int(self.value.cget("wraplength") or 0) != wl:
            self.value.config(wraplength=wl, justify="left")

    @property
    def key(self) -> str:
        return self.metric.key

    def set_value(self, text: str, fg: str | None = None) -> None:
        """Update displayed value and apply sentiment-based coloring.

        ``fg`` 由调用方显式给定（如看板的「达标绿」规则）时优先于内部异常阈值着色。
        """
        self.var.set(text)
        self.value.config(font=theme.value_font(text))
        if self.approx_lbl is not None:
            try:
                num = float(text.replace(",", "").replace("%", "").replace("$", ""))
                from tcer.core.format import fmt_approx_cn
                self.approx_lbl.config(text=fmt_approx_cn(num) or "")
            except (ValueError, TypeError):
                self.approx_lbl.config(text="")
        if fg is not None:
            self.value.config(fg=fg)
            return
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
        self._last_width = -1
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Map>", self._on_map, add="+")
        self._auto_height()

    def _on_map(self, _event=None) -> None:
        self._auto_height()

    def set_text(self, text: str) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", text)
        self.tag_add("all", "1.0", "end")
        self.configure(state="disabled")
        self._auto_height()

    def _on_configure(self, event) -> None:
        # 仅已映射且宽度真实有效(>1)发生变化时重算，避免未布局时算爆行数
        if event.width > 1 and event.width != self._last_width:
            self._last_width = event.width
            self._auto_height()

    # ---- 高度测量合并结算 ----------------------------------------------- #
    _PENDING: "set[SelectableLabel]" = set()
    _PENDING_AFTER: str | None = None
    _ANCHOR: "tk.Misc | None" = None

    def _auto_height(self) -> None:
        """登记待测高；实际测量合并到下一个空闲点（见 _flush_pending_heights）。"""
        cls = type(self)
        cls._PENDING.add(self)
        cls._arm()

    @classmethod
    def _arm(cls, delay: int | None = None) -> None:
        """（重新）武装合并测量定时器；锚死自愈。"""
        anchor = cls._ANCHOR
        try:
            if anchor is not None and not anchor.winfo_exists():
                anchor = None
                cls._PENDING_AFTER = None
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
        try:
            pending = [w for w in cls._PENDING if w.winfo_exists()]
        except tk.TclError:
            cls._PENDING.clear()
            return
        cls._PENDING.clear()
        for w in pending:
            try:
                # 仅对已映射在屏幕且具有真实有效宽度的 widget 测量；未映射容器等 <Map> 映射后再测
                if not w.winfo_ismapped() or w.winfo_width() <= 1:
                    continue
                n = w.count("1.0", "end-1c", "displaylines")
                lines = n[0] if n and n[0] else 1
                w.configure(height=max(1, lines + 1))
            except tk.TclError:
                pass

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
    custom_bg = kw.pop("bg", None)
    custom_fg = kw.pop("fg", None)
    custom_hover = kw.pop("hover_bg", None)
    base_bg = custom_bg if custom_bg is not None else (theme.ACCENT if primary else theme.PANEL)
    hover_bg = custom_hover if custom_hover is not None else (theme.HOVER_ACCENT if primary else theme.HOVER_BG)
    fg = custom_fg if custom_fg is not None else (theme.FG_WHITE if primary else theme.FG)
    pad_x = theme.PAD_M if padx is None else padx
    pad_y = theme.PAD_XS if pady is None else pady
    if "image" in kw and text and "compound" not in kw:
        kw["compound"] = "left"
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
    btn._base_bg = base_bg    # type: ignore[attr-defined]
    btn._hover_bg = hover_bg  # type: ignore[attr-defined]
    btn.bind("<Enter>", lambda _e: btn.config(bg=btn._hover_bg) if str(btn.cget("state")) != "disabled" else None, add="+")
    btn.bind("<Leave>", lambda _e: btn.config(bg=btn._base_bg), add="+")
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
_ROUNDED_CAPS_CACHE: dict[tuple, tuple[object, object]] = {}
_ROUNDED_CORNERS_CACHE: dict[tuple, tuple[object, object, object, object]] = {}
_TK9_SVG_SUPPORT: dict[int, bool] = {}

# 测试开关：供单测验证 Tk 9.0 原生 SVG / Pillow / 纯标准库 SDF PNG 三级回退链
_ALLOW_TK9_SVG: bool = True
_ALLOW_PIL: bool = True


def _parse_hex_rgb(color: str) -> tuple[int, int, int]:
    """将主题色串解析为 (R, G, B) 整数元组（不写裸 hex 字面量以守样式契约）。"""
    s = str(color).strip().lstrip("#")
    if len(s) == 3:
        return (int(s[0] * 2, 16), int(s[1] * 2, 16), int(s[2] * 2, 16))
    if len(s) >= 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    return (30, 30, 30)


def _encode_rgb_png(w: int, h: int, rows: list[bytes]) -> bytes:
    """纯标准库（struct + zlib）编码 8-bit RGB PNG 字节流。"""
    import struct
    import zlib

    raw = b"".join(b"\x00" + r for r in rows)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b"")


def _render_sdf_rgb_rows(w: int, h: int, r: int, fill: str, bg: str,
                         outline: str | None = None, outline_width: int = 1) -> list[bytes]:
    """基于有向距离场（Signed Distance Field）生成亚像素抗锯齿圆角矩形 RGB 行缓冲。

    纯标准库 math 实现，直边保持 1px 绝对锐利，四角圆弧做平滑覆盖率混合，
    在无 Pillow 环境或 PyInstaller 单文件构建下依然呈现细腻圆角与 1px 细描边。
    """
    import math

    w = max(1, int(w))
    h = max(1, int(h))
    rad = float(max(0, min(int(r), w // 2, h // 2)))
    ow = float(max(0, int(outline_width))) if outline else 0.0

    fr, fg_c, fb = _parse_hex_rgb(fill)
    br, bg_c, bb = _parse_hex_rgb(bg)
    or_, og, ob = _parse_hex_rgb(outline) if outline else (fr, fg_c, fb)

    fill_px = bytes((fr, fg_c, fb))
    out_px = bytes((or_, og, ob))

    cx = w * 0.5
    cy = h * 0.5
    bx = cx - rad
    by = cy - rad
    margin = int(math.ceil(rad + ow + 1.0))
    has_fast_mid = w > 2 * margin + 2

    rows: list[bytes] = []
    for y in range(h):
        py = y + 0.5
        qy = abs(py - cy) - by
        # 中段纯直边快速通道：省去逐像素距离计算
        if qy <= -(ow + 1.0) and has_fast_mid:
            ow_i = int(round(ow))
            mid_len = w - 2 * ow_i
            if ow_i > 0:
                rows.append(out_px * ow_i + fill_px * mid_len + out_px * ow_i)
            else:
                rows.append(fill_px * w)
            continue

        row_ba = bytearray(w * 3)
        x = 0
        while x < w:
            if has_fast_mid and margin <= x < w - margin and qy <= 0.0:
                span = (w - margin) - x
                # 顶/底直边中段：同一 y 上 d 恒为 qy - rad
                d_mid = qy - rad
                cov_out = 0.0 if d_mid >= 0.5 else (1.0 if d_mid <= -0.5 else 0.5 - d_mid)
                if ow > 0.0:
                    d_in = d_mid + ow
                    cov_in = 0.0 if d_in >= 0.5 else (1.0 if d_in <= -0.5 else 0.5 - d_in)
                    sr = or_ + (fr - or_) * cov_in
                    sg = og + (fg_c - og) * cov_in
                    sb = ob + (fb - ob) * cov_in
                else:
                    sr, sg, sb = fr, fg_c, fb
                pr = int(br + (sr - br) * cov_out + 0.5)
                pg = int(bg_c + (sg - bg_c) * cov_out + 0.5)
                pb = int(bb + (sb - bb) * cov_out + 0.5)
                px_bytes = bytes((max(0, min(255, pr)), max(0, min(255, pg)), max(0, min(255, pb))))
                row_ba[x * 3:(x + span) * 3] = px_bytes * span
                x += span
                continue

            px = x + 0.5
            qx = abs(px - cx) - bx
            ox = qx if qx > 0.0 else 0.0
            oy = qy if qy > 0.0 else 0.0
            outside = math.hypot(ox, oy)
            inside = min(max(qx, qy), 0.0)
            d = outside + inside - rad

            if d >= 0.5:
                pr, pg, pb = br, bg_c, bb
            else:
                cov_out = 1.0 if d <= -0.5 else (0.5 - d)
                if ow > 0.0:
                    d_in = d + ow
                    cov_in = 0.0 if d_in >= 0.5 else (1.0 if d_in <= -0.5 else (0.5 - d_in))
                    sr = or_ + (fr - or_) * cov_in
                    sg = og + (fg_c - og) * cov_in
                    sb = ob + (fb - ob) * cov_in
                else:
                    sr, sg, sb = float(fr), float(fg_c), float(fb)
                if cov_out >= 1.0:
                    pr, pg, pb = int(sr + 0.5), int(sg + 0.5), int(sb + 0.5)
                else:
                    pr = int(br + (sr - br) * cov_out + 0.5)
                    pg = int(bg_c + (sg - bg_c) * cov_out + 0.5)
                    pb = int(bb + (sb - bb) * cov_out + 0.5)
            idx = x * 3
            row_ba[idx] = max(0, min(255, pr))
            row_ba[idx + 1] = max(0, min(255, pg))
            row_ba[idx + 2] = max(0, min(255, pb))
            x += 1
        rows.append(bytes(row_ba))
    return rows


def _render_sdf_png_bytes(w: int, h: int, r: int, fill: str, bg: str,
                          outline: str | None = None, outline_width: int = 1) -> bytes:
    """生成纯标准库 SDF 抗锯齿圆角矩形 PNG 字节流。"""
    rows = _render_sdf_rgb_rows(w, h, r, fill, bg, outline, outline_width)
    return _encode_rgb_png(max(1, int(w)), max(1, int(h)), rows)


def _has_tk9_svg(master) -> bool:
    """探测当前 Tk 解释器是否原生支持 Tk 9.0 SVG PhotoImage 光栅化。"""
    if not _ALLOW_TK9_SVG:
        return False
    if float(getattr(tk, "TkVersion", 8.6)) < 9.0:
        return False
    tk_id = id(getattr(master, "tk", None))
    if tk_id in _TK9_SVG_SUPPORT:
        return _TK9_SVG_SUPPORT[tk_id]
    try:
        probe = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2"/></svg>'
        img = tk.PhotoImage(data=probe, format="svg", master=master)
        _ = img.width()
        _TK9_SVG_SUPPORT[tk_id] = True
    except Exception:
        _TK9_SVG_SUPPORT[tk_id] = False
    return _TK9_SVG_SUPPORT[tk_id]


def _render_tk9_svg_photo(master, w: int, h: int, r: int, fill: str, bg: str,
                          outline: str | None = None, outline_width: int = 1):
    """使用 Tk 9.0 内置 NanoSVG 引擎直接矢量渲染圆角矩形与细描边。"""
    w = max(1, int(w))
    h = max(1, int(h))
    rad = max(0, min(int(r), w // 2, h // 2))
    if outline and outline_width > 0:
        sw = float(outline_width)
        ins = sw * 0.5
        rw = max(0.0, w - sw)
        rh = max(0.0, h - sw)
        rr = max(0.0, rad - ins)
        rect_svg = (
            f'<rect x="{ins:.2f}" y="{ins:.2f}" width="{rw:.2f}" height="{rh:.2f}" '
            f'rx="{rr:.2f}" ry="{rr:.2f}" fill="{fill}" stroke="{outline}" stroke-width="{sw:.2f}"/>'
        )
    else:
        rect_svg = f'<rect x="0" y="0" width="{w}" height="{h}" rx="{rad}" ry="{rad}" fill="{fill}"/>'
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f'<rect width="{w}" height="{h}" fill="{bg}"/>{rect_svg}</svg>'
    )
    return tk.PhotoImage(data=svg.encode("utf-8"), format="svg", master=master)


def get_rounded_caps(master, h: int, r: int, fill: str, parent_bg: str):
    """生成并缓存左右圆角端头切片（宽度为 2*r，高度为 h）。

    端头切片尺寸仅依赖 (h, r, fill, parent_bg)，与容器实际像素宽度完全解耦。
    支持 Pillow 2x 超采样与纯标准库 SDF PNG 双引擎回退，无第三方库也不退化为直角。
    """
    h = max(2, int(h))
    r = max(1, int(r))
    tk_id = id(getattr(master, "tk", None))
    key = (tk_id, h, r, fill, parent_bg, _ALLOW_PIL)
    if key in _ROUNDED_CAPS_CACHE:
        return _ROUNDED_CAPS_CACHE[key]
    cap_w = max(1, 2 * r)
    full_w = max(2, 4 * r)
    if _ALLOW_PIL:
        try:
            from PIL import Image, ImageDraw, ImageTk
            ss = 2
            W, H, R = max(1, full_w * ss), max(1, h * ss), max(1, r * ss)
            im = Image.new("RGB", (W, H), parent_bg)
            d = ImageDraw.Draw(im)
            d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill)
            im = im.resize((full_w, h), Image.Resampling.LANCZOS)
            left_photo = ImageTk.PhotoImage(im.crop((0, 0, cap_w, h)), master=master)
            right_photo = ImageTk.PhotoImage(im.crop((full_w - cap_w, 0, full_w, h)), master=master)
            _ROUNDED_CAPS_CACHE[key] = (left_photo, right_photo)
            return left_photo, right_photo
        except Exception:
            pass
    try:
        rows = _render_sdf_rgb_rows(full_w, h, r, fill, parent_bg)
        left_rows = [row[:cap_w * 3] for row in rows]
        right_rows = [row[(full_w - cap_w) * 3:] for row in rows]
        left_photo = tk.PhotoImage(data=_encode_rgb_png(cap_w, h, left_rows), format="png", master=master)
        right_photo = tk.PhotoImage(data=_encode_rgb_png(cap_w, h, right_rows), format="png", master=master)
        _ROUNDED_CAPS_CACHE[key] = (left_photo, right_photo)
        return left_photo, right_photo
    except Exception:
        return None, None


def draw_rounded_rect_bg(canvas: tk.Canvas, w: int, h: int, r: int, fill: str, parent_bg: str, tag: str = "bg") -> None:
    """在 Canvas 上高效绘制自适应宽度的抗锯齿圆角背景（3-patch 端头切片拉伸技术）。

    1. 若 fill == parent_bg（占卡片 99% 的未选中/未悬停常态），直接清空旧图元返回，
       由 Canvas 原生背景色呈现纯色底，拖动时 0 计算开销。
    2. 若 fill != parent_bg，复用 (h, r, fill, parent_bg) 的左右端头切片缓存，
       中间以 Canvas 纯色矩形平滑拉伸；宽度变动时仅调用 coords 更新坐标（0.001ms 级），
       彻底根除拖拽调整侧边栏宽度时的剧烈卡顿。
    """
    if w < 10 or h < 10:
        return
    if fill == parent_bg:
        if canvas.find_withtag(tag):
            canvas.delete(tag)
        setattr(canvas, f"_{tag}_state", (h, r, fill, parent_bg))
        return

    if w < 4 * r:
        canvas.delete(tag)
        canvas.create_rectangle(0, 0, w, h, fill=fill, outline="", tags=tag)
        canvas.tag_lower(tag)
        setattr(canvas, f"_{tag}_state", (h, r, fill, parent_bg))
        return

    cap_w = 2 * r
    left_img, right_img = get_rounded_caps(canvas, h, r, fill, parent_bg)
    if left_img is None or right_img is None:
        canvas.delete(tag)
        canvas.create_rectangle(0, 0, w, h, fill=fill, outline="", tags=tag)
        canvas.tag_lower(tag)
        setattr(canvas, f"_{tag}_state", (h, r, fill, parent_bg))
        return

    left_item = canvas.find_withtag(f"{tag}_left")
    mid_item = canvas.find_withtag(f"{tag}_mid")
    right_item = canvas.find_withtag(f"{tag}_right")
    last_state = getattr(canvas, f"_{tag}_state", None)
    curr_state = (h, r, fill, parent_bg)

    if left_item and mid_item and right_item and last_state == curr_state:
        # 仅宽度改变，高度与颜色未变：纯坐标位移，0 毫秒极速更新
        canvas.coords(mid_item, cap_w, 0, w - cap_w, h)
        canvas.coords(right_item, w - cap_w, 0)
    else:
        canvas.delete(tag)
        canvas.create_image(0, 0, anchor="nw", image=left_img, tags=(tag, f"{tag}_left"))
        canvas.create_rectangle(cap_w, 0, w - cap_w, h, fill=fill, outline="", tags=(tag, f"{tag}_mid"))
        canvas.create_image(w - cap_w, 0, anchor="nw", image=right_img, tags=(tag, f"{tag}_right"))
        canvas.tag_lower(tag)
        setattr(canvas, f"_{tag}_state", curr_state)


def get_rounded_rect_img(master, w: int, h: int, r: int, fill: str, bg: str,
                         outline: str | None = None, outline_width: int = 1):
    """生成并缓存抗锯齿圆角矩形贴图（三级自适应管线：Tk 9.0 SVG → PIL 2x → 纯标准库 SDF PNG）。"""
    w = max(1, int(w))
    h = max(1, int(h))
    r = max(0, int(r))
    tk_id = id(getattr(master, "tk", None))
    key = (tk_id, w, h, r, fill, bg, outline, outline_width, _ALLOW_TK9_SVG, _ALLOW_PIL)
    if key in _ROUNDED_IMG_CACHE:
        return _ROUNDED_IMG_CACHE[key]
    if len(_ROUNDED_IMG_CACHE) > 384:
        for k in list(_ROUNDED_IMG_CACHE.keys())[:192]:
            _ROUNDED_IMG_CACHE.pop(k, None)

    if _has_tk9_svg(master):
        try:
            photo = _render_tk9_svg_photo(master, w, h, r, fill, bg, outline, outline_width)
            _ROUNDED_IMG_CACHE[key] = photo
            return photo
        except Exception:
            pass

    if _ALLOW_PIL:
        try:
            from PIL import Image, ImageDraw, ImageTk
            ss = 2
            W, H, R = max(1, w * ss), max(1, h * ss), max(0, min(r, w // 2, h // 2) * ss)
            im = Image.new("RGB", (W, H), bg)
            d = ImageDraw.Draw(im)
            if outline and outline_width > 0:
                d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill,
                                    outline=outline, width=outline_width * ss)
            else:
                d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill)
            im = im.resize((w, h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(im, master=master)
            _ROUNDED_IMG_CACHE[key] = photo
            return photo
        except Exception:
            pass

    try:
        png_bytes = _render_sdf_png_bytes(w, h, r, fill, bg, outline, outline_width)
        photo = tk.PhotoImage(data=png_bytes, format="png", master=master)
        _ROUNDED_IMG_CACHE[key] = photo
        return photo
    except Exception:
        return None


def get_rounded_corners(master, r: int, fill: str, parent_bg: str,
                        outline: str | None = None, outline_width: int = 1):
    """生成并缓存 9-slice 圆角容器的四个角切片 (tl, tr, bl, br)，每块尺寸 cs×cs。"""
    r = max(1, int(r))
    cs = max(3, r + 2)
    tk_id = id(getattr(master, "tk", None))
    key = (tk_id, cs, r, fill, parent_bg, outline, outline_width, _ALLOW_PIL)
    if key in _ROUNDED_CORNERS_CACHE:
        return cs, _ROUNDED_CORNERS_CACHE[key]

    S = 2 * cs + 6
    if _ALLOW_PIL:
        try:
            from PIL import Image, ImageDraw, ImageTk
            ss = 2
            W, H, R = S * ss, S * ss, r * ss
            im = Image.new("RGB", (W, H), parent_bg)
            d = ImageDraw.Draw(im)
            if outline and outline_width > 0:
                d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill,
                                    outline=outline, width=outline_width * ss)
            else:
                d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill)
            im = im.resize((S, S), Image.Resampling.LANCZOS)
            quads = (
                ImageTk.PhotoImage(im.crop((0, 0, cs, cs)), master=master),
                ImageTk.PhotoImage(im.crop((S - cs, 0, S, cs)), master=master),
                ImageTk.PhotoImage(im.crop((0, S - cs, cs, S)), master=master),
                ImageTk.PhotoImage(im.crop((S - cs, S - cs, S, S)), master=master),
            )
            _ROUNDED_CORNERS_CACHE[key] = quads
            return cs, quads
        except Exception:
            pass

    try:
        rows = _render_sdf_rgb_rows(S, S, r, fill, parent_bg, outline, outline_width)
        tl_rows = [row[:cs * 3] for row in rows[:cs]]
        tr_rows = [row[(S - cs) * 3:] for row in rows[:cs]]
        bl_rows = [row[:cs * 3] for row in rows[S - cs:]]
        br_rows = [row[(S - cs) * 3:] for row in rows[S - cs:]]
        quads = (
            tk.PhotoImage(data=_encode_rgb_png(cs, cs, tl_rows), format="png", master=master),
            tk.PhotoImage(data=_encode_rgb_png(cs, cs, tr_rows), format="png", master=master),
            tk.PhotoImage(data=_encode_rgb_png(cs, cs, bl_rows), format="png", master=master),
            tk.PhotoImage(data=_encode_rgb_png(cs, cs, br_rows), format="png", master=master),
        )
        _ROUNDED_CORNERS_CACHE[key] = quads
        return cs, quads
    except Exception:
        return cs, None


def draw_rounded_box(canvas: tk.Canvas, w: int, h: int, r: int, fill: str, parent_bg: str,
                     outline: str | None = None, outline_width: int = 1, tag: str = "box_bg") -> None:
    """9-slice 圆角细描边容器绘制器：支持任意宽高动态伸缩且缩放时 0 贴图重分配。"""
    if w < 8 or h < 8:
        return
    cs, quads = get_rounded_corners(canvas, max(1, r), fill, parent_bg, outline, outline_width)
    if quads is None or w < 2 * cs + 2 or h < 2 * cs + 2:
        canvas.delete(tag)
        img = get_rounded_rect_img(canvas, w, h, r, fill, parent_bg, outline, outline_width)
        if img is not None:
            setattr(canvas, f"_{tag}_full_img", img)
            canvas.create_image(0, 0, anchor="nw", image=img, tags=tag)
        else:
            canvas.create_rectangle(0, 0, w, h, fill=fill, outline=outline or "", tags=tag)
        canvas.tag_lower(tag)
        setattr(canvas, f"_{tag}_9state", None)
        return

    tl, tr, bl, br = quads
    ow = max(0, int(outline_width)) if outline else 0
    curr_state = (cs, r, fill, parent_bg, outline, ow)
    last_state = getattr(canvas, f"_{tag}_9state", None)

    if last_state == curr_state and canvas.find_withtag(f"{tag}_tl"):
        canvas.coords(f"{tag}_tr", w - cs, 0)
        canvas.coords(f"{tag}_bl", 0, h - cs)
        canvas.coords(f"{tag}_br", w - cs, h - cs)
        canvas.coords(f"{tag}_fc", cs, ow, w - cs, h - ow)
        canvas.coords(f"{tag}_fl", ow, cs, cs, h - cs)
        canvas.coords(f"{tag}_fr", w - cs, cs, w - ow, h - cs)
        if ow > 0:
            canvas.coords(f"{tag}_bt", cs, 0, w - cs, ow)
            canvas.coords(f"{tag}_bb", cs, h - ow, w - cs, h)
            canvas.coords(f"{tag}_blf", 0, cs, ow, h - cs)
            canvas.coords(f"{tag}_brt", w - ow, cs, w, h - cs)
        return

    canvas.delete(tag)
    setattr(canvas, f"_{tag}_quads_ref", quads)
    canvas.create_rectangle(cs, ow, w - cs, h - ow, fill=fill, outline="", tags=(tag, f"{tag}_fc"))
    canvas.create_rectangle(ow, cs, cs, h - cs, fill=fill, outline="", tags=(tag, f"{tag}_fl"))
    canvas.create_rectangle(w - cs, cs, w - ow, h - cs, fill=fill, outline="", tags=(tag, f"{tag}_fr"))
    if ow > 0 and outline:
        canvas.create_rectangle(cs, 0, w - cs, ow, fill=outline, outline="", tags=(tag, f"{tag}_bt"))
        canvas.create_rectangle(cs, h - ow, w - cs, h, fill=outline, outline="", tags=(tag, f"{tag}_bb"))
        canvas.create_rectangle(0, cs, ow, h - cs, fill=outline, outline="", tags=(tag, f"{tag}_blf"))
        canvas.create_rectangle(w - ow, cs, w, h - cs, fill=outline, outline="", tags=(tag, f"{tag}_brt"))
    canvas.create_image(0, 0, anchor="nw", image=tl, tags=(tag, f"{tag}_tl"))
    canvas.create_image(w - cs, 0, anchor="nw", image=tr, tags=(tag, f"{tag}_tr"))
    canvas.create_image(0, h - cs, anchor="nw", image=bl, tags=(tag, f"{tag}_bl"))
    canvas.create_image(w - cs, h - cs, anchor="nw", image=br, tags=(tag, f"{tag}_br"))
    canvas.tag_lower(tag)
    setattr(canvas, f"_{tag}_9state", curr_state)


def _opt_info(key: str, value) -> tuple:
    """Tk configure(key) 查询形契约：返回 5 元组
    ``(key, dbName, dbClass, defaultValue, currentValue)``。

    仓库惯例经 ``w.configure("bg")[4]``（或 ``str()``）取当前值——裸字符串
    会让 ``[4]`` 直接 TypeError（doc/gui-notes.md cget 铁律）。虚拟选项的
    dbName/dbClass/default 无真实资源库对应，填惯例近似值即可（消费方只读
    [0] 与 [4]）。"""
    cap = key[0].upper() + key[1:]
    return (key, key, cap, "", value)


def _config_dict(super_cfg: dict, overrides: dict) -> dict:
    """无参 ``configure()`` 枚举形契约：以 Tk 原生选项表为底，覆盖虚拟选项。"""
    cfg = dict(super_cfg)
    for k, v in overrides.items():
        cfg[k] = v
        if k == "bg" and "background" in cfg:
            cfg["background"] = v
        elif k == "background" and "bg" in cfg:
            cfg["bg"] = v
    return cfg


class RoundedFrame(tk.Canvas):
    """原生支持 pack/grid 几何管理的 9-slice 抗锯齿圆角细描边容器。

    作为子控件的直接父容器（子控件 pack/grid 时留出 >=4px 外边距即可露出四角圆弧与 1px 描边），
    零 create_window 嵌套，100% 兼容 winfo_manager()、自适应宽高与滚轮事件冒泡。
    """

    def __init__(self, parent, *, fill: str = theme.SURFACE_CARD,
                 bg: str | None = None, parent_bg: str | None = None,
                 outline: str | None = theme.BORDER_SUBTLE,
                 outline_width: int = 1,
                 radius: int = theme.RADIUS_L, **kw) -> None:
        kw.pop("padx", None)
        kw.pop("pady", None)
        p_bg = parent_bg if parent_bg is not None else (
            bg if bg is not None else (str(parent.cget("bg")) if hasattr(parent, "cget") else theme.BG)
        )
        super().__init__(parent, bg=p_bg, highlightthickness=0, bd=0, **kw)
        self._rf_fill = fill
        self._rf_parent_bg = p_bg
        self._rf_outline = outline
        self._rf_outline_w = outline_width
        self._rf_radius = radius
        self.bind("<Configure>", self._rf_on_configure, add="+")

    def _rf_on_configure(self, event=None) -> None:
        w = event.width if event is not None else max(self.winfo_width(), self.winfo_reqwidth())
        h = event.height if event is not None else max(self.winfo_height(), self.winfo_reqheight())
        if w >= 8 and h >= 8:
            draw_rounded_box(
                self, w, h, self._rf_radius, self._rf_fill, self._rf_parent_bg,
                outline=self._rf_outline, outline_width=self._rf_outline_w, tag="rf_bg"
            )

    def set_surface(self, *, fill: str | None = None, outline: str | None = ...) -> None:
        if fill is not None:
            self._rf_fill = fill
        if outline is not ...:
            self._rf_outline = outline
        self._rf_on_configure()

    def cget(self, key: str):
        if key in ("bg", "background"):
            return self._rf_fill
        return super().cget(key)

    def configure(self, cnf=None, **kw):
        # 四种调用形全兼容：dict / kwargs / 单键查询（返回 5 元组）/ 无参枚举
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if isinstance(cnf, str):
            if cnf in ("bg", "background"):
                return _opt_info("background", self._rf_fill)
            if cnf == "outline":
                return _opt_info("outline", self._rf_outline)
            return super().configure(cnf)
        if "bg" in kw or "background" in kw:
            self._rf_fill = str(kw.pop("bg", kw.pop("background", self._rf_fill)))
            self._rf_on_configure()
        if "outline" in kw:
            self._rf_outline = kw.pop("outline")
            self._rf_on_configure()
        if kw:
            return super().configure(**kw)
        return _config_dict(super().configure(), {
            "bg": _opt_info("background", self._rf_fill),
        })

    config = configure


_FIT_FONTS: dict = {}


def entry_box(parent, *, font=theme.FONT_UI, width: int | None = None,
              bg: str = theme.CONTROL_BG, pad: int = 4) -> tk.Entry:
    """带内衬的扁平输入框：Entry 包在同色 Frame 里留出 ``pad`` 像素内边距
    （tk.Entry 无 padx；不用 highlight 边框——style.md 禁边框表达状态）。

    返回 Entry；布局请对 ``entry.box``（外层 Frame）调用 pack / grid。"""
    box = tk.Frame(parent, bg=bg)
    kw = {"width": width} if width is not None else {}
    ent = tk.Entry(box, bg=bg, fg=theme.FG, insertbackground=theme.FG, font=font,
                   relief="flat", bd=0, highlightthickness=0, **kw)
    ent.pack(fill="both", expand=True, padx=pad + 2, pady=pad)
    ent.box = box  # type: ignore[attr-defined]
    return ent


def to_int(v) -> int:
    """Tcl 返回值转 int。CI 打包版（GitHub Actions Python 3.11 冻结）里
    ``cget()`` 部分选项返回 ``Tcl_Obj`` 包装而非 int/str，直接 ``int()`` 抛
    TypeError（v2.0.0 Windows exe 启动即崩，源码 3.14 运行不复现）；经
    ``str()`` 取字符串表示后转换，源码 / 打包两种环境结果一致。"""
    return int(str(v))


def fit_text(font, text: str, width: int) -> str:
    """把 text 按像素宽截到 ``width`` 内，超出则末尾加「…」（二分查找）。"""
    if width <= 0 or font.measure(text) <= width:
        return text
    budget = width - font.measure("…")
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def fit_label(label: tk.Label, text: str) -> tk.Label:
    """标签文字按实际像素宽截断加省略号，布局宽度变化时自动重算。

    按字符数截断会切在半个字上、且中英文字宽不同误差大（侧栏卡片标题曾出现
    「hover扌」「3.4 小」这类半截字）。标签设 ``width=1`` 不按全文请求宽度，
    须以 ``fill="x", expand=True`` 布局，由容器分配宽度后再截。"""
    import tkinter.font as tkfont
    spec = str(label.cget("font"))
    font = _FIT_FONTS.get(spec)
    if font is None:
        font = _FIT_FONTS[spec] = tkfont.Font(font=spec)
    label._fit_full = text  # type: ignore[attr-defined]

    def refit(_e=None) -> None:
        w = label.winfo_width() - 2 * to_int(label.cget("padx")) - 4
        full = label._fit_full  # type: ignore[attr-defined]
        txt = full if w <= 8 else fit_text(font, full, w)
        if str(label.cget("text")) != txt:
            label.configure(text=txt)

    if not getattr(label, "_fit_bound", False):
        label.bind("<Configure>", refit, add="+")
        label._fit_bound = True  # type: ignore[attr-defined]
    label.configure(width=1, text=text)
    refit()
    return label


class FlowRow(tk.Frame):
    """流式换行容器：子控件按加入顺序横排，超出宽度自动折行。

    子控件须以本容器为 parent 创建，经 ``add`` 加入、``set_visible`` 显隐
    （勿再 pack/grid）。用 place 定位 + 显式设高——pack 单行排不下时会把
    末尾控件直接裁掉（窄侧栏里的过滤胶囊就是这么消失的）。
    """

    def __init__(self, parent, *, gap_x: int = 2, gap_y: int = 2, **kw) -> None:
        super().__init__(parent, **kw)
        self._gap_x, self._gap_y = gap_x, gap_y
        self._items: list[tk.Widget] = []
        self._hidden: set[tk.Widget] = set()
        self.bind("<Configure>", lambda _e: self.relayout(), add="+")

    def add(self, widget: tk.Widget) -> tk.Widget:
        self._items.append(widget)
        self.relayout()
        return widget

    def set_visible(self, widget: tk.Widget, visible: bool) -> None:
        if visible:
            self._hidden.discard(widget)
        else:
            self._hidden.add(widget)
        self.relayout()

    def relayout(self) -> None:
        avail = self.winfo_width()
        if avail <= 1:  # 尚未排版：先按单行摆，<Configure> 到来后重排
            avail = 1 << 30
        x = y = row_h = 0
        for w in self._items:
            if w in self._hidden:
                w.place_forget()
                continue
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x and x + ww > avail:
                x, y, row_h = 0, y + row_h + self._gap_y, 0
            w.place(x=x, y=y)
            x += ww + self._gap_x
            row_h = max(row_h, wh)
        height = max(y + row_h, 1)
        if to_int(self.cget("height")) != height:
            self.configure(height=height)


class RoundedPill(tk.Canvas):
    """带抗锯齿圆角的胶囊按钮/标签：自绘圆角底，支持图标、细描边与文字。"""

    def __init__(self, parent, text: str = "", icon=None, command=None, *,
                 width: int = 76, height: int = 22, radius: int = theme.RADIUS_M,
                 fill: str = theme.CONTROL_BG, hover_fill: str = theme.HOVER_BG,
                 bg: str = theme.PANEL, fg: str = theme.FG,
                 outline: str | None = None,
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
        self._outline = outline
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
            import tkinter.font as tkfont
            try:
                txt_w = tkfont.Font(font=self._font).measure(self._text)
                ico_extra = (getattr(self._icon, "width", lambda: 16)() + 4) if self._icon else 0
                needed_w = txt_w + ico_extra + 18
                if needed_w > self._width_px:
                    self._width_px = needed_w
                    super().configure(width=needed_w)
            except Exception:
                pass
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
                                          self._radius, self._fill, self._bg_col,
                                          outline=self._outline)
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
    """带抗锯齿圆角与焦点细描边的搜索框：底板为圆角底，内嵌搜索图标与扁平 Entry。"""

    def __init__(self, parent, textvariable, *, width: int = 110, height: int = 24,
                 radius: int = theme.RADIUS_M, fill: str = theme.CONTROL_BG,
                 bg: str = theme.SECTION_HEADER_BG, fg: str = theme.FG,
                 outline: str | None = theme.BORDER_SUBTLE,
                 icon=None, **kw) -> None:
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, **kw)
        self._width_px = width
        self._height_px = height
        self._radius = radius
        self._fill = fill
        self._bg_col = bg
        self._fg = fg
        self._outline = outline
        self._focused = False
        self._icon = icon

        self.entry = tk.Entry(self, textvariable=textvariable, bg=fill, fg=fg,
                              insertbackground=fg, relief="flat", borderwidth=0,
                              highlightthickness=0, font=theme.FONT_UI)
        def _clear(_e=None):
            textvariable.set("")
            self.entry.delete(0, "end")
            return "break"
        self.entry.bind("<Escape>", _clear)
        self.entry.bind("<FocusIn>", self._on_focus_in, add="+")
        self.entry.bind("<FocusOut>", self._on_focus_out, add="+")
        self.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.bind("<Configure>", self._on_configure)
        self._redraw()

    def _on_focus_in(self, _e=None) -> None:
        if not self._focused:
            self._focused = True
            self._redraw()

    def _on_focus_out(self, _e=None) -> None:
        if self._focused:
            self._focused = False
            self._redraw()

    def _on_configure(self, event) -> None:
        if event.width > 20 and event.width != self._width_px:
            self._width_px = event.width
            self._redraw()

    def _redraw(self) -> None:
        self.delete("bg")
        self.delete("icon")
        cur_outline = theme.ACCENT if self._focused else self._outline
        draw_rounded_box(self, self._width_px, self._height_px,
                         self._radius, self._fill, self._bg_col,
                         outline=cur_outline, tag="bg")
        self.tag_lower("bg")
        x_entry = 8
        if self._icon:
            self.create_image(6, self._height_px // 2, anchor="w", image=self._icon, tags="icon")
            ico_w = getattr(self._icon, "width", lambda: 16)()
            x_entry = 6 + ico_w + 4
        entry_w = max(12, self._width_px - x_entry - 6)
        if not self.find_withtag("entry_win"):
            self.create_window(x_entry, self._height_px // 2, anchor="w",
                               window=self.entry, width=entry_w, tags="entry_win")
        else:
            self.coords("entry_win", x_entry, self._height_px // 2)
            self.itemconfigure("entry_win", width=entry_w)
        self.tag_bind("bg", "<Button-1>", lambda _e: self.entry.focus_set())
        self.tag_bind("icon", "<Button-1>", lambda _e: self.entry.focus_set())

class RoundedKpiChip(tk.Canvas):
    """带抗锯齿圆角与 1px 细描边的头部 KPI 徽标胶囊：标签灰度小字 + 数值粗体，支持悬停提亮与点击回调。"""

    def __init__(self, parent, label: str, value: str = "-", *, command=None,
                 tip: str = "", width: int = 95, height: int = 24, radius: int = theme.RADIUS_M,
                 fill: str = theme.SURFACE_CARD, hover_fill: str = theme.HOVER_BG,
                 outline: str | None = theme.BORDER_SUBTLE,
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
        self._outline = outline
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
            return self._bg_col  # 稳定父底色（_fill 随 hover 变化，不是画布 bg）
        if attr in ("fg", "foreground"):
            return self._fg
        if attr == "text":
            return f"{self._value} {self._extra}".strip()
        return super().cget(attr)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if isinstance(cnf, str):
            if cnf == "text":
                return _opt_info("text", f"{self._value} {self._extra}".strip())
            if cnf in ("fg", "foreground"):
                return _opt_info("foreground", self._fg)
            if cnf in ("bg", "background"):
                return _opt_info("background", self._bg_col)
            return super().configure(cnf)
        if "text" in kw:
            self._value = kw.pop("text")
        if "fg" in kw:
            self._fg = kw.pop("fg")
        self._redraw()
        if kw:
            return super().configure(**kw)
        return _config_dict(super().configure(), {
            "text": _opt_info("text", f"{self._value} {self._extra}".strip()),
            "fg": _opt_info("foreground", self._fg),
            "bg": _opt_info("background", self._bg_col),
        })

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
                                          self._radius, self._fill, self._bg_col,
                                          outline=self._outline)
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


class StatusBadge(tk.Canvas):
    """圆角细描边状态徽标胶囊（对标 OpenResearch 状态签：暗色微光底 + 1px 同色相细框 + 高清语义字）。"""

    KINDS: dict[str, tuple[str, str, str]] = {
        "neutral": (theme.SURFACE_CARD, theme.BORDER_SUBTLE, theme.MUTED),
        "success": (theme.SUCCESS_TINT_BG, theme.SUCCESS_TINT_BORDER, theme.SUCCESS),
        "warning": (theme.WARN_TINT_BG, theme.WARN_TINT_BORDER, theme.WARNING),
        "error": (theme.ERROR_TINT_BG, theme.ERROR_TINT_BORDER, theme.ERROR),
        "info": (theme.INFO_TINT_BG, theme.INFO_TINT_BORDER, theme.VENDOR_BADGE_FG),
    }

    def __init__(self, parent, text: str = "", *, kind: str = "neutral",
                 tone: str | None = None,
                 dot: bool = False, height: int = 22, padx: int = 9,
                 radius: int = theme.RADIUS_FULL, bg: str | None = None,
                 font=theme.FONT_UI_SMALL, command=None, tip: str = "", **kw) -> None:
        eff_kind = tone if tone is not None else kind
        p_bg = bg if bg is not None else (str(parent.cget("bg")) if hasattr(parent, "cget") else theme.BG)
        super().__init__(parent, width=20, height=height, bg=p_bg,
                         highlightthickness=0, bd=0,
                         cursor=CLICK_CURSOR if command else "arrow", **kw)
        self._bg_col = p_bg
        self._text = text
        self._kind = eff_kind if eff_kind in self.KINDS else "neutral"
        self._dot = dot
        self._height_px = height
        self._padx = padx
        self._radius = radius
        self._font = font
        self._custom_fg: str | None = None
        self._command = command
        if command:
            self.bind("<Button-1>", lambda _e: command())
        if tip:
            Tooltip(self, tip)
        self._redraw()

    def set_status(self, text: str, *, kind: str | None = None, dot: bool | None = None) -> None:
        self._text = text
        if kind is not None and kind in self.KINDS:
            self._kind = kind
            self._custom_fg = None
        if dot is not None:
            self._dot = dot
        self._redraw()

    def cget(self, attr: str):
        if attr == "text":
            return self._text
        if attr in ("fg", "foreground"):
            return self._custom_fg or self.KINDS[self._kind][2]
        if attr in ("bg", "background"):
            return self._bg_col  # 构造时的父底色（KINDS 色是画出来的胶囊面，不是画布底）
        return super().cget(attr)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if isinstance(cnf, str):
            if cnf == "text":
                return _opt_info("text", self._text)
            if cnf in ("fg", "foreground"):
                return _opt_info("foreground", self._custom_fg or self.KINDS[self._kind][2])
            if cnf in ("bg", "background"):
                return _opt_info("background", self._bg_col)
            return super().configure(cnf)
        if "text" in kw:
            self._text = str(kw.pop("text"))
        if "kind" in kw:
            k = str(kw.pop("kind"))
            if k in self.KINDS:
                self._kind = k
                self._custom_fg = None
        if "fg" in kw or "foreground" in kw:
            fg_v = str(kw.pop("fg", kw.pop("foreground", "")))
            self._custom_fg = fg_v
            if fg_v == theme.ERROR:
                self._kind = "error"
            elif fg_v == theme.SUCCESS:
                self._kind = "success"
            elif fg_v == theme.WARNING:
                self._kind = "warning"
            elif fg_v in (theme.ACCENT, theme.VENDOR_BADGE_FG):
                self._kind = "info"
            elif fg_v == theme.MUTED:
                self._kind = "neutral"
        if "bg" in kw or "background" in kw:
            self._bg_col = str(kw.pop("bg", kw.pop("background", self._bg_col)))
        self._redraw()
        if kw:
            return super().configure(**kw)
        return _config_dict(super().configure(), {
            "text": _opt_info("text", self._text),
            "fg": _opt_info("foreground", self._custom_fg or self.KINDS[self._kind][2]),
            "bg": _opt_info("background", self._bg_col),
        })

    config = configure

    def _redraw(self) -> None:
        import tkinter.font as tkfont

        self.delete("all")
        if not self._text:
            super().configure(width=1)
            return
        fill, border, kind_fg = self.KINDS[self._kind]
        fg = self._custom_fg or kind_fg
        txt_w = tkfont.Font(font=self._font).measure(self._text)
        dot_extra = 11 if self._dot else 0
        w = max(24, txt_w + dot_extra + self._padx * 2)
        h = self._height_px
        super().configure(width=w, height=h)
        self._img = get_rounded_rect_img(self, w, h, min(self._radius, h // 2),
                                         fill, self._bg_col, outline=border)
        if self._img is not None:
            self.create_image(0, 0, anchor="nw", image=self._img)
        x = self._padx
        if self._dot:
            self._dot_img = get_rounded_rect_img(self, 6, 6, 3, fg, fill)
            if self._dot_img is not None:
                self.create_image(x, (h - 6) // 2, anchor="nw", image=self._dot_img)
            x += 11
        self.create_text(x, h // 2, anchor="w", text=self._text, fill=fg, font=self._font)


class ToggleSwitch(tk.Canvas):
    """36x20px 平滑抗锯齿拨杆开关（绑定 BooleanVar，支持点击与悬停微光）。"""

    def __init__(self, parent, variable: tk.BooleanVar, on_toggle=None, *,
                 command=None,
                 width: int = 36, height: int = 20, bg: str | None = None,
                 on_fill: str = theme.ACCENT, off_fill: str = theme.CONTROL_BG,
                 outline: str | None = theme.BORDER_SUBTLE,
                 tooltip: str | None = None, **kw) -> None:
        p_bg = bg if bg is not None else (str(parent.cget("bg")) if hasattr(parent, "cget") else theme.SURFACE_CARD)
        super().__init__(parent, width=width, height=height, bg=p_bg,
                         highlightthickness=0, bd=0, cursor=CLICK_CURSOR, **kw)
        self.var = variable
        self._on_toggle = on_toggle if on_toggle is not None else command
        self._width_px = width
        self._height_px = height
        self._bg_col = p_bg
        self._on_fill = on_fill
        self._off_fill = off_fill
        self._outline = outline
        self._hovered = False
        self._disabled = False

        self.bind("<Button-1>", lambda _e: self.click())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        if tooltip:
            Tooltip(self, tooltip)
        self._trace_id = None
        self._attach_var_trace()
        # tk Variable 以 PY_VARn 活在解释器级，widget 销毁后 trace 闭包仍强引用
        # 整棵子树（弹窗反复开关 = 累积泄漏）——销毁时显式解除。
        self.bind("<Destroy>", self._release_var_trace, add="+")
        self._redraw()

    def _attach_var_trace(self) -> None:
        try:
            self._trace_id = self.var.trace_add("write", lambda *_: self._safe_redraw())
        except Exception:
            self._trace_id = None

    def _release_var_trace(self, e=None) -> None:
        # <Destroy> 对每个后代销毁都冒泡触发，只响应自身那次
        if e is not None and e.widget is not self:
            return
        if getattr(self, "_trace_id", None) is not None:
            try:
                self.var.trace_remove("write", self._trace_id)
            except Exception:
                pass
            self._trace_id = None

    def get(self) -> bool:
        return bool(self.var.get())

    def set(self, val: bool) -> None:
        self.var.set(bool(val))
        self._redraw()

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        if "state" in kw:
            st = str(kw.pop("state"))
            self._disabled = (st == "disabled")
            super().configure(cursor="arrow" if self._disabled else CLICK_CURSOR)
            self._redraw()
        if kw:
            return super().configure(**kw)
        return None

    config = configure

    def _safe_redraw(self) -> None:
        try:
            if self.winfo_exists():
                self._redraw()
        except tk.TclError:
            pass

    def click(self) -> None:
        if self._disabled:
            return
        self.var.set(not bool(self.var.get()))
        self._redraw()
        if self._on_toggle:
            self._on_toggle()

    toggle = click

    def _draw(self) -> None:
        self._redraw()

    def _on_enter(self, _e=None) -> None:
        self._hovered = True
        self._redraw()

    def _on_leave(self, _e=None) -> None:
        self._hovered = False
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        on = bool(self.var.get())
        if on:
            track = theme.HOVER_ACCENT if self._hovered else self._on_fill
            border = None
            knob_col = theme.FG_WHITE
        else:
            track = theme.HOVER_BG if self._hovered else self._off_fill
            border = self._outline
            knob_col = theme.FG_WHITE if self._hovered else theme.FG

        self._track_img = get_rounded_rect_img(
            self, self._width_px, self._height_px, self._height_px // 2, track, self._bg_col, outline=border
        )
        if self._track_img is not None:
            self.create_image(0, 0, anchor="nw", image=self._track_img)

        pad = 3
        kd = max(6, self._height_px - 2 * pad)
        kx = (self._width_px - pad - kd) if on else pad
        self._knob_img = get_rounded_rect_img(self, kd, kd, kd // 2, knob_col, track)
        if self._knob_img is not None:
            self.create_image(kx, pad, anchor="nw", image=self._knob_img)


class SegmentedControl(tk.Canvas):
    """圆角分段胶囊切换器（对标 OpenResearch 主题/视图切换：细描边底槽 + 凸起圆角激活滑块）。"""

    def __init__(self, parent, options: list[tuple[str, str]],
                 variable: tk.StringVar | None = None, *,
                 value: str | None = None, on_change=None,
                 height: int = 26, pad: int = 2, item_padx: int = 11,
                 bg: str | None = None, track_fill: str = theme.PANEL,
                 active_fill: str = theme.CONTROL_BG,
                 active_fg: str = theme.FG_WHITE, inactive_fg: str = theme.MUTED,
                 outline: str | None = theme.BORDER_SUBTLE,
                 radius: int = theme.RADIUS_M,
                 font=theme.FONT_UI_SMALL, **kw) -> None:
        import tkinter.font as tkfont

        p_bg = bg if bg is not None else (str(parent.cget("bg")) if hasattr(parent, "cget") else theme.SURFACE_CARD)
        self._items = list(options)
        self._var = variable
        self._on_change = on_change
        self._height_px = height
        self._pad = pad
        self._item_padx = item_padx
        self._bg_col = p_bg
        self._track_fill = track_fill
        self._active_fill = active_fill
        self._active_fg = active_fg
        self._inactive_fg = inactive_fg
        self._outline = outline
        self._radius = radius
        self._font = font
        self._hover_idx: int | None = None

        init_val = variable.get() if variable is not None else (value if value is not None else (options[0][0] if options else ""))
        valid_keys = {k for k, _ in self._items}
        if init_val not in valid_keys and self._items:
            init_val = self._items[0][0]
        self._value = init_val

        f_meas = tkfont.Font(font=font)
        bold_spec = (font[0], font[1], "bold") if len(font) == 2 else font
        self._active_font = bold_spec
        f_bold = tkfont.Font(font=bold_spec)
        self._seg_widths = [max(f_meas.measure(lbl), f_bold.measure(lbl)) + 2 * item_padx for _, lbl in self._items]
        total_w = max(24, sum(self._seg_widths) + 2 * pad)

        super().__init__(parent, width=total_w, height=height, bg=p_bg,
                         highlightthickness=0, bd=0, cursor=CLICK_CURSOR, **kw)
        self._width_px = total_w
        self._seg_bounds: list[tuple[int, int]] = []

        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self._var_trace_id = None
        self._attach_var_trace()
        # 同 ToggleSwitch：Variable 活在解释器级，销毁时解除 trace 防泄漏
        self.bind("<Destroy>", self._release_var_trace, add="+")
        self._redraw()

    def _attach_var_trace(self) -> None:
        if self._var is None:
            return
        try:
            self._var_trace_id = self._var.trace_add("write", lambda *_: self._sync_from_var())
        except Exception:
            self._var_trace_id = None

    def _release_var_trace(self, e=None) -> None:
        if e is not None and e.widget is not self:
            return
        if getattr(self, "_var_trace_id", None) is not None:
            try:
                self._var.trace_remove("write", self._var_trace_id)
            except Exception:
                pass
            self._var_trace_id = None

    def get(self) -> str:
        return self._var.get() if self._var is not None else self._value

    def set(self, val: str, *, notify: bool = False) -> None:
        if val == self._value and (self._var is None or self._var.get() == val):
            return
        self._value = val
        if self._var is not None and self._var.get() != val:
            self._var.set(val)
        self._redraw()
        if notify and self._on_change:
            self._on_change(val)

    def _sync_from_var(self) -> None:
        if self._var is None:
            return
        try:
            if not self.winfo_exists():
                return
            v = self._var.get()
            if v != self._value:
                self._value = v
                self._redraw()
        except tk.TclError:
            pass

    def _idx_at(self, x: int) -> int | None:
        for i, (x0, x1) in enumerate(self._seg_bounds):
            if x0 <= x < x1:
                return i
        return None

    def _on_motion(self, event) -> None:
        idx = self._idx_at(event.x)
        if idx != self._hover_idx:
            self._hover_idx = idx
            self._redraw()

    def _on_leave(self, _event=None) -> None:
        if self._hover_idx is not None:
            self._hover_idx = None
            self._redraw()

    def _on_click(self, event) -> None:
        idx = self._idx_at(event.x)
        if idx is None or idx >= len(self._items):
            return
        key = self._items[idx][0]
        if key == self.get():
            return
        self._value = key
        if self._var is not None and self._var.get() != key:
            self._var.set(key)
        self._redraw()
        if self._on_change:
            self._on_change(key)

    def _redraw(self) -> None:
        self.delete("all")
        self._track_img = get_rounded_rect_img(
            self, self._width_px, self._height_px, self._radius, self._track_fill, self._bg_col, outline=self._outline
        )
        if self._track_img is not None:
            self.create_image(0, 0, anchor="nw", image=self._track_img)

        cur = self.get()
        inner_h = max(10, self._height_px - 2 * self._pad)
        inner_r = max(2, self._radius - 1)
        x = self._pad
        self._seg_bounds = []
        self._pill_imgs = []
        for i, ((key, lbl), sw) in enumerate(zip(self._items, self._seg_widths)):
            x0, x1 = x, x + sw
            self._seg_bounds.append((x0, x1))
            is_act = (key == cur)
            is_hov = (i == self._hover_idx and not is_act)
            if is_act or is_hov:
                p_fill = self._active_fill if is_act else theme.HOVER_BG
                p_img = get_rounded_rect_img(self, sw, inner_h, inner_r, p_fill, self._track_fill)
                if p_img is not None:
                    self._pill_imgs.append(p_img)
                    self.create_image(x0, self._pad, anchor="nw", image=p_img)
            fg = self._active_fg if is_act else (theme.FG if is_hov else self._inactive_fg)
            fnt = self._active_font if is_act else self._font
            self.create_text((x0 + x1) // 2, self._height_px // 2, anchor="center",
                             text=lbl, fill=fg, font=fnt)
            x = x1


class RoundedEntry(tk.Frame):
    """圆角细描边单行输入框：常态 1px BORDER_SUBTLE，持焦切换为 1px ACCENT，兼容 tk.Entry 常用接口。"""

    def __init__(self, parent, *, textvariable: tk.StringVar | None = None,
                 width: int | None = None, height: int = 30,
                 bg: str | None = None, fill: str = theme.CONTROL_BG,
                 fg: str = theme.FG, font=theme.FONT_UI,
                 radius: int = theme.RADIUS_M,
                 placeholder: str = "", show: str = "", **kw) -> None:
        import tkinter.font as tkfont

        p_bg = bg if bg is not None else (str(parent.cget("bg")) if hasattr(parent, "cget") else theme.SURFACE_CARD)
        char_w = tkfont.Font(font=font).measure("0")
        px_w = max(80, int(width * char_w + 20)) if width is not None else 180
        super().__init__(parent, bg=p_bg, highlightthickness=0, bd=0, **kw)
        self._width_px = px_w
        self._height_px = height
        self._bg_col = p_bg
        self._fill = fill
        self._fg = fg
        self._radius = radius
        self._focused = False
        self._placeholder = placeholder
        self._var = textvariable
        self.box = self

        self._canvas = tk.Canvas(self, width=px_w, height=height, bg=p_bg,
                                 highlightthickness=0, bd=0, cursor="xterm")
        self._canvas.pack(fill="both", expand=True)

        ent_kw: dict = {
            "bg": fill,
            "fg": fg,
            "insertbackground": fg,
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 0,
            "font": font,
        }
        if textvariable is not None:
            ent_kw["textvariable"] = textvariable
        if show:
            ent_kw["show"] = show
        self.entry = tk.Entry(self._canvas, **ent_kw)

        self._ph_lbl: tk.Label | None = None
        if placeholder:
            self._ph_lbl = tk.Label(self._canvas, text=placeholder, bg=fill, fg=theme.MUTED,
                                    font=font, anchor="w", cursor="xterm")
            self._ph_lbl.bind("<Button-1>", lambda _e: self.entry.focus_set())

        self.entry.bind("<FocusIn>", self._on_focus_in, add="+")
        self.entry.bind("<FocusOut>", self._on_focus_out, add="+")
        self.entry.bind("<KeyRelease>", lambda _e: self._sync_placeholder(), add="+")
        self._var_trace_id = None
        self._attach_var_trace()
        # 同 ToggleSwitch：Variable 活在解释器级，销毁时解除 trace 防泄漏
        self.bind("<Destroy>", self._release_var_trace, add="+")
        self._canvas.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self._canvas.bind("<Configure>", self._on_configure)
        self._redraw()
        self._sync_placeholder()

    def _attach_var_trace(self) -> None:
        if self._var is None:
            return
        try:
            self._var_trace_id = self._var.trace_add("write", lambda *_: self._sync_placeholder())
        except Exception:
            self._var_trace_id = None

    def _release_var_trace(self, e=None) -> None:
        if e is not None and e.widget is not self:
            return
        if getattr(self, "_var_trace_id", None) is not None:
            try:
                self._var.trace_remove("write", self._var_trace_id)
            except Exception:
                pass
            self._var_trace_id = None

    def _on_focus_in(self, _e=None) -> None:
        self._focused = True
        self._redraw()
        self._sync_placeholder()

    def _on_focus_out(self, _e=None) -> None:
        self._focused = False
        self._redraw()
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        if self._ph_lbl is None:
            return
        try:
            if not self.winfo_exists():
                return
            empty = (self.entry.get() == "")
            if empty and not self._focused:
                self._ph_lbl.place(x=10, rely=0.5, anchor="w")
            else:
                self._ph_lbl.place_forget()
        except tk.TclError:
            pass

    def _on_configure(self, event) -> None:
        if event.width > 16 and event.width != self._width_px:
            self._width_px = event.width
            self._redraw()

    def _redraw(self) -> None:
        outline = theme.ACCENT if self._focused else theme.BORDER_SUBTLE
        draw_rounded_box(self._canvas, self._width_px, self._height_px, self._radius, self._fill, self._bg_col,
                         outline=outline, tag="re_bg")
        self._canvas.tag_lower("re_bg")
        ew = max(12, self._width_px - 20)
        if not self._canvas.find_withtag("re_win"):
            self._canvas.create_window(10, self._height_px // 2, anchor="w", window=self.entry, width=ew, tags="re_win")
        else:
            self._canvas.coords("re_win", 10, self._height_px // 2)
            self._canvas.itemconfigure("re_win", width=ew)
        self._canvas.tag_bind("re_bg", "<Button-1>", lambda _e: self.entry.focus_set())

    # 透明代理 tk.Entry 常用读写接口，无缝替换裸 Entry / entry_box
    def get(self) -> str:
        return self.entry.get()

    def insert(self, index, string: str) -> None:
        self.entry.insert(index, string)
        self._sync_placeholder()

    def delete(self, first, last=None) -> None:
        self.entry.delete(first, last)
        self._sync_placeholder()

    def focus_set(self) -> None:
        self.entry.focus_set()

    def icursor(self, index) -> None:
        self.entry.icursor(index)

    def selection_range(self, start, end) -> None:
        self.entry.selection_range(start, end)

    def select_range(self, start, end) -> None:
        self.entry.select_range(start, end)

    def bind(self, sequence=None, func=None, add=None):
        if sequence and (sequence.startswith("<Key") or sequence in ("<Return>", "<FocusIn>", "<FocusOut>")):
            return self.entry.bind(sequence, func, add)
        return super().bind(sequence, func, add)


class SurfaceCard(tk.Frame):
    """分组圆角细描边卡片（对标 OpenResearch 设置分组：外部清爽小节标题 + 9-slice 1px BORDER_SUBTLE 容器）。"""

    def __init__(self, parent, title: str = "", *, subtitle: str = "",
                 bg: str = theme.SURFACE_CARD, parent_bg: str | None = None,
                 outline: str | None = theme.BORDER_SUBTLE,
                 radius: int = theme.RADIUS_L,
                 padx: int = 14, pady: int = 12,
                 pad_x: int | None = None, pad_y: int | None = None) -> None:
        # 不再静默改写 bg：调用方传什么画什么（曾有 bg==BG → SURFACE_CARD 的
        # 偷换特判，调用方以为在画 BG 底时被换色，已全部调用点显式化）。
        p_bg = parent_bg if parent_bg is not None else (str(parent.cget("bg")) if hasattr(parent, "cget") else theme.BG)
        super().__init__(parent, bg=p_bg)
        self.surface_bg = bg
        self._parent_bg = p_bg
        self._outline = outline
        self._radius = radius
        self._padx = pad_x if pad_x is not None else padx
        self._pady = pad_y if pad_y is not None else pady

        self.header_row: tk.Frame | None = None
        self.header_extra: tk.Frame | None = None
        self.title_lbl: tk.Label | None = None
        if title or subtitle:
            self.header_row = tk.Frame(self, bg=p_bg)
            self.header_row.pack(fill="x", pady=(0, 6))
            self.header_extra = tk.Frame(self.header_row, bg=p_bg)
            self.header_extra.pack(side="right")
            if title:
                self.title_lbl = tk.Label(self.header_row, text=title, bg=p_bg,
                                          fg=theme.FG_WHITE, font=theme.FONT_HEADING, anchor="w")
                self.title_lbl.pack(side="left")
            if subtitle:
                tk.Label(self.header_row, text=subtitle, bg=p_bg,
                         fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w").pack(side="left", padx=(8, 0))

        self.canvas = tk.Canvas(self, bg=p_bg, height=2 * pady + 16, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._body_win = self.canvas.create_window(padx, pady, window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_body_configure(self, _event=None) -> None:
        req_h = max(2 * self._radius + 6, self.body.winfo_reqheight() + 2 * self._pady)
        if to_int(self.canvas.cget("height")) != req_h:
            self.canvas.configure(height=req_h)
        w = self.canvas.winfo_width()
        if w >= 12:
            draw_rounded_box(self.canvas, w, req_h, self._radius, self.surface_bg,
                             self._parent_bg, outline=self._outline, tag="sc_bg")

    def _on_canvas_configure(self, event) -> None:
        w, h = event.width, event.height
        if w < 12 or h < 12:
            return
        bw = max(1, w - 2 * self._padx)
        if getattr(self, "_last_bw", None) != bw:
            self._last_bw = bw
            self.canvas.itemconfigure(self._body_win, width=bw)
        draw_rounded_box(self.canvas, w, h, self._radius, self.surface_bg,
                         self._parent_bg, outline=self._outline, tag="sc_bg")

    def add_row(self, title: str, subtitle: str = "", *, tip: str = "", pady: int = 6) -> tk.Frame:
        """声明式添加左文案说明 + 右对齐控件槽的标准设置行，返回右侧控件父容器。"""
        row = tk.Frame(self.body, bg=self.surface_bg)
        row.pack(fill="x", pady=pady)
        right = tk.Frame(row, bg=self.surface_bg)
        right.pack(side="right", padx=(12, 0))
        left = tk.Frame(row, bg=self.surface_bg)
        left.pack(side="left", fill="x", expand=True)
        t_lbl = tk.Label(left, text=title, bg=self.surface_bg, fg=theme.FG,
                         font=theme.FONT_UI, anchor="w", justify="left")
        t_lbl.pack(fill="x", anchor="w")
        s_lbl = None
        if subtitle:
            s_lbl = tk.Label(left, text=subtitle, bg=self.surface_bg, fg=theme.MUTED,
                             font=theme.FONT_UI_SMALL, anchor="w", justify="left", wraplength=420)
            s_lbl.pack(fill="x", anchor="w", pady=(2, 0))
            left.bind("<Configure>", lambda e, lb=s_lbl: lb.configure(wraplength=max(120, e.width - 4)), add="+")
        if tip:
            Tooltip(t_lbl, tip)
            if s_lbl is not None:
                Tooltip(s_lbl, tip)
        right.row = row  # type: ignore[attr-defined]
        right.title_lbl = t_lbl  # type: ignore[attr-defined]
        return right

    def add_divider(self, pady: int = 6) -> tk.Frame:
        """插入 1px 极淡行间分隔线。"""
        sep = tk.Frame(self.body, bg=theme.DIVIDER, height=1)
        sep.pack(fill="x", pady=pady)
        return sep


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


# ---------------------------------------------------------------------------
# 统一模态对话框（替代 tkinter.messagebox 的系统样式）
# ---------------------------------------------------------------------------
_DIALOG_KINDS = {
    # kind → (徽标文案, StatusBadge kind, 警示符号, 符号色)
    "info":    ("提示", "info", "", ""),
    "success": ("完成", "success", "", ""),
    "warning": ("注意", "warning", "⚠", theme.WARNING),
    "error":   ("出错", "error", "⚠", theme.ERROR),
    "confirm": ("请确认", "warning", "", ""),
    "danger":  ("高危操作", "error", "⚠", theme.DANGER),
}


def message_dialog(parent, title, message, *, kind="info", detail="",
                   buttons=(("ok", "知道了", "accent"),),
                   scroll=False) -> "str | None":
    """统一样式模态对话框——替代 tkinter.messagebox（系统样式与主题割裂）。

    样式对齐 ConfirmDeletePopup：留白页头（语义徽标 + 警示符号 + 标题）+
    SurfaceCard 正文卡（message 可选中复制，detail 次要说明）+ 右对齐按钮行。
    buttons 为 ``(key, 文案, 样式)`` 三元组列表，样式 "accent"（强调主操作）/
    "danger"（红色警示）/ None（中性）。实际布局：**列表首项最靠左（主操作
    在左）**，末项（取消侧）最靠右——与 Windows 对话框习惯一致。
    ``scroll=True`` 用可滚动正文承载长文本（如完整 traceback）；非 scroll 模式
    窗口高度按内容自适应（按钮行永不被裁）。
    返回所点按钮 key；标题栏 × / Esc 关闭返回 None。grab_set + wait_window
    模态阻塞（与 messagebox 语义一致）。
    """
    badge_text, badge_kind, glyph, glyph_fg = _DIALOG_KINDS.get(kind, _DIALOG_KINDS["info"])
    win = new_window(parent, title, "640x500" if scroll else "520x250")
    win.transient(parent)
    win.resizable(False, False)

    head = tk.Frame(win, bg=theme.BG)
    head.pack(fill="x", padx=20, pady=(18, 8))
    StatusBadge(head, text=badge_text, kind=badge_kind, dot=True, bg=theme.BG).pack(side="right")
    if glyph:
        tk.Label(head, text=glyph, bg=theme.BG, fg=glyph_fg,
                 font=(theme.FONT_MONO_NAME, 16, "bold")).pack(side="left", padx=(0, 8))
    tk.Label(head, text=title, bg=theme.BG, fg=theme.FG_WHITE,
             font=theme.FONT_TITLE).pack(side="left")

    card = SurfaceCard(win, bg=theme.SURFACE_CARD, parent_bg=theme.BG, pad_x=16, pad_y=12)
    card.pack(fill="both", expand=True, padx=20, pady=(0, 8))
    if scroll:
        sf = ScrollFrame(card.body, bg=theme.SURFACE_CARD)
        sf.pack(fill="both", expand=True)
        msg_parent, msg_w = sf.inner, 72
    else:
        msg_parent, msg_w = card.body, 52
    SelectableLabel(msg_parent, text=message, bg=theme.SURFACE_CARD, fg=theme.FG,
                    font=theme.FONT_UI, justify="left", width=msg_w).pack(fill="x")
    if detail:
        card.add_divider(pady=6)
        SelectableLabel(card.body, text=detail, bg=theme.SURFACE_CARD, fg=theme.MUTED,
                        font=theme.FONT_UI_SMALL, justify="left", width=52).pack(fill="x")

    btn_bar = tk.Frame(win, bg=theme.BG)
    btn_bar.pack(fill="x", padx=20, pady=(8, 16))
    result: list = [None]

    def _pick(key):
        result[0] = key
        win.destroy()

    cancel_btn = None
    for key, text, style in reversed(buttons):   # 末项先 pack → 最靠右（取消侧）
        if style == "danger":
            btn = tk.Button(btn_bar, text=text, command=lambda k=key: _pick(k),  # style-exempt: style.md §3 豁免：危险操作红色警示
                            bg=theme.DANGER, fg=theme.FG_WHITE, relief="flat",
                            activebackground=theme.DANGER_ACTIVE, activeforeground=theme.FG_WHITE,
                            padx=16, pady=5, font=theme.FONT_UI_BOLD, cursor=CLICK_CURSOR)
            btn.pack(side="right", padx=(8, 0))
        else:
            btn = flat_button(btn_bar, text, lambda k=key: _pick(k),
                              primary=(style == "accent"))
            btn.pack(side="right", padx=(8, 0))
        cancel_btn = cancel_btn or btn   # 聚焦取消侧（列表末项 = 最右）
    if cancel_btn is not None:
        cancel_btn.focus_set()
    # 高度自适应（创建时估算）：正文在 Canvas 窗口项内，reqheight 反馈不到
    # （卡面高度是 canvas 选项），只能按文本量直接估算窗高——按钮行 pack 在
    # 最后，被裁即不可点（Esc 会当「否」处理，违反知情确认）。
    import math
    import tkinter.font as _tkfont
    _f = _tkfont.Font(font=theme.FONT_UI)
    _avail_px = 420            # 卡片正文可用像素宽（520 - 页头/卡边距）
    _text_lines = sum(
        max(1, math.ceil(_f.measure(seg) / _avail_px))
        for seg in message.split("\n"))
    _need_h = 150 + _text_lines * 21 + (34 if detail else 0)
    _max_h = max(300, win.winfo_screenheight() - 90)
    if _need_h > 250:
        win.geometry(f"520x{min(_need_h, _max_h)}+{win.winfo_x()}+{win.winfo_y()}")
    win.bind("<Escape>", lambda _e: win.destroy(), add="+")
    win.grab_set()
    win.wait_window()
    return result[0]


def show_info(parent, title, message) -> None:
    """替代 messagebox.showinfo。"""
    message_dialog(parent, title, message, kind="info")


def show_warning(parent, title, message) -> None:
    """替代 messagebox.showwarning。"""
    message_dialog(parent, title, message, kind="warning")


def show_error(parent, title, message) -> None:
    """替代 messagebox.showerror。"""
    message_dialog(parent, title, message, kind="error")


def ask_yes_no(parent, title, message, *, confirm_text="确定", danger=False) -> bool:
    """替代 messagebox.askyesno；Esc / 关闭 = 否。danger=True 走红色警示主按钮。"""
    key = message_dialog(
        parent, title, message,
        kind="danger" if danger else "confirm",
        buttons=(("yes", confirm_text, "danger" if danger else "accent"),
                 ("no", "取消", None)),
    )
    return key == "yes"


def ask_yes_no_cancel(parent, title, message, *, yes_text="是", no_text="否") -> "str | None":
    """替代 messagebox.askyesnocancel；返回 "yes" / "no" / None（取消或关闭）。"""
    return message_dialog(
        parent, title, message, kind="confirm",
        buttons=(("yes", yes_text, "accent"), ("no", no_text, None), ("cancel", "取消", None)),
    )


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

        # 1. 顶部大气留白页头（与画布同底色，去除旧版厚重灰顶条与冗余关闭钮，靠标题栏 × 与 Esc 关闭）
        self.header = tk.Frame(self.win, bg=bg)
        self.header.pack(fill="x", padx=theme.PAD_L + 4, pady=(theme.PAD_L + 2, theme.PAD_M))

        self.header_extra = tk.Frame(self.header, bg=bg)
        self.header_extra.pack(side="right")

        title_box = tk.Frame(self.header, bg=bg)
        title_box.pack(side="left", fill="x", expand=True)

        self.title_lbl = tk.Label(title_box, text=title, bg=bg,
                                  fg=theme.FG_WHITE, font=theme.FONT_TITLE, anchor="w")
        self.title_lbl.pack(side="top", anchor="w")
        if subtitle:
            self.sub_lbl = tk.Label(title_box, text=subtitle, bg=bg,
                                    fg=theme.MUTED, font=theme.FONT_UI_SMALL, anchor="w")
            self.sub_lbl.pack(side="top", anchor="w", pady=(2, 0))
        else:
            self.sub_lbl = None

        tk.Frame(self.win, bg=theme.DIVIDER, height=1).pack(fill="x", padx=theme.PAD_L + 4)

        # 2. 底部 Action Bar (可选)
        if with_action_bar:
            self.action_bar = tk.Frame(self.win, bg=bg, height=44)
            self.action_bar.pack(side="bottom", fill="x")
            self.action_bar.pack_propagate(False)
            tk.Frame(self.action_bar, bg=theme.DIVIDER, height=1).pack(side="top", fill="x", padx=theme.PAD_L + 4)
            self.action_inner = tk.Frame(self.action_bar, bg=bg)
            self.action_inner.pack(fill="both", expand=True, padx=theme.PAD_L + 4, pady=theme.PAD_S)
        else:
            self.action_bar = None
            self.action_inner = None

        # 3. 中部 Content 容器
        self.content = tk.Frame(self.win, bg=bg, padx=theme.PAD_L + 4, pady=theme.PAD_L)
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
