"""Visual constants and ttk style setup for the TCER GUI.

One place for every color / font / grade mapping, so views stay free of magic
numbers. ``LEVEL_COLORS`` maps the semantic ``level`` tag carried by each metric
definition (in ``metric_defs``) to a hex color; ``GRADE_HEX`` colors CTEI bars
by rating. No business logic lives here.
"""
from __future__ import annotations

from .platform import FONT_CJK, FONT_MONO_NAME

# 间距节奏（px）：容器/组件 padding 统一从这四档取值，保持视觉一致。
PAD_XS = 2
PAD_S = 4
PAD_M = 8
PAD_L = 12

# --- 收编自散落 magic hex（style.md §11/§16 查漏补缺）---
CONTROL_BG = "#333333"      # 控件底：分段控件容器/pill、进度条槽、Treeview 表头
CARD_HEADER_BG = "#2a2a2e"  # 弹窗/卡片头部略抬升底（比 PANEL_2 更近 PANEL）
SEL_ROW_BG = "#15324f"      # 选中行底色（CheckRow 淡蓝；多行高亮不刺眼）
WARN_TINT_BG = "#3a2a1a"    # 警示提示条暗橙底（配 WARNING 前景字）
FG_WHITE = "#ffffff"        # 选中态纯白字（比 FG 更亮一档）

# 交互反馈色（hover 态），与 flat_button / Card 共用。
HOVER_BG = "#33333a"
HOVER_ACCENT = "#1a8ce0"
BORDER = "#3e3e42"
BORDER_HOVER = "#5a5a60"  # Card 等可点卡片 hover 边框提亮（灰、非蓝，选中才用 ACCENT）

# 滚动条极简细条：深灰滑块（只比凹槽亮一点）、hover 稍亮（灰、非蓝）、凹槽近背景隐形（见 setup_style）。
SCROLL_THUMB = "#3a3a3a"
SCROLL_THUMB_HOVER = "#555555"

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
VIEW_PROJECT = "#cc7a1e"   # 项目视角标识色（橙黄，与会话蓝 ACCENT 区分）

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

# Per-row "best" marker (模型对比) — gold highlights the best value in each row,
# where "best" follows the metric's 词性 (越大越好 → 取最大；越小越好 → 取最小).
# Metrics with no good/bad direction get no marker.
VALUE_BEST = "#e0b341"   # 金色 — 该行最优值

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
    "G1": "#3a3a3e",
    "G2": "#1e4a6f",
    "G3": "#1e5c5c",
    "G4": "#1e5c2b",
    "G5": "#6f4a1e",
    "G6": "#5a1e6f",
    "G_NEUTRAL": "#4a4a4e",
}

# Fonts (named so they can be tuned in one place).
FONT_UI = (FONT_CJK, 9)
FONT_UI_BOLD = (FONT_CJK, 9, "bold")
FONT_UI_SMALL = (FONT_CJK, 8)
FONT_UI_SMALL_BOLD = (FONT_CJK, 8, "bold")
FONT_HEADING = (FONT_CJK, 10, "bold")
FONT_VALUE = (FONT_MONO_NAME, 11, "bold")
FONT_MONO = (FONT_MONO_NAME, 9)


def setup_style(ttk) -> None:
    """Configure the ttk Style for the dark theme (call once after Style() creation)."""
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except ttk.TclError:
        pass
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=22,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("Treeview.Heading", background=CONTROL_BG, foreground=FG,
                    relief="flat", borderwidth=1)
    # clam draws a raised (white-ish) border on heading hover/press — keep it dark & flat.
    style.map("Treeview", background=[("selected", "#094771")])
    style.map("Treeview.Heading",
              background=[("active", "#3d3d3d"), ("pressed", "#2b2b2b")],
              foreground=[("active", FG)],
              relief=[("active", "flat"), ("pressed", "flat")])

    # 下拉框深色化 — 默认白底在深色主题里非常突兀（下拉列表颜色需另经
    # root.option_add 设置，见 app.__init__）。
    style.configure("TCombobox", fieldbackground=PANEL, background=PANEL_2,
                    foreground=FG, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=PANEL, darkcolor=PANEL, insertcolor=FG)
    style.map("TCombobox",
              fieldbackground=[("readonly", PANEL), ("active", PANEL)],
              foreground=[("readonly", FG)],
              selectbackground=[("readonly", PANEL)],
              selectforeground=[("readonly", FG)],
              # hover/active 态：clam 默认会把箭头与按钮底刷成白色，压回主题色。
              background=[("active", PANEL_2)],
              arrowcolor=[("active", FG)])

    # 滚动条极简细条（常驻）：clam 原生 trough/thumb + 自定义 layout 去箭头，只留滑块。
    # 配色走 style.configure（对 clam 原生元素有效），所有未显式指定 style 的
    # ttk.Scrollbar（ScrollFrame/Treeview/Listbox）统一继承。
    # 注：曾尝试用 image 元素重绘以把厚度控到 8px，但 image thumb 不响应 scrollbar 的
    # 几何控制、滑块不渲染（实机像素验证为 0），故回退原生 thumb —— 可靠可见，厚度 ~14px。
    style.layout("Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {
            "sticky": "ns",
            "children": [("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "ns"})],
        }),
    ])
    style.configure("Vertical.TScrollbar", gripcount=0,
                    background=SCROLL_THUMB, troughcolor=BG,
                    bordercolor=SCROLL_THUMB, lightcolor=SCROLL_THUMB,
                    darkcolor=SCROLL_THUMB, arrowcolor=BG, relief="flat")
    style.map("Vertical.TScrollbar",
              background=[("active", SCROLL_THUMB_HOVER),
                          ("pressed", SCROLL_THUMB_HOVER)])
    style.layout("Horizontal.TScrollbar", [
        ("Horizontal.Scrollbar.trough", {
            "sticky": "ew",
            "children": [("Horizontal.Scrollbar.thumb", {"expand": 1, "sticky": "ew"})],
        }),
    ])
    style.configure("Horizontal.TScrollbar", gripcount=0,
                    background=SCROLL_THUMB, troughcolor=BG,
                    bordercolor=SCROLL_THUMB, lightcolor=SCROLL_THUMB,
                    darkcolor=SCROLL_THUMB, arrowcolor=BG, relief="flat")
    style.map("Horizontal.TScrollbar",
              background=[("active", SCROLL_THUMB_HOVER),
                          ("pressed", SCROLL_THUMB_HOVER)])

    # Notebook (tab) styling — dark theme matching left panel.
    # 客户区边框色显式压成 BG：clam 默认 bordercolor/lightcolor 是浅色，会沿标签页
    # 内容画一圈白框（borderwidth=0 压不住元素自带的 1px 边），故三色同 BG 隐形。
    style.configure("TNotebook", background=BG, borderwidth=0,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG)
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                    padding=[14, 4], font=(FONT_CJK, 9),
                    bordercolor=BG, lightcolor=BG, darkcolor=BG,
                    focusthickness=0)  # 去掉点击页签后文字四周的虚线聚焦框
    style.map("TNotebook.Tab",
              background=[("selected", BORDER), ("active", CONTROL_BG)],
              foreground=[("selected", FG), ("active", FG)],
              padding=[("selected", [16, 6])],
              # 压住 clam 默认浅色 bevel：selected 态 lightcolor 默认 #eeebe7 会给选中
              # 页签画一圈白描边，且 map 优先级高于 configure，故三色各态都得在 map 里设 BG。
              lightcolor=[("selected", BG), ("active", BG), ("!disabled", BG)],
              darkcolor=[("selected", BG), ("active", BG), ("!disabled", BG)],
              bordercolor=[("selected", BG), ("active", BG), ("!disabled", BG)])
    # 彻底去掉点击页签后的黑色虚线聚焦框：clam 的 Notebook.focus 元素即使
    # focusthickness=0 仍会描 1px 虚线，故直接把它从 layout 移除（label 保留，图标+文字照常）。
    style.layout("TNotebook.Tab", [
        ("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})
            ]})
        ]})
    ])

# 时段热力图（GitHub 日历风）配色。不用 GitHub 绿系：其空格 #161b22 比 PANEL 更黑
# 像「洞」，亮绿与 SUCCESS 撞语义（成本高≠好）。空格=略抬升灰；数据四档=ACCENT 蓝
# 同色相 暗→亮（含 HOVER_ACCENT），蓝=强度中性，只表「多少」不表「好坏」。
HEATMAP_EMPTY = PANEL_2
HEATMAP_RAMP = ("#153a57", "#0f5d94", HOVER_ACCENT, "#6dbdf2")
# 坏方向指标（sentiment="down"，如返工率）用橙阶：值越高越「差」，蓝的强度
# 中性语义不适用，橙与 WARNING 同语义族但独立分档，暗→亮对应 低→高（差）。
HEATMAP_RAMP_BAD = ("#4a2e15", "#8a5220", "#c97a2a", "#f2a75c")
