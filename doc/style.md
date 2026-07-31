# TCER GUI 样式规范

界面视觉的唯一参考。配色 / 间距 / 字体的**代码 SSOT** 在 `tcer/gui/theme.py`；
本文解释每条规范的**为什么**与**怎么用**，供改 GUI 时对照，避免回退。

> 深色主题（VS Code 风）。任何新组件必须从 `theme` 取色取间距，禁止散落 magic number。

---

## 1. 配色 / 间距 / 字体（`theme.py` SSOT）

| 常量 | 值 | 用途 |
|---|---|---|
| `BG` | `#1e1e1e` | 最底层背景（图表绘图区右侧） |
| `PANEL` | `#252526` | 主面板 / Treeview / 图表 canvas 底 |
| `PANEL_2` | `#2d2d30` | 略抬升的面板（卡片、仪表盘格子） |
| `FG` | `#e0e0e0` | 主文字 |
| `MUTED` | `#9e9e9e` | 次要文字 / 轴刻度 |
| `ACCENT` | `#007acc` | 强调（主按钮、选中态、折线默认） |
| `BORDER` | `#3e3e42` | 1px 分隔线 / FlatMenu 边框 |
| `HOVER_BG` / `HOVER_ACCENT` | `#33333a` / `#1a8ce0` | hover 反馈 |
| `SUCCESS`/`WARNING`/`ERROR` | `#4ec9b0`/`#ce9178`/`#f48771` | 好/警/坏 |

- **间距**：`PAD_XS/S/M/L = 2/4/8/12`。容器 / 组件 padding 一律从这四档取，保持节奏。
- **字体**：`FONT_UI`(9) / `FONT_UI_BOLD` / `FONT_UI_SMALL`(8) / `FONT_HEADING`(10) / `FONT_VALUE`(11 粗) / `FONT_MONO`；CJK 与等宽字体名走 `theme.platform`。
- **数值颜色语义**：白色 = 基准值 / 纯数据；黄色（`LEVEL_COMPOUND`）= 含 magic number，仅参考。
- **数值方向语义色**（模型对比等对比场景）：`VALUE_GOOD` 绿 = 好方向、`VALUE_BAD` 红 = 坏方向、`VALUE_NEUTRAL` 灰 = 无方向/零值；`VALUE_BEST`（金 `#e0b341`）= **该行最优值**。方向染色逻辑收口在 `MetricCell.set_value`（按 metric `sentiment` up/down 判向）。
  - **金 ≠ 黄**：`VALUE_BEST`（金）标「行内最优」，`LEVEL_COMPOUND`（黄 `#f39c12`）标「含 magic number 的复合指标标题」——前者用于**值**、后者用于**标题**，不可混用。
- **视角标识色**：`VIEW_PROJECT`（橙）= 项目视角，与会话蓝（`ACCENT`）配对区分——与 §2 身份图标标准（项目/会话）同属一套身份体系，给项目相关 UI 染色用它，**不要用 ACCENT**。
- **收编常量**（曾散落为 magic hex，一律引用）：`CONTROL_BG` 控件底（分段控件/进度条槽/表头）、`CARD_HEADER_BG` 弹窗头部卡片底、`SEL_ROW_BG` 选中行淡蓝、`WARN_TINT_BG` 警示条暗橙底（配 `WARNING` 字）、`FG_WHITE` 选中态纯白字、`BORDER_HOVER` 卡片 hover 边框。
- **CTEI 评级色**：`GRADE_HEX`（优秀→极端低效）是 grade 染色唯一源，排名分布条 / 趋势 CTEI 带都派生自它。
- **分组色**：`GROUP_COLORS`（G1–G6 + `G_NEUTRAL`）是组级背景色，**为大块表头填充设计，偏暗**——细折线不可直接用（见 §9）。
- **Token 构成四色** `TOKEN_COLORS`（输入蓝/缓存写橙/缓存读青/输出黄）：时间线、模型弹窗、HTML 报告、会话对比**必须同源取此 dict**——曾因两套配色冲突（青一处=输出、另一处=缓存读）跨窗误读。注意缓存写与 `WARNING`、缓存读与 `SUCCESS` 同值，语义靠上下文区分。
- **图表 palette** `CHART_PALETTE`（6 色循环，语法高亮系）：多列对比、多模型色带、弹窗强调色统一从这取；小节标题 `SECTION_ACCENT`、基准值 `BASELINE_ACCENT`、奖牌 `MEDAL_COLORS`、危险操作 `DANGER`/`DANGER_ACTIVE`。

### 色相 → 语义倒排表（新增语义色前先查，防撞色）

| 色相 | 已占用语义 |
|---|---|
| 蓝 `#007acc` 系 | `ACCENT` 强调/会话视角；`HEATMAP_RAMP` 强度；`TOKEN_COLORS.input`；`CHART_PALETTE[0]` |
| 青 `#4ec9b0` | `SUCCESS` 好方向；`TOKEN_COLORS.cache_read` |
| 暖橙 `#ce9178` | `WARNING` 警示；成本条；`TOKEN_COLORS.cache_write` |
| 橙 `#cc7a1e`/`#f39c12` | `VIEW_PROJECT` 项目视角；`LEVEL_COMPOUND` 复合指标标题；`HEATMAP_RAMP_BAD` 坏方向强度 |
| 黄 `#dcdcaa` | `TOKEN_COLORS.output`；`BASELINE_ACCENT` 基准值 |
| 金 `#e0b341`/`#ffd700` | `VALUE_BEST` 行最优；`MEDAL_COLORS[0]` 金牌 |
| 红 `#f48771`/`#e53935` | `ERROR` 坏方向值；`DANGER` 危险操作 |
| 紫 `#c586c0` | `CHART_PALETTE[4]` 分类色/记忆文件强调 |

---

## 2. 图标体系

- **禁用 emoji**：跨平台渲染不一致、深色下发虚。统一用 **Icons8 material-outlined 白色线图标**，16×16 PNG，存 `tcer/gui/assets/ui-*.png`。下载源：`https://img.icons8.com/material-outlined/48/ffffff/<slug>.png`（限流，http=000 时需间隔重试）。
- **来源 / 厂商品牌图标**（Claude / Codex / Grok / omp / OpenCode）独立维护，**保持现状不插手**。
- **统一入口**：`views.ui_icon(name)` = `source_icon(master, "ui-" + name)`。
- **身份图标标准**（凡「项目 / 会话」出现处——列头、分段控件、右键菜单——一律遵守）：
  - 项目（identity）→ `ui-project`（源码文件图标）
  - 会话（identity）→ `ui-session`（**双气泡** comments，与「用户消息」菜单的**单气泡**区分）

---

## 3. 按钮

- 一律用 `widgets.flat_button(...)`；`primary=True` = 强调色主操作。
- **禁止**再手写 `tk.Button(relief="flat", ...)`。
- **例外**（刻意保留原样）：UploadDialog 按钮（归上传负责人）、删除确认弹窗（红色警示）。

---

## 4. 弹出菜单 = `widgets.FlatMenu`（必须）

Windows 原生 `tk.Menu` 自带一圈**无法去除的白色系统边框**——`borderwidth=0`、`*Menu*borderWidth=0` 都无效（系统 chrome，Tk 不可控）。

→ 所有右键菜单 / 下拉菜单必须用 `widgets.FlatMenu`：`Toplevel` + `overrideredirect(True)` + 1px `BORDER` 边框 + `grab_set_global`，body 走 `PANEL` 底。支持 `add_command`（hover=ACCENT、disabled 态）、`add_radiobutton`（● 标记）、`add_separator`、屏缘自动校正、点外关闭。

---

## 5. ttk 深色化（`theme.setup_style`）

clam 主题默认带浅色 bevel / 白边，逐元素压回暗色。关键陷阱：**`style.map` 优先级高于 `style.configure`**——只在 configure 里设色会被 map 的默认态覆盖。

- **Combobox**：①ttk style 设 `fieldbackground/arrowcolor`；②active 态 clam 会把箭头与按钮底刷白，必须在 **map** 里压回：`arrowcolor=[("active",FG)]`、`background=[("active",PANEL_2)]`；③**下拉列表不吃 ttk style**，另经 `root.option_add("*TCombobox*Listbox.background/selectBackground/...")` 双管齐下。
- **Notebook**：①client 白框 → `bordercolor/lightcolor/darkcolor = BG`（三色同设）；②页签 bevel → clam 的 map 默认 `selected` 态 `lightcolor=#eeebe7` 会给选中页签画白描边，**三色各态（selected/active/!disabled）都得在 map 里设 BG**；③点击页签的虚线聚焦框 → `focusthickness=0` 仍不够，clam 的 `Notebook.focus` 元素会描 1px 虚线，必须从 `style.layout("TNotebook.Tab", ...)` **移除 `Notebook.focus` 元素**（保留 `Notebook.label`，图标+文字照常）。
- **Treeview.Heading**：clam 在 hover/press 画 raised 白边，map 里压 `relief=[("active","flat")]` + 暗色 `background`。
- **Treeview body**：clam 默认 `bordercolor` 是亮白，深色下整个列表会包一圈白框；`style.configure("Treeview", ..., bordercolor/lightcolor/darkcolor = BORDER)` 三色同设压回深灰。

---

## 6. 聚焦环消除

聚焦环（虚线/实线框）在深色主题下显廉价，全局消除：

- **tk 组件**：`highlightthickness=0`（构造时，或 `root.option_add("*Button/*Checkbutton/*Radiobutton*highlightThickness", 0)`）。
- **ttk 组件**：移除 focus 元素（见 §5 Notebook）+ `focusthickness=0`。

---

## 7. 图表抗锯齿 = `charts._aa_layer`

`tk.Canvas` **不抗锯齿**，对角线与圆点锯齿明显（HiDPI 位图拉伸更糟）。

→ 趋势图 / 散点图的**折线与数据点**走 `_aa_layer(c, items, store, ss=2, pad=3, tag=None)`：PIL 2× 超采样画到**透明 RGBA** 图（`ImageDraw.line(joint="curve")` / `ellipse`），`LANCZOS` 缩回，`ImageTk.PhotoImage` 贴回 canvas。

- 透明背景 → 下层坐标轴 / 网格透出，上层选中标记盖在上面，**z 序天然正确、无需重排**。
- 图幅自适应 items 的 bbox，无需 plot 尺寸。
- `store` 列表持 PhotoImage 引用防 GC；**每帧 `_draw` 开头 `self._aa_imgs = []` 重置**。
- items：`("line", pts, color, width)` | `("dot", x, y, r, fill, outline [,lw])`。
- **仪表盘 sparkline 例外**：cell 极小，用 canvas 原生 `create_line(..., smooth=True)` + `create_oval` 即可，不上 AA。
- **趋势图选中点**：白色环（透明填充 + 白边 `width=2`）+ 白色内点，走 `_aa_layer` 抗锯齿（`create_oval` 在深色下锯齿明显）；不用 ACCENT 蓝（与数据色混淆）。`_aa_layer` 的 `tag` 参数让选中层可被 `c.delete("sel_overlay")` 增量擦除。选中标签 + tooltip 显示**会话标题**（非 session_id）。

---

## 8. 图表数值 = `charts._fmt_num`（禁科学计数法）

`f"{v:g}"` 对 |v|≥1e6 会输出 `1.2e+06`——在轴刻度 / 统计栏 / 仪表盘里既丑又难读。

→ 这些位置一律用 `_fmt_num(v)`：大数紧凑 K/M/B（与模型对比页 Token 紧凑惯例一致），小数定点保留有效位，**全程不产生科学计数法**。

- 轴刻度（趋势 Y / 散点 X·Y / 热力图 X·Y）、统计栏均值/中位/极值、仪表盘当前/均值，都用 `_fmt_num`。
- **指标语义值**（tooltip / HTML 报告）仍优先走 metric_defs SSOT（`format_plot` / `display`）。
- **例外**：CTEI 评级阈值 `:g` 保留——那是 0–10 的固定评级常量，不会科学计数法。

---

## 9. 仪表盘分类色：表头暗 / 折线亮

`GROUP_COLORS` 是为**大块表头填充**设计的暗色；直接画细折线在 `PANEL_2` 格子里会**灰蒙蒙**。

→ 折线用 `charts._DASHBOARD_LINE_COLORS`（各分类色的**提亮同色相版**，亮度 +65~+99、补饱和），表头仍用原 `GROUP_COLORS`。两者**同色相、深浅呼应**，按色识组依旧。

| 组 | 表头（GROUP_COLORS） | 折线（提亮版） |
|---|---|---|
| G1 | `#3a3a3e` | `#9d9da5` |
| G2 | `#1e4a6f` | `#4aa8ec` |
| G3 | `#1e5c5c` | `#2fc4c4` |
| G4 | `#1e5c2b` | `#4cc96a` |
| G5 | `#6f4a1e` | `#e2a23c` |
| G6 | `#5a1e6f` | `#b65bd6` |

- 若改了 `theme.GROUP_COLORS`，按此「同色相提亮一档」同步 `_DASHBOARD_LINE_COLORS`。
- 仪表盘**标题不显示 G1–G6 前缀**（只留指标名 + 单位括号），分类识别全靠颜色（表头条 + 折线）。
- 折线极值标记 ▲▼ 仍用语义色（max=`SUCCESS` 绿 / min=`ERROR` 红），不随分类色。

---

## 10. 趋势图图例（`_update_legend`）

- 图例标签底色**必须与所在头部容器一致**（`GROUP_COLORS["G_NEUTRAL"]`）。旧值 `theme.BG`（近黑）会在灰色头部上凸出一块更深的矩形色块，与主题冲突。
- 色点用 **10×10 实色块**（`tk.Frame(bg=color, width=10, height=10)` + `pack_propagate(False)`），不用 `●` 字符（字体相关、边缘发虚）。

---

## 11. 分段控件（segmented toggle）

深色 pill：`theme.CONTROL_BG` 容器 + 各 pill；选中 = `ACCENT` + 白字，未选 = `MUTED`。

- 每个 pill 包在**独立 Frame**里并同步 bg，避免选中态时 parent-bg 从子组件间隙透出「裂缝」。
- 模式切换（趋势图 / 散点图 / 仪表盘 / 时段）即此样式。

---

## 12. 滚动条

`widgets.ScrollFrame` 自带**按需显示**的极简细条：clam 原生 thumb + 自定义 layout 去箭头，只留滑块。

- thumb 深灰（`SCROLL_THUMB`）、hover 稍亮 `SCROLL_THUMB_HOVER`（**灰、非蓝**）、凹槽近背景隐形。
- 所有未显式指定 style 的 `ttk.Scrollbar` 统一继承。

---

## 13. Windows 高 DPI

`app._enable_windows_hidpi` **必须在创建 `Tk` 之前**调用（否则整窗被位图拉伸发糊），再 `_apply_tk_scaling`。

---

## 14. 界面偏好持久化

几何 / 分栏 / 筛选 / 上次项目 / 启动检查开关经 `core/ui_prefs` 存到 `core/app_dirs.prefs_dir()`（发布版优先 exe 同目录，不可写回退 `~/.tcer/`；早期 `~/.claude/` 位置自动迁移）：关闭时保存、启动时恢复（`last_project` 一次性生效）。

---

## 15. 启动脚本 `launch.bat`

优先 `pyw` / `pythonw`（`start` 分离，无残留控制台窗口）；文件 **GBK 编码**以保 cmd 中文注释正常。

---

## 16. 勾选行 = `widgets.CheckRow`（替代 Checkbutton）

`tk.Checkbutton` 的系统 indicator 在深色下是黑底方框、与文字割裂，老气。**全 GUI 不再用它。**

→ 勾选类一律 `CheckRow`：**无 checkbox 方块**，选中 = 整行高亮（淡蓝 `theme.SEL_ROW_BG` + 白字），未选 = 普通行，整行点击 toggle，hover 略亮。可选 `icon`（左）/`hint`（右，淡色说明）。

- 用于：趋势图指标选择（`MetricTrendSelector`）、仪表板「按日聚合」、分析弹窗「跳过 LOC」。
- **豁免**：UploadDialog 内 4 处 `tk.Checkbutton`（匿名/记住密码/会话详情/自动上传）归上传负责人，保持现状（代码以 `# style-exempt` 标记，契约测试跳过）。
- 单选/多选/上限逻辑在调用方；`CheckRow.click()` 只 flip `var` + 回调，由调用方 `_redraw()` 统一刷新所有行（单选会改别行 `var`）。`hint` 需先于标题 pack（否则 `expand` 标题会挤掉它）。

---

## 17. 可折叠标题 = `widgets.CollapsibleSection` + 分组折叠

「▼ 装饰标题」一律可点击折叠（与指标分类/模型对比分组一致），不再纯装饰。

- **通用区** `CollapsibleSection(parent, title, color, expand=)`：彩色标题（▼/▶，点击 toggle）+ `content` 容器（调用方把控件 pack 进去）。用于排名页（评级分布 / 会话排名）、CTEI 分解（CTEI 概览 / 因子分解 / 与项目均值对比）。
- **指标分类 `MetricPanel` / 模型对比 `ModelCompareView`**：每个分组自带 `header + body`，点 header `pack_forget`/`pack` body 折叠。
- **默认折叠**：指标分类「代码产出与质量」(G4)、模型对比「代码质量与行为」(M_QUAL)——项多、常只需概览。
- **状态保持**：`MetricPanel` 分组在 `__init__` 建一次、内存保持；`ModelCompareView` 每次 `update` 重建分组，折叠态存 `self._group_collapsed` dict（键=group id）跨 update 保持。都不持久化（重启回默认）。

---

## 18. 子窗口标题栏统一深色

Tk 子窗口（`Toplevel`）标题栏是系统渲染，深色应用下默认发白。

→ `platform.apply_dark_titlebar(widget)`：Windows 读注册表系统主题（`AppsUseLightTheme`，进程级缓存），设 DWM `DWMWA_USE_IMMERSIVE_DARK_MODE`（attr 20/19 兼容）；非 win32 no-op。

- **主窗口**：`app._apply_dark_titlebar(root)`（委托 platform）。
- **所有子窗口**：`widgets.new_window` 工厂自动应用，并在 `<Map>` 时再设一次兜底（部分窗口首次显示前 DWM 属性未生效），确保弹出时与主窗口一致深色。
- **无边框浮层**（`overrideredirect`：Tooltip / FlatMenu / CalendarPopup / ChartTooltip）无系统标题栏，不涉及。
- **限制**：仅启动时检测系统主题；运行中切换系统深/浅需重启 GUI。

---

## 19. 时间格式：统一本地时区 + 收口

- **显示** `format.fmt_dt(ms, fmt=FMT_*)`（本地）。格式常量 `FMT_DATE / MINUTE / SECOND / SHORT_MINUTE / SHORT_SECOND`——各处 `fmt_dt` 收口到常量，不再散落字面格式串；粒度差异有意（详情秒、卡片分）。
- **时长** `format.fmt_duration_ms(ms, short=)`：`short=True`（延迟/回合/审批/思考，英文 `<1s / 4.3s / 12m / 2.0h`）、默认（会话总时长，中文 `38 分钟 / 2.4 小时`）。**禁止裸 `f"{ms/1000}"` 无单位**。
- **「现在」** `format.fmt_now()`（HTML「生成于」标签，收口重复的 `datetime.now().strftime`）。
- **筛选阈值** `analyze._parse_date_to_ms` / `paths.since_date_to_ms` 都按**本地**时区（naive `datetime.timestamp()`）——与 `fmt_dt` 显示、FilterBar 预设 `datetime.now()` 口径一致。「今天」= 本地 0 点（曾用 UTC，凌晨会话被切；两者须逐字节一致，有契约测试）。
- **会话级过滤语义**：活跃区间 `[started_at, ended_at]` 与窗口相交即命中（跨天但今天仍活跃的会话算「今天」），与左栏 mtime 过滤一致——避免「左栏显示、点进去 0 会话」。

---

## 20. Tooltip（悬停提示）深色

`widgets.Tooltip`：`PANEL_2` 底 + `FG` 字，`Toplevel` `bg=BORDER` 透出 1px 边（替代老式 `relief="solid"` 黑边）。全局 hover 提示统一走它（曾用米黄浅色 `#fff8e1` + 深字，与深色主题冲突）。

---

## 21. 可选卡片 = `widgets.Card`

可点选的列表卡片（会话/项目列表项）。`PANEL_2` 底 + 1px `BORDER` 边（`highlightthickness` 实现，非 relief）。

- 交互三态：hover = 边框提亮 `BORDER_HOVER`（灰）；选中 = `ACCENT` 边框 + 加粗到 2px（`set_selected`）；选中态不被 hover 覆盖。
- 内容 pack 进 `self.frame`；**子组件需可点击时必须 `bind_to` 注册**（Tk 事件不冒泡，子 Label 会吞掉点击）。
- **何时不用**：纯静态展示面板直接用 `tk.Frame(bg=PANEL_2)`，不要为了边框套 Card。

## 22. 指标格子 = `widgets.MetricCell`

单指标 tile：彩色标题（色 = `LEVEL_COLORS[metric.level]`，§1 层级语义）+ 值（`FONT_VALUE` 等宽粗体）+ tooltip。

- 值色由 `set_value` 按 metric `sentiment` 自动判向：up/down 方向 → `VALUE_GOOD`/`VALUE_BAD`，无方向或零值 → `VALUE_NEUTRAL`，数据源不支持（`UNSUPPORTED_LABEL`）→ `MUTED` 弱化（与「无数据 -」区分）。
- 面板更新值走 `cell.var` / `set_value`，**不重建格子**。
- 方向染色逻辑只在这里收口，调用方不要自行染值色。

## 23. 可复制文本 = `widgets.SelectableLabel`

`tk.Label` 无法选中复制；长文本（用户消息等）需可拷出 → 用 `state="disabled"` 的 `tk.Text` 伪装 Label（flat 无边框、同 bg/fg/font），可拖选 + Ctrl+C + 右键「复制全文」。选中底色 `HOVER_ACCENT` + `FG_WHITE` 字。

- **换行按 `width` 字符列**，与像素宽度无关。**禁止从像素反推字符列**——未布局时 `winfo_width()=1` 会把行数算爆。定宽容器给显式 `width`，`count(displaylines)` 一次算高，无需 `<Configure>` 动态重算。
- **何时不用**：短静态文本用普通 `tk.Label`（Text 组件更重，大量实例有性能开销）。

## 24. 日历弹窗 = `widgets.CalendarPopup`

日期过滤「点选代替手输」。无边框 `Toplevel`（§18 浮层类，无标题栏问题）：`BORDER` 底透出 1px 边 + `PANEL` 头部，◀ 年月 ▶ + 周一首日 7×6 网格 + ✕ 清除。

- 选日回调 `on_select("YYYY-MM-DD")` 后自关；Esc / 失焦 / 点外关闭。
- **FocusOut 延迟 150ms 再绑**——窗口刚建时焦点抖动会误触发立即关闭，这是刻意的，勿「简化」掉。

## 25. 时段热力图（日历 / 周×小时 双视图）

曾直接搬 GitHub 绿系（`#161b22…#39d353`），两个问题：①空格比 `PANEL` 更黑，无数据格像「挖洞」；②亮绿与 `SUCCESS` 撞语义——成本/返工率高的格子显亮绿，暗示「好」，方向反了。

→ 空格 = `HEATMAP_EMPTY`（`PANEL_2` 略抬升灰）；数据四档 = `HEATMAP_RAMP`（`ACCENT` 蓝同色相 暗→亮，第三档 = `HOVER_ACCENT`）。蓝 = 强度中性，只表「多少」不表「好坏」。

- 分档用**四分位**而非线性 min-max：会话数/成本右偏分布下线性分档会把多数格子挤进最低档、图面死暗。全同值退化时取次亮档保证可见。
- 图例 5 格（空 + 四档）与格子取同一常量源，改色只动 theme；右侧动态显示**四分位档位边界**（`≤a · ≤b · ≤c · >c`，`_fmt_num` 格式），全同值/无数据时隐藏。
- **坏方向指标走橙阶**：`sentiment="down"`（返工率等）用 `HEATMAP_RAMP_BAD`（`WARNING` 同族橙，暗→亮 = 低→高「差」），蓝的「强度中性」语义不适用；图例色块随指标同步换色。
- **双视图**（§11 pill 切换）：日历（列=周，按天聚合）/ 周×小时（列=0–23 时，行=星期，X 轴每 3 小时一标）。两视图共用聚合器 `_aggregate(bucket_of)`、四分位分档、色阶。
- **边际汇总条**：右=按星期几聚合（横条 + 数值，一眼「周几最高」），底=按周/按小时聚合（竖条）；条色取 ramp 中间档，与格子同源。
- **今日格**：日历视图当天格 `FG_WHITE` 1px 描边定位「现在」。
- **周末分隔**：周五/周六行间一条 `BORDER` 细线，工作日/周末节奏可辨。
- **点击下钻**：点格子弹 `FlatMenu`（§4）列该时段会话（标题惯例 `meta.title or session_id or path.stem`，36 字截断，上限 15 条），选中经 `controller.on_select_session(sid)` 跳会话详情——聚合视图到明细的闭环。
- **格径自适应**：`min(可用宽/周数, 可用高/7)` 夹在 22–44px——下限保 GitHub 观感与横向滚动，上限防少数据时格子成巨块。
- **月份标签防重叠**：碰撞时**删前一个**（跨列少的月末部分月）、保新标签；被删标签若带年份则新标签升级为带年份格式，年份不丢；星期标签锚网格左缘 `ox`（非固定 `PAD_L`，网格居中时不脱节）。
