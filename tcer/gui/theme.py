"""Visual constants and ttk style setup for the TCER GUI.

One place for every color / font / grade mapping, so views stay free of magic
numbers. ``LEVEL_COLORS`` maps the semantic ``level`` tag carried by each metric
definition (in ``metric_defs``) to a hex color; ``GRADE_HEX`` colors CTEI bars
by rating. No business logic lives here.
"""
from __future__ import annotations

# Base palette (dark, VS Code-ish).
BG = "#1e1e1e"
FG = "#e0e0e0"
PANEL = "#252526"
PANEL_2 = "#2d2d30"          # slightly raised surface for cards
MUTED = "#9e9e9e"
ACCENT = "#007acc"
SUCCESS = "#4ec9b0"
WARNING = "#ce9178"
ERROR = "#f48771"

# Metric semantic levels → display color.
# basic (white): absolute baseline values and direct calculations.
# compound (yellow): contains magic numbers / coefficients, reference only.
LEVEL_BASIC = "#e0e0e0"
LEVEL_COMPOUND = "#f39c12"
LEVEL_COLORS = {
    "basic": LEVEL_BASIC,
    "compound": LEVEL_COMPOUND,
}

# Value sentiment colors (applied to the metric VALUE, not the name).
VALUE_GOOD = "#4ec9b0"   # green  — good direction
VALUE_BAD = "#f48771"    # red    — bad direction
VALUE_NEUTRAL = "#e0e0e0"  # default gray

# CTEI grade → bar/cell fill color (used by the Canvas CTEI chart).
GRADE_HEX = {
    "优秀": "#2e7d32",
    "良好": "#0277bd",
    "中等": "#f9a825",
    "低效": "#d84315",
    "极端低效": "#b71c1c",
}

# Six-group framework — header background per group.
GROUP_COLORS = {
    "G1": "#2a2a2e",
    "G2": "#1e4a6f",
    "G3": "#1e5c5c",
    "G4": "#1e5c2b",
    "G5": "#6f4a1e",
    "G6": "#5a1e6f",
}

# Fonts (named so they can be tuned in one place).
FONT_UI = ("Microsoft YaHei", 9)
FONT_UI_BOLD = ("Microsoft YaHei", 9, "bold")
FONT_UI_SMALL = ("Microsoft YaHei", 8)
FONT_UI_SMALL_BOLD = ("Microsoft YaHei", 8, "bold")
FONT_HEADING = ("Microsoft YaHei", 10, "bold")
FONT_VALUE = ("Consolas", 11, "bold")
FONT_MONO = ("Consolas", 9)


def setup_style(ttk) -> None:
    """Configure the ttk Style for the dark theme (call once after Style() creation)."""
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except ttk.TclError:
        pass
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=22)
    style.configure("Treeview.Heading", background="#333333", foreground=FG,
                    relief="flat", borderwidth=1)
    # clam draws a raised (white-ish) border on heading hover/press — keep it dark & flat.
    style.map("Treeview", background=[("selected", "#094771")])
    style.map("Treeview.Heading",
              background=[("active", "#3d3d3d"), ("pressed", "#2b2b2b")],
              foreground=[("active", FG)],
              relief=[("active", "flat"), ("pressed", "flat")])
