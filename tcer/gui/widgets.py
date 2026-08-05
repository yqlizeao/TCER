"""Reusable Tk widgets for the TCER GUI: Tooltip, ScrollFrame, Card, MetricCell.

Dumb, data-free components — they render what they're given and emit callbacks.
Selection state and data live in ``app`` / ``views``. Importing this module
imports tkinter (only happens when the GUI actually launches).
"""
from __future__ import annotations

import tkinter as tk

from . import theme
from .metric_defs import Metric, UNSUPPORTED_LABEL


class Tooltip:
    """Lightweight hover tooltip for any widget (stdlib only)."""

    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg=theme.BORDER)  # 外层露 1px 作边框（深色主题）
        self.tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self.tip, text=self.text, justify="left",
                       bg=theme.PANEL_2, fg=theme.FG,
                       wraplength=460, font=theme.FONT_UI, padx=8, pady=5)
        lbl.pack(padx=1, pady=1)  # 1px 边框 = Toplevel(bg=BORDER) 透出

    def _hide(self, _event=None) -> None:
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
        self._row = tk.Frame(parent, bg=theme.PANEL, cursor="hand2")
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

    def _on_leave(self, _e=None) -> None:
        self._apply()

    def _draw(self) -> None:
        """外部改 var 后刷新（兼容旧接口名，等价 _apply）。"""
        self._apply()


class CollapsibleSection:
    """可折叠区：彩色标题(header,可点击)+ 内容容器(content frame)。

    调用方把实际控件 pack 进 ``content``；点标题 toggle content 显隐。
    用于把排名页/得分构成等「▼ 装饰标题」统一赋予折叠能力（与指标分类、
    模型对比的分组折叠一致）。``expand`` 控制 content 是否占满剩余空间。
    """

    def __init__(self, parent, title, color, *, expand: bool = True) -> None:
        self._title = title
        self._expand = expand
        self._collapsed = False
        self.header = tk.Frame(parent, bg=color, padx=6, pady=3)
        self.header.pack(fill="x", pady=(1, 0))
        self._arrow = tk.Label(self.header, text=f"▼ {title}", bg=color, fg=theme.FG,
                               font=theme.FONT_UI_SMALL_BOLD, anchor="w", cursor="hand2")
        self._arrow.pack(side="left")
        self.content = tk.Frame(parent, bg=theme.BG)
        self.content.pack(fill="both", expand=expand)
        for w in (self.header, self._arrow):
            w.bind("<Button-1>", lambda e: self.toggle(), add="+")

    def set_title(self, title: str) -> None:
        """更新标题文字（保留当前折叠状态）。用于随视角切换重命名区块。"""
        self._title = title
        self._arrow.config(text=f"{'▶' if self._collapsed else '▼'} {title}")

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._arrow.config(text=f"{'▶' if self._collapsed else '▼'} {self._title}")
        if self._collapsed:
            self.content.pack_forget()
        else:
            self.content.pack(fill="both", expand=self._expand)


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
    """A selectable list card. Selection highlight via ``set_selected``.

    Build content into ``self.frame``; register any child widget that should
    also trigger selection via ``bind_to``.
    """

    def __init__(self, parent, on_click, on_right_click=None,
                 bg: str = theme.PANEL_2, padx: int = 2, pady: int = 2) -> None:
        self.frame = tk.Frame(parent, bg=bg, relief="flat", borderwidth=1,
                              highlightthickness=1, highlightbackground=theme.BORDER,
                              cursor="hand2")
        self.frame.pack(fill="x", padx=padx, pady=pady)
        self._on_click = on_click
        self._on_right_click = on_right_click
        self._selected = False
        self.frame.bind("<Button-1>", lambda e: on_click(self))
        # hover 反馈：未选中时边框提亮，可点击感（Enter/Leave 覆盖整卡含子组件）。
        self.frame.bind("<Enter>", self._on_hover, add="+")
        self.frame.bind("<Leave>", self._on_unhover, add="+")
        if on_right_click:
            self.frame.bind("<Button-3>", on_right_click)

    def _on_hover(self, _e=None) -> None:
        if not self._selected:
            self.frame.configure(highlightbackground=theme.BORDER_HOVER)

    def _on_unhover(self, _e=None) -> None:
        if not self._selected:
            self.frame.configure(highlightbackground=theme.BORDER)

    def bind_to(self, widget) -> None:
        widget.bind("<Button-1>", lambda e: self._on_click(self))
        if self._on_right_click:
            widget.bind("<Button-3>", self._on_right_click)
        return widget

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.frame.configure(highlightbackground=theme.ACCENT if selected else theme.BORDER,
                             highlightthickness=2 if selected else 1)


class MetricCell:
    """One metric tile: colored title + value (StringVar) + unit + tooltip.

    Holds ``self.var`` so the panel can update the value without rebuilding.
    Value color reflects sentiment: green=good direction, red=bad, gray=neutral.
    """

    def __init__(self, parent, metric: Metric, on_click=None) -> None:
        self.metric = metric
        self.frame = tk.Frame(parent, bg=theme.PANEL, padx=4, pady=0)
        color = theme.LEVEL_COLORS.get(metric.level, theme.LEVEL_BASIC)

        # Title with unit inlined: "TCER（行/百万）" or just "缓存命中率"
        title_text = f"{metric.name}（{metric.unit}）" if metric.unit else metric.name
        self.title = tk.Label(self.frame, text=title_text, bg=theme.PANEL, fg=color,
                              font=theme.FONT_UI_SMALL, anchor="w")
        self.title.pack(anchor="w")

        self.var = tk.StringVar(value="-")
        value_fg = theme.VALUE_NEUTRAL
        self.value = tk.Label(self.frame, textvariable=self.var, bg=theme.PANEL,
                              fg=value_fg, font=theme.FONT_VALUE, anchor="w")
        self.value.pack(anchor="w")

        if on_click:
            self.value.config(cursor="hand2")
            self.title.config(cursor="hand2")
            self.value.bind("<Button-1>", lambda e: on_click())
            self.title.bind("<Button-1>", lambda e: on_click())

        tip = f"{metric.name}\n{metric.tip}"
        for w in (self.frame, self.title, self.value):
            Tooltip(w, tip)

    def set_value(self, text: str) -> None:
        """Update displayed value and apply sentiment-based coloring."""
        self.var.set(text)
        if text == UNSUPPORTED_LABEL:
            # 数据源不提供该字段 — 弱化显示，与「无数据 -」区分。
            self.value.config(fg=theme.MUTED)
            return
        sentiment = self.metric.sentiment
        if not sentiment or text in ("-", "0", "0.0", "0.00", "0.000"):
            fg = theme.VALUE_NEUTRAL
        else:
            # Try to parse numeric value for directional coloring
            try:
                num = float(text.replace(",", "").replace("%", "").replace("$", ""))
                if sentiment == "up":
                    fg = theme.VALUE_GOOD if num > 0 else theme.VALUE_BAD
                elif sentiment == "down":
                    fg = theme.VALUE_BAD if num > 0 else theme.VALUE_GOOD
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
                 font=None, justify="left", width=60, padx=0, pady=0, **kw):
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

    def _auto_height(self) -> None:
        """按 wrap 后的实际视觉行数撑高，displaylines+1 余量避免末行被裁。"""
        self.update_idletasks()
        n = self.count("1.0", "end-1c", "displaylines")
        lines = n[0] if n and n[0] else 1
        self.configure(height=max(1, lines + 1))

    def _copy_menu(self, event=None) -> None:
        menu = FlatMenu(self)
        menu.add_command(label="复制", command=self._copy_all)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_all(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.get("1.0", "end-1c"))


def flat_button(parent, text, command=None, *, primary=False, padx=None, **kw):
    """统一扁平按钮：一致的配色/内边距/hover 反馈（按钮效果一致性的单一来源）。

    ``primary=True`` 用主题强调色（主操作），否则面板灰（普通操作）。
    """
    base_bg = theme.ACCENT if primary else theme.PANEL
    hover_bg = theme.HOVER_ACCENT if primary else theme.HOVER_BG
    fg = theme.FG_WHITE if primary else theme.FG
    btn = tk.Button(parent, text=text, command=command, relief="flat",  # style-exempt: flat_button 本体
                    bg=base_bg, fg=fg, bd=0, cursor="hand2",
                    activebackground=hover_bg, activeforeground=fg,
                    padx=theme.PAD_M if padx is None else padx,
                    pady=theme.PAD_XS, font=theme.FONT_UI, **kw)
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

    def __init__(self, parent):
        self._closed = False
        self._top = tk.Toplevel(parent)
        self._top.overrideredirect(True)
        self._top.configure(bg=theme.BORDER)               # 1px 外框色
        self._body = tk.Frame(self._top, bg=theme.PANEL)
        self._body.pack(fill="both", expand=True, padx=1, pady=1)  # 1px 露出外框
        self._top.withdraw()

    def add_command(self, label="", command=None, image=None, compound=None,
                    state="normal", **_kw):
        disabled = (state == "disabled")
        row = tk.Frame(self._body, bg=theme.PANEL)
        row.pack(fill="x")
        fg = theme.MUTED if disabled else theme.FG
        lbl = tk.Label(row, text=label, image=image, compound="left",
                       bg=theme.PANEL, fg=fg, font=theme.FONT_UI,
                       padx=14, pady=4, anchor="w")
        lbl.pack(fill="x")
        if not disabled:
            def enter(_e):
                row.configure(bg=theme.ACCENT)
                lbl.configure(bg=theme.ACCENT, fg=theme.FG_WHITE)
            def leave(_e):
                row.configure(bg=theme.PANEL)
                lbl.configure(bg=theme.PANEL, fg=theme.FG)
            def click(_e):
                self._close()
                if command is not None:
                    command()
            for w in (row, lbl):
                w.configure(cursor="hand2")
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)
                w.bind("<Button-1>", click)
        return row

    def add_radiobutton(self, label="", variable=None, value=None, command=None, **_kw):
        selected = variable is not None and variable.get() == value
        prefix = "●  " if selected else "    "
        row = tk.Frame(self._body, bg=theme.PANEL)
        row.pack(fill="x")
        lbl = tk.Label(row, text=prefix + label, bg=theme.PANEL, fg=theme.FG,
                       font=theme.FONT_UI, padx=14, pady=4, anchor="w")
        lbl.pack(fill="x")

        def enter(_e):
            row.configure(bg=theme.ACCENT)
            lbl.configure(bg=theme.ACCENT, fg=theme.FG_WHITE)

        def leave(_e):
            row.configure(bg=theme.PANEL)
            lbl.configure(bg=theme.PANEL, fg=theme.FG)

        def click(_e):
            if variable is not None:
                variable.set(value)
            self._close()
            if command is not None:
                command()

        for w in (row, lbl):
            w.configure(cursor="hand2")
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
            w.bind("<Button-1>", click)
        return row

    def add_separator(self):
        tk.Frame(self._body, bg=theme.BORDER, height=1).pack(fill="x", padx=2, pady=2)

    def tk_popup(self, x, y, *_args):
        self._top.deiconify()
        self._top.update_idletasks()
        w, h = self._top.winfo_reqwidth(), self._top.winfo_reqheight()
        sw, sh = self._top.winfo_screenwidth(), self._top.winfo_screenheight()
        if x + w > sw:
            x = max(0, sw - w)
        if y + h > sh:
            y = max(0, sh - h)
        self._top.geometry(f"+{x}+{y}")
        self._top.grab_set_global()
        self._top.bind("<Button-1>", self._on_top_click, add="+")
        self._top.bind("<Escape>", lambda _e: self._close())
        self._top.focus_set()

    def _on_top_click(self, e):
        if self._closed:
            return
        # 命中项时由项的 Button-1 先处理（并 close）；落在菜单外才由这里关。
        x0, y0 = self._top.winfo_rootx(), self._top.winfo_rooty()
        x1, y1 = x0 + self._top.winfo_width(), y0 + self._top.winfo_height()
        if not (x0 <= e.x_root <= x1 and y0 <= e.y_root <= y1):
            self._close()

    def _close(self):
        if self._closed:
            return
        self._closed = True
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
                        font=theme.FONT_UI, cursor="hand2")
        prev.pack(side="left", padx=2, pady=3)
        prev.bind("<Button-1>", lambda _e: self._shift(-1))
        self._title = tk.Label(head, text="", bg=theme.PANEL, fg=theme.FG,
                               font=theme.FONT_UI_BOLD, width=9)
        self._title.pack(side="left", expand=True, fill="x", pady=3)
        nxt = tk.Label(head, text=" ▶ ", bg=theme.PANEL, fg=theme.MUTED,
                       font=theme.FONT_UI, cursor="hand2")
        nxt.pack(side="left", padx=2, pady=3)
        nxt.bind("<Button-1>", lambda _e: self._shift(1))
        clr = tk.Label(head, text=" ✕ ", bg=theme.PANEL, fg=theme.MUTED,
                       font=theme.FONT_UI, cursor="hand2")
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
                            cursor="hand2")
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


def new_window(parent, title, size, bg=theme.BG):
    """Create a centered Toplevel relative to *parent* (shared popup shell)."""
    import tkinter as tk

    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=bg)
    parent.update_idletasks()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    wpx, hpx = (int(x) for x in size.split("x"))
    x = px + (pw - wpx) // 2
    y = py + (ph - hpx) // 2
    win.geometry(f"{wpx}x{hpx}+{x}+{y}")
    from .platform import apply_dark_titlebar
    apply_dark_titlebar(win)   # 创建即设
    # 部分子窗口首次显示时尚未完成映射，DWM 属性可能没生效；<Map> 时再设一次兜底，
    # 确保每个子窗口实际显示时标题栏与主窗口一致。
    win.bind("<Map>", lambda e: apply_dark_titlebar(win), add="+")
    return win
