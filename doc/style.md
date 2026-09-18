# TCER Monokai Dimmed 工作台样式系统 2.0 (Workbench 2.0)

界面视觉与工程规范的唯一参考。配色 / 间距 / 字体的**代码 SSOT** 在 `tcer/gui/theme.py`；组件 SSOT 在 `tcer/gui/widgets.py`。
本文详细解释每条规范的**为什么**与**怎么用**，供后续迭代时对照，严禁回退。

> 基于 VS Code 官方 **Monokai Dimmed** 调色板与材质体系构建，纯 Python ≥3.11 标准库 Tkinter 零依赖实现。任何新组件必须从 `theme` 取色取间距，禁止散落 magic number。

---

## 1. 工作台四区拓扑架构（Workbench Topology）

TCER 全面重构为 VS Code 标准四区工作台架构（**顶部页签栏已移入侧栏纵向导航，主内容区顶格到顶**）：

```
┌────┬─────────────────────────────┬────────────────────────────────────────────────────────────────────────┐
│ ≡  │ 资源管理器  [时间▾][来源▾] ⟳ │                                                                        │
│ 项 ├─────────────────────────────┤ ● 项目全量汇总 · 21691 回合   支出: $1685.67   净增: +31,006行           │
│ 目 │ ▸ 指标分类        ← 选中    ├────────────────────────────────────────────────────────────────────────┤
│    │   模型对比                  │ ▼ Token 用量                                                           │
│ ≡  │   效率榜                    │                                                                        │
│ 会 │   趋势分析                  │ ▼ 代码与返工                                                           │
│ 话 │   项目聚合                  │                                                                        │
│    │   LLM 报告                  │ ▼ 会话交互                                                             │
│ ≡  ├─────────────────────────────┤                                                                        │
│ 聚 │ ▼ 项目 (46)                 │ ▶ 工具调用                                                             │
│ 合 │ ▼ 会话 (2)                  │                                                                        │
│ ⚙  │                             │ ▼ 诊断与建议                                                           │
├────┴─────────────────────────────┴────────────────────────────────────────────────────────────────────────┤
│ ⨀ 完成 · 共 24 个会话  │  全部 · Claude Opus 4.8  │  21691 回合 · +0行 · $1685.67  │  TCER v1.9.0          │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **左侧活动栏（Activity Bar，44px 宽）**——导航中心，两组结构（1px `MUTED` 短分隔线分组）：
   - 背景 `#353535` (`theme.ACTIVITY_BG`，官方 activityBar 亮柱)，1px 右边框分界；
   - **页组**（顶部导航，影响主区内容）：`ui-dashboard` 指标看板、`ui-model` 模型对比、`ui-rank` 效率榜、`ui-trend` 趋势分析、`ui-layers` 项目聚合、`ui-sparkle` LLM 报告；
   - **工具组**（底部入口）：`ui-upload` 上传、`ui-export` 导出菜单、`ui-tools` 工具与基准设置；
   - 激活态 = 白色 `RAIL_W_NAV=2px` 垂直 rail + 全亮图标（无底色切换）；未激活图标 65% 透明度；hover = `ACTIVITY_HOVER_BG #5a5a5a` + 90% 图标。主区容器为 `_NavStack`（ttk.Notebook 已退役，`set_page_active` 同步页组高亮）。
   - **视角切换**（项目视角 / 会话视角）已收拢至侧栏顶部的 `FilterBar` 中，联动各视图的数据颗粒度。
2. **主侧边栏（Primary Side Bar，280px 宽）**：
   - 背景 `theme.PANEL = #272727`；顶部标题条（资源管理器 + 时间/来源下拉 + 刷新）；
   - 纯资源管理器：项目卡片列表 + 会话卡片流（实时搜索与红旗过滤）——**主内容区无页签栏、顶格到顶**；
3. **主工作区（Editor Area）**：
   - 顶格到顶无标签栏噪声——原横向页签栏的 ~30px 高度还给内容；
   - 四级灰度分区：工作区底 `BG` → 卡片 `PANEL` → 区块头/状态条 `PANEL_2`。
4. **底部吸底状态栏（Status Bar，22px 高）**：
   - 背景 `#303030` (`theme.STATUS_BG`，与卡片同阶，明度跳变即分隔，无顶边线)；
   - 左侧：分析状态点（`● 就绪` / `● 分析中…` / `● 出错`，语义色属状态本体）+ 会话数量；
   - 中间：当前数据源与活动模型；
   - 右侧：当前会话/项目总消耗与代码增量（如 `550 回合 · +1076行 · $27.82`）+ 版本号。

---

## 2. 配色 / 间距 / 字体（`theme.py` SSOT）

### 工作台表面明度标尺（Surface Elevation）

**纪律：95% 面积收敛为 `BG → PANEL → PANEL_2 → CONTROL` 四级灰，强调色只给焦点/选中/状态。**

| 常量 | 值 | 说明 |
|---|---|---|
| `BG` | `#1e1e1e` | 主工作区 / 编辑器 / 画布最底层底色（官方 editor.background） |
| `PANEL` | `#272727` | 侧边栏资源管理器 / 容器底色（官方 sideBar.background） |
| `ACTIVITY_BG` | `#353535` | 左侧 44px 活动栏底色（**官方 activityBar.background——亮柱是 Monokai Dimmed 的主题特征，勿改成暗列**）；hover 用 `ACTIVITY_HOVER_BG #5a5a5a`（亮柱上全局 HOVER_BG 不可辨） |
| `PANEL_2` | `#303030` | 卡片底色、面板分割底色（官方 tab.border / panelSection.border 同阶） |
| `STATUS_BG` | `#303030` | 底部状态栏底色（与卡片 PANEL_2 同阶，明度跳变即分隔） |
| `SECTION_HEADER_BG` | `#272727` | 侧栏分组头条底色（对齐 VS Code 官方真机渲染：侧栏保持一体化平铺深色） |
| `CONTROL_BG` | `#3c3c3c` | 输入框底色、下拉组件槽底色（官方 input 默认值 #3C3C3C） |
| `CARD_HEADER_BG` | `#353535` | 卡片 / 区块头部略抬升底色 |
| `BORDER` | `#3e3e42` | 极细微结构分隔线——**只做结构分隔，永不表达状态** |
| `BORDER_HOVER` | `#505050` | 弹窗图形选择框描边（Card 体系已退出，勿再新增用途） |
| `HOVER_BG` | `#444444` | 列表 / 按钮悬停底色（**全局唯一 hover token**） |
| `SEL_ROW_BG` | `#1e293b` | 非聚焦选中底色 |
| `SEL_ROW_ACTIVE` | `#1e3a8a` | 聚焦高亮底色 (Cobalt 宝石蓝) |
| `ACCENT` | `#2563eb` | 现代高能焦点与徽标蓝 |
| `RAIL_W` / `RAIL_W_NAV` | `3` / `2` | rail 宽度 SSOT：内容卡状态轨 / 导航指示条 |

### 前景色与排版标尺（Typography & Text）

- `FG = "#e6edf3"`：纯净白文本色（**数值默认同此色**）。
- `FG_WHITE = "#ffffff"`：纯白——**白名单专用**：Card 选中提白、活动页签、活动栏 rail 与激活图标、flat_button primary、弹窗大标题。
- `MUTED = "#9ca3af"`：次要文本、标签与刻度。
- **字阶（纪律派：数值=正文字号 10pt，层级靠字重+等宽家族，杜绝仪表盘大数字）**：
  - `FONT_TITLE = (FONT_CJK, 12, "bold")`
  - `FONT_HEADING = (FONT_CJK, 11, "bold")`
  - `FONT_UI = (FONT_CJK, 10)`
  - `FONT_UI_BOLD = (FONT_CJK, 10, "bold")`
  - `FONT_UI_SMALL = (FONT_CJK, 9)`
  - `FONT_UI_SMALL_BOLD = (FONT_CJK, 9, "bold")`
  - `FONT_VALUE = (FONT_MONO_NAME, 10, "bold")`（= FONT_MONO 的粗体配对，与 FONT_UI 同字号）
  - `FONT_MONO = (FONT_MONO_NAME, 10)`
  - `FONT_STAT = (FONT_CJK, 9)`
- **间距**：`PAD_XS/S/M/L = 2/4/8/12`，容器 / 组件 padding 统一从这四档取。
- **9pt 分界线**（2026-09-15 体验轮）：用户主扫读的信息正文一律 9pt（指标名/chip 名/卡片摘要/徽标/彩色文字）；8pt 仅限真注脚（副注、pill 计数、图表刻度、盘符、配对小标签）——8pt 彩色小字在暗底不可读，禁止彩字 8pt。
- **8px 网格节奏**（deck chip / MetricCell tile）：内距 8/4、格沟 4/4（列沟 8px）、名称→数值行距 2、副注间距 4、deck 外距 12/4。

### 语义色系（Monokai Dimmed Syntax Palette）

- `SUCCESS = "#22c55e"`：活力翡翠绿——状态点/色轨用；**不用作数值奖励色**。
- `WARNING = "#f59e0b"`：明亮温暖金橙。
- `ERROR = "#ef4444"`：清晰活力珊瑚红。
- **「异常才着色」纪律**：数值无绿色奖励、无非零即色；只有真异常阈值允许警示色（返工率 ≥35% 红 / ≥15% 黄、工具错误率 ≥10% 红 / >0 黄、成本 ≥$50 橙、先读后写 <0.3 红）。效率榜得分轴值、项目聚合卡成本均已中性化（好坏方向由 bar 图形色表达）。
- **彩色文字可读性**（2026-09-15 体验轮）：彩色文字一律 ≥9pt；`GRADE_HEX` 良好/低效两档为暗底可读性提亮（`#6c8ce5`/`#d0606a`，对比度 <2:1 的原值禁用）；彩色底上的文字用 `FG_WHITE`（分布条模型名）。
- `LEVEL_BASIC = "#e6edf3"`：绝对基准值 / 纯数据。
- `LEVEL_COMPOUND = "#eab308"`：明亮金黄（**含 magic number 的复合指标**——产品语义，指标标题保留；与 WARNING #f59e0b 明确区分）。
- `VALUE_NEUTRAL = FG`（别名）：数值默认 = 正文色。
- `VALUE_BEST = "#D0B344"`：行最优值金色标记（模型对比）。
- `VIEW_PROJECT = "#D08442"`：柔和暖橙项目视角标识色。

---

## 3. 核心通用组件升级（`widgets.py`）

### 现代卡片（`Card 2.0`）
- **边框退出状态表达**（`highlightthickness=0`，契约测试守卫）：hover / selected 全靠底色 + 左 rail，容器分区靠明度阶；
- 未选中底色 `#272727` (`theme.PANEL`) 或 `#303030` (`theme.PANEL_2`)；
- Hover 态：底色过渡为 `#444444` (`theme.HOVER_BG`)，**全局唯一 hover token**；
- Selected 态：整卡背景填充 `#4e4e4e` (`theme.SEL_ROW_BG`)，左边缘 `RAIL_W=3px` 状态轨（`theme.ACCENT` 或报告终态色），标签自动提白 `#ffffff`（`_label_fgs` 白名单：FG/MUTED 起源自动升白——提白是选中的专属奖励）；
- **rail 语义专用**：选中/焦点/数据状态（Card 选中、tier 状态轨、诊断色轨、活动栏焦点轨）——不可交互容器（deck 头等）不加 rail；
- 支持 `set_state_rail(color)` 动态定制状态轨颜色。

### 诊断条目色轨制（Insights 色轨）
- 条目 = 中性底 (`PANEL_2`) + 左 `RAIL_W_NAV=2px` 色轨（红=drag / 黄=warn / 绿=good），无边框；
- 标题 `FG` 9pt bold、正文 `MUTED`——**正文全中性**，级别只由色轨与分组标题承担（VS Code Problems 面板做法）。

### 手风琴折叠区块（`AccordionSection`）
- 表头高度固定 28px，底色 `#353535` (`theme.CARD_HEADER_BG`)；
- **折叠箭头全 GUI 统一细箭头 `▾`（展开）/ `▸`（折叠）**——deck 头 / 组头 / 洞察分组 / 无数据 expander / charts 组头一律同款（排序箭头 `▼/▲` 与左右指示 `◀/▶` 是另一语义体系，不混用）；
- 左侧折叠箭头，中间标题，右侧数量/操作徽标胶囊；
- 内容容器平滑展开/折叠，完美兼容旧版 `CollapsibleSection` 调用。

### 交互反馈（2026-09-16 体验轮）
- **Tooltip 300ms hover 延迟**（= VS Code `workbench.hover.delay` 默认）——立即弹出在扫读列表时闪烁；点击目标 widget 即隐；
- **全局快捷键最小集**：`Ctrl+F` 聚焦会话搜索并全选、`F5`/`Ctrl+R` 刷新项目（`bind_all`，不受按钮 `takefocus=0` 影响）；
- **表头排序方向指示**：可排序 Treeview（效率榜 / LLM 报告列表）当前排序列标题尾缀 `▾` 降序 / `▴` 升序，点击翻转，其余列无符号；
- **hover 全局唯一 token**：MetricCell 网格 hover 与 chip/卡片同走 `HOVER_BG`（不再用 PANEL_2 充当 hover）；
- **滚轮假 Leave 守卫**：`ScrollFrame` 在指针仍处 canvas 矩形内（移入嵌入子窗）不解绑滚轮；
- **窗口最小尺寸** `1080×540`——分栏 minsize 不约束窗口缩放，极小窗口会挤压重叠。

### 对标调研结论（2026-09-16，**Monokai Dimmed 官方主题文件**逐 token 对照）

基准 = VS Code 内置 `theme-monokai-dimmed/themes/dimmed-monokai-color-theme.json`（**不是 Dark Modern**——后者 chrome 全暗 #181818，与本主题血统不同，勿混用其论据）：

- **逐字节命中的 token**（血统纯正，勿漂移）：editor/`BG` #1e1e1e、foreground/`FG` #c5c8c6、sideBar/`PANEL` #272727、menu.background/FlatMenu `PANEL`、list.hover/`HOVER_BG` #444444、list.activeSelection/`SEL_ROW_ACTIVE` #707070、list.inactiveSelection/`SEL_ROW_BG` #4e4e4e、focusBorder/`ACCENT` #3655b5、`BORDER` #303030；语义色同源 terminal ansi（#86B42B 绿 / #56ADBC 青 / #c586c0 品红即 `CHART_PALETTE` 成员）。
- **已修正对齐官方**（回归主题本源）：`ACTIVITY_BG` → **#353535**（官方亮柱；曾两度误改 #1e1e1e/#1a1a1a，均为 Dark Modern 思路的错误引申）；`STATUS_BG` → **#505050**（官方亮灰条，标志性亮 chrome）；`SECTION_HEADER_BG` → **#505050**（侧栏分组头）；`CONTROL_BG` #3c3c3c 恰为官方 input 默认值（暗槽配亮组头）。
- **尺寸/交互**：活动栏 44px = VS Code 2026 最新值（PR #324488）；状态栏 22px；hover 延迟 300ms（`workbench.hover.delay` 默认）；活动项无底色切换（官方 modernActivityBarItem.activeBackground=底色本身）——白 rail + 全亮图标同构。

### 模型对比页纪律（2026-09-15 对齐）
- 组头与 MetricPanel deck 头同款：`PANEL_2` 底 + `FG` 标题 + 无 rail（rail 语义专用）；
- **列名 / 模型名一律 `FG`**（异常才着色；段色只存在于成本占比条自身）；
- 成本占比条**仅 ≥2 模型时绘制**（单模型 100% 单色条无信息量），段色取 `theme.CHART_PALETTE`；
- 行最优值金色 `VALUE_BEST` 保留（产品语义：多列对比的行内最优标记）。

### charts 页（趋势/散点/相关/仪表板/时段）纪律（2026-09-16 统一）
- 全部组头与主区同语言：`PANEL_2` 底 + `FG` 9pt bold（旧 G1-G6 彩底/8pt 已清除——`GROUP_COLORS` 只用于图表配色，不再作组头底色）；
- 图例底色跟随所在组头（`PANEL_2`），图例文字 9pt `FG`。

---

## 4. 会话卡片精简与顶栏优化

### 会话卡片三行高信噪比排版
- **移除第 4 行裸露 UUID**：将会话 36 位裸哈希移入卡片 Tooltip（悬停完整展示）与右键菜单（“复制会话 ID”）；
- **修复金额错误染色**：普通成本使用中性前景色 `#c5c8c6`，仅当成本超异常阈值（≥$50.00）时采用 `theme.WARNING` 警示橙；
- **行级布局**（信息密度克制——卡面只留三行，详细指标进 Tooltip）：
  - **Row 1**：时间（`09-15 11:12`，9pt Mono MUTED） + 右侧置顶/红旗标记（未标记时悬浮显示）；
  - **Row 2**：会话主标题（9pt Bold 正文色——列表行主文本靠字重；选中时 Card 提白升一级）；
  - **Row 3**：左「模型短名 · 时长」（9pt MUTED）+ 右成本金额（9pt Mono Bold）；回合/LOC/返工等完整摘要在 Tooltip 悬浮可见（`550 回合 · +1,076 行 · 极少返工`，**数字千分位 + 数字与单位间空格**，全 GUI 统一）；
- **左缘状态色轨（State Rail）**：左边缘根据会话终态绘制 3px 垂直色条（绿色=优秀/良好，橙色=待改进/低效）。

### 时间预设下拉胶囊
- 侧栏顶栏（资源管理器标题行）承载时间筛选：`[ 时间: 今天 ▾ ]`，与「来源」下拉同款胶囊（FlatMenu 四档：今天 / 本周 / 本月 / 全部），**启动默认「今天」**、不随偏好恢复；
- 自定义起止日期与日历点选已按产品决策移除（`CalendarPopup` 随之退役）；
- 胶囊统一高度 26px，底色 `#303030`，边框同色，hover `#444444`。

---

## 5. LLM 报告工作区与相图优化

### 报告列表抽屉化
- 在“LLM 报告”页签左侧列表上方增加 `[◀ 折叠]` 按钮；
- 支持一键将报告列表折叠为 36px 窄边条，将主工作区全部 1200px+ 宽度让渡给相空间动力学相图与长篇复盘正文；
- 会话联动：在会话列表中点选会话时，自动定位并联动展示该会话的专属报告。

### 相空间动力学相图三舱化
- **控制舱**：视图模式选择（时序流形 / 相速度极限环）+ 视口缩放微胶囊（－ / 100% / ＋ / 复位）；
- **遥测芯片舱**：意图降熵、偏离敏锐、反馈收敛、认知平衡四能力芯片微卡；
- **终态定性舱**：`[ 成功破局 · 达成收敛 ]` 状态徽标胶囊，带 Monokai 橄榄绿光晕。

---

## 6. 样式契约与防回退机制（Style Contract）

`tests/test_style_contract.py` 严格守护本规范：

1. **禁新增 Magic Hex**：全工程所有非豁免文件的 Hex 代码必须来自 `theme.py`，历史遗留 hex 采用严格棘轮机制（2026-09-15 收紧至 2 个——仅剩 charts/popups 画布暗刻度 `#444444/#555555`，只许减少不许增加）；
2. **禁原生 `tk.Menu`**：必须使用 `widgets.FlatMenu`，杜绝 Windows 系统原生白边 chrome；
3. **禁手写 `tk.Button`**：必须使用 `widgets.flat_button`，保证全局统一的圆角、内边距与 hover 质感；
4. **禁原生 `Checkbutton`**：必须使用 `widgets.CheckRow`，提供现代全行选中浸润交互；
5. **禁图表科学计数法**：图表数值必须经 `_fmt_num` 格式化为 K/M/B 易读格式。

---

## 7. 顶级暗色开发者工具设计哲学与去噪法则 (Quiet Chrome & Calm Data)

参考世界级开发者工具（Linear、Vercel、VS Code）的核心设计规范，TCER 2.0 确立了如下去噪设计法则：

1. **明度阶梯（Luminance Steps）替代边框囚笼（Zero-Border Cages）**：
   - 传统暗色 UI 常误给每个单元格套 1px 细线边框，导致满屏像方格网（Border Cages）。
   - 规范法则：在暗色界面中，明度越高距离眼睛越近。通过背景微明度差（`#1e1e1e` 底座 → `#272727` 容器 → `#303030` 卡片）自然实现物理抬升与层级堆叠；指标网格内部彻底无框化，仅以呼吸留白和鼠标悬停微光交互。

2. **色彩语义收敛（Monochrome Minimalism & Restrained Sentiment）**：
   - 拒绝“圣诞树效应”：严禁凡数值必染高亮红绿（如增删行数全染绿、正常成本全染红）。
   - 规范法则：90% 的常规数据使用舒适中性白（`#c5c8c6`），整个界面保持单色系（Monochrome）的素雅与沉静。仅在核心诊断指标触碰极端阈值时（缓存命中率 >75% 橄榄绿、自返工率 >35% 绯红、工具错误数 >0 绯红）才点缀警示色。

3. **面板融合，消灭顶栏（Zero-Waste Panel Integration）**：
   - 彻底废除悬浮横跨全屏的独立工具条，腾退 34px 宝贵纵向高度；
   - **侧栏顶栏**直接承载数据源筛选、日期范围选择、视角切换与刷新，就地筛选项目与会话；
   - **主工作区顶格**：横向顶栏完全消除，由左侧活动栏统领全部六个核心视图与工具菜单，零额外高度开销，整窗自上而下顶格对齐。

4. **原生子窗口与沉浸弹窗体系（Native Sub-windows）**：
   - 详情、设置、时间线、雷达、上传等所有辅助弹窗统一采用原生居中 `tk.Toplevel` 子窗口（`widgets.new_window`）；
   - 配备系统深色沉浸式标题栏、居中定位、`<Escape>` 快速关闭，不侵占主工作区内容与页签，避免次级内容污染主工作台；
   - 主编辑区内容顶格到顶，由左侧活动栏独占纯净导航（指标看板、模型对比、效率榜、趋势分析、项目聚合、LLM 报告），彻底移除顶部重复的横向页签栏。
