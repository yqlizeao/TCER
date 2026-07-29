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
        self.tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self.tip, text=self.text, justify="left", bg="#fff8e1",
                       fg="#222222", relief="solid", borderwidth=1,
                       wraplength=460, font=theme.FONT_UI, padx=8, pady=5)
        lbl.pack()

    def _hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


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

    def _on_resize(self, event) -> None:
        self.canvas.itemconfig(self._win, width=event.width)

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
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
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if reset:
            self.canvas.yview_moveto(0)
            self.canvas.after_idle(self._finish_reset)

    def _finish_reset(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
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
            self.frame.configure(highlightbackground="#5a5a60")

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
                         selectforeground="#ffffff",
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
        menu = tk.Menu(self, tearoff=0, bg=theme.PANEL, fg=theme.FG,
                       activebackground=theme.HOVER_BG, activeforeground=theme.FG,
                       borderwidth=0)
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
    fg = "#ffffff" if primary else theme.FG
    btn = tk.Button(parent, text=text, command=command, relief="flat",
                    bg=base_bg, fg=fg, bd=0, cursor="hand2",
                    activebackground=hover_bg, activeforeground=fg,
                    padx=theme.PAD_M if padx is None else padx,
                    pady=theme.PAD_XS, font=theme.FONT_UI, **kw)
    btn.bind("<Enter>", lambda _e: btn.config(bg=hover_bg), add="+")
    btn.bind("<Leave>", lambda _e: btn.config(bg=base_bg), add="+")
    return btn


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
    return win
