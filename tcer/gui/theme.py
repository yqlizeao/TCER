"""Visual constants and ttk style setup for the TCER GUI.

One place for every color / font / grade mapping, so views stay free of magic
numbers. ``LEVEL_COLORS`` maps the semantic ``level`` tag carried by each metric
definition (in ``metric_defs``) to a hex color; ``GRADE_HEX`` colors 综合效率分
bars by tier. No business logic lives here.
"""
from __future__ import annotations

from .platform import FONT_CJK, FONT_MONO_NAME

# 间距节奏（px）：容器/组件 padding 统一从这四档取值，保持视觉一致。
PAD_XS = 2
PAD_S = 4
PAD_M = 8
PAD_L = 12

# --- Monokai Dimmed 工作台表面明度标尺 (Surface Elevation) ---
# 纪律：95% 面积收敛为 BG → PANEL → PANEL_2 → CONTROL 四级灰；强调色只给焦点/选中/状态。
BG = "#1e1e1e"              # 主工作区/编辑器底色（活动栏同为全窗最暗列）
PANEL = "#272727"           # 侧边栏资源管理器底色
ACTIVITY_BG = "#353535"     # 左侧 44px 活动栏底色（Monokai Dimmed 官方 activityBar.background——亮柱是该主题特征，勿改成暗列）
ACTIVITY_HOVER_BG = "#5a5a5a"  # 亮柱上的 hover（官方 hover=40% 白混合的保守近似；全局 HOVER_BG 在亮柱上不可辨）
PANEL_2 = "#303030"         # 卡片底色、面板分割底色（含状态栏——chrome 与内容同阶）
STATUS_BG = "#303030"       # 底部状态栏底色（与卡片 PANEL_2 同阶，明度跳变即分隔）
SECTION_HEADER_BG = "#272727"  # 侧栏分组头底色（对齐 VS Code 官方真机渲染：侧栏保持一体化平铺深色，无突兀亮条）
CONTROL_BG = "#3c3c3c"      # 输入框底色、下拉组件槽底色、分段控件底（官方 input 默认值 #3C3C3C——暗槽配亮组头是官方同构）
CARD_HEADER_BG = "#353535"  # 卡片/区块头部略抬升底
BORDER = "#3e3e42"          # 极细微结构分隔线 (sideBarSectionHeader.border)——只做结构分隔，永不表达状态
BORDER_HOVER = "#505050"    # 弹窗图形选择框描边（Card 体系已退出边框状态表达，勿再新增用途）
HOVER_BG = "#444444"        # 列表/按钮悬停底色（全局唯一 hover token）
HOVER_ACCENT = "#3b82f6"    # 活力焦点蓝 hover
SEL_ROW_BG = "#1e293b"      # 非聚焦选中底色
SEL_ROW_ACTIVE = "#1e3a8a"  # 聚焦高亮底色 (Cobalt 宝石蓝，与卡片/高能主题浑然一体)
ACCENT = "#2563eb"          # 充满精气神的现代高能焦点蓝

# rail（左侧竖条）宽度 SSOT：3px=内容卡选中/数据状态轨；2px=导航指示条（活动栏/页签）
RAIL_W = 3
RAIL_W_NAV = 2

WARN_TINT_BG = "#3a2a1a"    # 警示提示条暗橙底 (配 WARNING 前景字)
ERROR_TINT_BG = "#3a1a1a"   # 错误/吸引子警示卡片暗红底 (配 ERROR 前景字)

# 滚动条极简细条
SCROLL_THUMB = "#3a3a3c"
SCROLL_THUMB_HOVER = "#5c5c60"

# 效率榜项目视角三轴离散度须线 (min–max 区间条)
AXIS_SPREAD = "#4a5a6a"

# 前景色与排版标尺 (Typography & Text)
FG = "#e6edf3"              # 高对比度纯净白（GitHub Dark / Modern IDE 标杆文字色）
FG_WHITE = "#ffffff"        # 纯白强调文本
MUTED = "#9ca3af"           # 清晰次级灰度文字与刻度（告别低对比度泥泞暗灰）

# 语义色系（高清晰、高饱和、精气神充盈）
SUCCESS = "#22c55e"         # 活力翡翠绿
WARNING = "#f59e0b"         # 明亮温暖金橙
ERROR = "#ef4444"           # 清晰活力珊瑚红
CYAN = "#06b6d4"            # 高亮电光青蓝
CODE_MINT = "#4ec9b0"       # VS Code 官方标准代码语法青色
PURPLE = "#a855f7"          # 典雅紫罗兰
VIEW_PROJECT = "#f59e0b"    # 温暖金橙项目视角标识色

# 相空间动力学相图色 (平庸吸引子漏斗暗红光晕 + 狄拉克发光核底 + 流场与势阱边界)
ATTRACTOR_RINGS = ("#281818", "#341d1d", "#442222")
DIRAC_CORE_BG = "#1c2a1c"
ATTRACTOR_BASIN_BORDER = "#3a2020"   # 平庸吸引盆外层等势圈
DIRAC_WELL_BORDER = "#1c3b28"        # 狄拉克势阱保护圈
PHASE_STREAMLINE = "#272a2e"         # 相速度背景流线微矢量
PHASE_CORRIDOR = "#1c3828"           # 理想收敛流形走廊参考线
PHASE_GRID_DIRAC = "#27382c"         # 目标收敛区参考虚线
PHASE_GRID_TRAP = "#3d2a20"          # 吸引子危险区参考虚线
PHASE_ZONE_DIRAC = "#86B42B"         # 收敛目标域提示文字绿
PHASE_ZONE_TRAP = "#D08442"          # 高熵危险域提示文字橙
PHASE_START_HALO = "#D0B344"         # 相空间起点光晕金
PHASE_IMPULSE = "#56ADBC"            # 向心推力做功脉冲青
PHASE_CONTOUR_LOW = "#16221d"        # 深渊极低势
PHASE_CONTOUR_MID = "#1f262e"        # 中间平原
PHASE_CONTOUR_HIGH = "#32251e"       # 高耸势垒
PHASE_BARRIER_CREST = "#D0B344"      # 鞍点势垒脊线色 (金色微细虚线)
DEBT_GLOW_SAFE = "#86B42B"           # 低负债向心绿
DEBT_GLOW_DANGER = "#ef4444"         # 盲目动刀橙红
REGIME_GAS = "#569cd6"               # 气态探查蓝
REGIME_LIQUID = "#86B42B"            # 液态构建绿
REGIME_GLASS = "#D08442"             # 玻璃态死锁褐橙
REGIME_CRYSTAL = "#D0B344"           # 晶态收敛金白
WATERBED_ARC = "#c586c0"             # 控制论水床扰动弧
PHASE_HUD_BG = "#1e1e1e"             # 质点探针 HUD 驻留卡片底色

# basic (white): absolute baseline values and direct calculations.
# compound (yellow): contains magic numbers / coefficients, reference only.
LEVEL_BASIC = "#e6edf3"
LEVEL_COMPOUND = "#eab308"
LEVEL_COLORS = {
    "basic": LEVEL_BASIC,
    "compound": LEVEL_COMPOUND,
}

# Value sentiment colors (applied to the metric VALUE, not the name).
VALUE_GOOD = "#22c55e"      # 活力绿 — 正向指标
VALUE_BAD = "#ef4444"       # 活力红 — 负向/异常指标
VALUE_NEUTRAL = FG          # 数值默认 = 正文色（层级靠字重/等宽家族，不靠变色）

# Per-row "best" marker (模型对比) — gold highlights the best value in each row
VALUE_BEST = "#eab308"      # 璀璨金 — 该行最优值

# 综合效率分 tier → bar/cell fill color (used by the ranking bar + trend dots).
# 「良好」「低效」两档为暗底可读性提亮（原 #3655b5/#C7444A 对 PANEL_2 对比度 <2:1，
# 8-9pt 小字看不清——档位色文字/色点同源此表，只动档位映射不动 ERROR/ACCENT 本体）。
GRADE_HEX = {
    "优秀": "#22c55e",
    "良好": "#3b82f6",
    "中等": "#eab308",
    "待改进": "#f97316",
    "低效": "#ef4444",
}

# Six-group framework — header background per group.
GROUP_COLORS = {
    "G1": "#263238",        # 会话概况: 高级深蓝灰/Slate
    "G2": "#1a4f8b",        # Token 用量: 宝石深蓝/Sapphire (充盈算力感)
    "G3": "#0e7490",        # 缓存效率: 活力青翠/Cyan-Teal (高通量轻快感)
    "G4": "#15803d",        # 代码产出与质量: 充盈翠绿/Emerald (代码创作生机)
    "G5": "#a15209",        # 成本分析: 温暖金褐/Amber-Bronze (商业与价值感)
    "G6": "#7e22ce",        # 综合评分: 典雅皇家紫/Royal Purple (高阶智能)
    "G_NEUTRAL": "#263238",
}

# Fonts (named so they can be tuned in one place).
FONT_TITLE = (FONT_CJK, 14, "bold")
FONT_HEADING = (FONT_CJK, 11, "bold")
FONT_UI = (FONT_CJK, 10)
FONT_UI_BOLD = (FONT_CJK, 10, "bold")
FONT_UI_SMALL = (FONT_CJK, 9)
FONT_UI_SMALL_BOLD = (FONT_CJK, 9, "bold")
FONT_VALUE = (FONT_MONO_NAME, 10, "bold")   # 数值沉稳有力（等宽粗体，扎实锚点）
FONT_MONO = (FONT_MONO_NAME, 10)
FONT_STAT = (FONT_CJK, 9)                   # 状态栏字号清晰可辨
def setup_style(ttk) -> None:
    """Configure the ttk Style for the dark theme (call once after Style() creation)."""
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except ttk.TclError:
        pass
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=24,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure("Treeview.Heading", background=PANEL_2, foreground=FG,
                    relief="flat", borderwidth=1)
    # clam draws a raised (white-ish) border on heading hover/press — keep it dark & flat.
    style.map("Treeview", background=[("selected", SEL_ROW_ACTIVE)])
    style.map("Treeview.Heading",
              background=[("active", HOVER_BG), ("pressed", CONTROL_BG)],
              foreground=[("active", FG_WHITE)],
              relief=[("active", "flat"), ("pressed", "flat")])

    # 下拉框深色化
    style.configure("TCombobox", fieldbackground=CONTROL_BG, background=PANEL_2,
                    foreground=FG, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=PANEL_2, darkcolor=PANEL_2, insertcolor=FG)
    style.map("TCombobox",
              fieldbackground=[("readonly", CONTROL_BG), ("active", CONTROL_BG)],
              foreground=[("readonly", FG)],
              selectbackground=[("readonly", CONTROL_BG)],
              selectforeground=[("readonly", FG_WHITE)],
              background=[("active", HOVER_BG)],
              arrowcolor=[("active", FG_WHITE)])

    # 滚动条极简细条（常驻）：clam 原生 trough/thumb + 自定义 layout 去箭头，只留滑块。
    style.layout("Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {
            "sticky": "ns",
            "children": [("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "ns"})],
        }),
    ])
    style.configure("Vertical.TScrollbar", gripcount=0, arrowsize=9,
                    background=SCROLL_THUMB, troughcolor=PANEL,
                    bordercolor=PANEL, lightcolor=PANEL,
                    darkcolor=PANEL, arrowcolor=PANEL, relief="flat")
    style.map("Vertical.TScrollbar",
              background=[("active", SCROLL_THUMB_HOVER),
                          ("pressed", SCROLL_THUMB_HOVER)])
    style.layout("Horizontal.TScrollbar", [
        ("Horizontal.Scrollbar.trough", {
            "sticky": "ew",
            "children": [("Horizontal.Scrollbar.thumb", {"expand": 1, "sticky": "ew"})],
        }),
    ])
    style.configure("Horizontal.TScrollbar", gripcount=0, arrowsize=9,
                    background=SCROLL_THUMB, troughcolor=PANEL,
                    bordercolor=PANEL, lightcolor=PANEL,
                    darkcolor=PANEL, arrowcolor=PANEL, relief="flat")
    style.map("Horizontal.TScrollbar",
              background=[("active", SCROLL_THUMB_HOVER),
                          ("pressed", SCROLL_THUMB_HOVER)])

    # （Notebook 已退役：页签栏移入侧栏 SidebarNav，ttk.Notebook 样式随之删除。）

# 时段热力图（GitHub 日历风）配色。不用 GitHub 绿系：其空格 #161b22 比 PANEL 更黑
# 像「洞」，亮绿与 SUCCESS 撞语义（成本高≠好）。空格=略抬升灰；数据四档=ACCENT 蓝
HEATMAP_EMPTY = PANEL_2
HEATMAP_RAMP = ("#1b2d56", "#2a4387", ACCENT, "#6784d6")
# 坏方向指标（sentiment="down"，如返工率）用橙阶：值越高越「差」
HEATMAP_RAMP_BAD = ("#4a2e15", "#8a5220", WARNING, "#f2a75c")

# --- 语义套色 (收编自弹窗/图表散落 hex，VS Code 语法高亮系) ---
TOKEN_COLORS = {
    "input": "#569cd6",        # 蓝 — 输入
    "cache_write": WARNING,    # 橙 — 缓存写 (创建)
    "cache_read": SUCCESS,     # 青绿 — 缓存读 (命中)
    "output": LEVEL_COMPOUND,  # 黄 — 输出
}
# 图表分类 palette（多列/多模型循环取色），与 TOKEN_COLORS 同族色。
CHART_PALETTE = (ACCENT, SUCCESS, LEVEL_COMPOUND, WARNING, "#c586c0", "#56ADBC")
SECTION_ACCENT = "#56ADBC"    # 弹窗内小节标题淡蓝/青
BASELINE_ACCENT = LEVEL_COMPOUND   # 基准值黄（BaselinesPopup）
MEDAL_COLORS = (LEVEL_COMPOUND, "#a335ee", ACCENT)   # 排名奖牌 金/紫/蓝
DANGER = ERROR                # 危险操作红（删除确认主按钮）
DANGER_ACTIVE = "#a8353a"     # 危险按钮按下/悬停态

# 图表网格虚线 / 刻度细条
CHART_GRID = "#272a2e"        # 趋势/散点中值虚线网格
RADAR_FILL = "#1e2e4a"        # 雷达数据面半透明深蓝 (canvas 无 alpha，实色近似)
BAR_TICK = "#505050"          # 排名条内分隔细线
GRADE_DIM = "#353535"         # 排名分布条中被过滤档的置灰段
TREE_SEL_BG = SEL_ROW_ACTIVE  # Treeview 选中行底 (setup_style map 用)
TREE_HEAD_ACTIVE = HOVER_BG   # 表头 hover
TREE_HEAD_PRESSED = CONTROL_BG  # 表头按下

# 趋势叠加折线固定 palette（最多 4 条线）。
OVERLAY_COLORS = (ACCENT, SUCCESS, WARNING, "#c586c0")
# 趋势背景评级带填充（键 = SCORE_TIER_BANDS 评级名）。
SCORE_BAND_FILL = {
    "优秀": "#162816", "良好": "#162030", "中等": "#2e2814",
    "待改进": "#2e1e14", "低效": "#2e1414",
}
# 仪表盘折线色 = GROUP_COLORS 各分类的「提亮同色相版」（展示局部色）。
DASH_LINE_COLORS = {
    "G1": "#949494",   # 中性灰
    "G2": "#569cd6",   # 亮蓝
    "G3": "#56ADBC",   # 亮青
    "G4": SUCCESS,     # 亮绿
    "G5": WARNING,     # 亮橙
    "G6": "#b65bd6",   # 亮紫
}

