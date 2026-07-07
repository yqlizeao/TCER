"""Reusable Tk widgets for the TCER GUI: Tooltip, ScrollFrame, Card, MetricCell.

Dumb, data-free components — they render what they're given and emit callbacks.
Selection state and data live in ``app`` / ``views``. Importing this module
imports tkinter (only happens when the GUI actually launches).
"""
from __future__ import annotations

import tkinter as tk

from . import theme
from .metric_defs import Metric


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
        self.canvas = tk.Canvas(parent, bg=bg, highlightthickness=0)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._reset_pending = False
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_resize)
        self._unbind_wheel = None
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.pack(side="left", fill="both", expand=True)

    def _on_resize(self, event) -> None:
        self.canvas.itemconfig(self._win, width=event.width)

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self._reset_pending:
            self.canvas.yview_moveto(0)

    def _on_enter(self, _event=None) -> None:
        from .platform import bind_mousewheel
        self._unbind_wheel = bind_mousewheel(
            self.canvas, lambda units: self.canvas.yview_scroll(units, "units"))

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
                              highlightthickness=1, highlightbackground="#3e3e42")
        self.frame.pack(fill="x", padx=padx, pady=pady)
        self._on_click = on_click
        self._on_right_click = on_right_click
        self.frame.bind("<Button-1>", lambda e: on_click(self))
        if on_right_click:
            self.frame.bind("<Button-3>", on_right_click)

    def bind_to(self, widget) -> None:
        widget.bind("<Button-1>", lambda e: self._on_click(self))
        if self._on_right_click:
            widget.bind("<Button-3>", self._on_right_click)
        return widget

    def set_selected(self, selected: bool) -> None:
        self.frame.configure(highlightbackground=theme.ACCENT if selected else "#3e3e42",
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
