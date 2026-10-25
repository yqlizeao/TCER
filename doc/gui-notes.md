# GUI 细节与踩坑（gui-notes）

> 配色/图标/组件规范的 SSOT 是 [style.md](style.md) 与 `TCER/gui/theme.py`；本文收录从 CLAUDE.md 迁出的 GUI 实现细节、交互约定与踩坑。

> 本节自 CLAUDE.md 迁移而来（2026-10 瘦身）：CLAUDE.md 只保留一行硬规则，完整细节、实测数据与历史缘由在此。条目前缀「原 #N」= 瘦身前 CLAUDE.md 的「关键注意事项」编号，可据此追溯。

## 样式与基础设施

<a id="style"></a>
### 原 #24 GUI 样式

- （完整规范见 [doc/style.md](style.md)；配色/间距/字体 SSOT 在 `tcer/gui/theme.py`）：
  - ①Windows 高 DPI 感知在 `app._enable_windows_hidpi`（**必须在创建 Tk 之前**调用，否则整窗位图拉伸发糊）+ `_apply_tk_scaling`（mac/Aqua 跳过——Tk 自管点单位，显式改会虚胖）；恢复的窗口几何经 `app.clamp_geometry` 钳进当前屏幕（1920×1080 存的窗口搬到小屏/mac 会超屏，多显示器拔掉后 +3000 偏移会让窗口整个落在屏外），默认尺寸随屏幕收缩；
  - ②按钮一律用 `widgets.flat_button`（`primary=True`=强调色主操作），禁手写 `tk.Button(relief="flat")`（UploadDialog / 删除确认弹窗例外）；
  - ③间距从 `theme.PAD_XS/S/M/L`（2/4/8/12）取值；
  - ④**禁用 emoji 当图标**——统一 lucide 线性白线图标（与 cc-switch 同一图标库，ISC；2026-10 由 Codicons 迁移）。**唯一生成方式** `python scripts/build_icons.py`：角色→lucide 名映射表 `ICONS` 是 SSOT，构建期从 unpkg 取固定版本 SVG、超采样光栅化成 16/22px PNG 存 `gui/assets/ui-<角色>[-22].png`，运行时零依赖；换图标只改映射重跑，代码 `views.ui_icon(master, "<角色>")` 不动；禁止手绘/混用其它图标族，来源/厂商品牌图标不动；身份图标标准：项目→`ui-project`、会话→`ui-session`（双气泡）；
  - ⑤**弹出菜单必须用 `widgets.FlatMenu`**（Windows 原生 `tk.Menu` 有去不掉的白边系统 chrome，`borderwidth=0` 无效）；
  - ⑥**聚焦环全局消除**（tk `highlightthickness=0` / ttk 移除 focus 元素 + `focusthickness=0`）。
  - **Windows 经典 `tk.Button` 的持焦蚂蚁线是系统 `DrawFocusRect` 画的，`highlightthickness=0` 挡不住**（像素差分实测），唯一解法是 `takefocus=0` 不抢焦点——`flat_button` Windows 分支两者都传（曾只传前者，用户实测虚线框仍在；mac 分支是 Label 无此问题）。例外：删除确认弹窗**刻意** `focus_set()`（回车不误删），保留焦点行为；
  - ⑦Notebook/Combobox 深色化陷阱：`style.map` 优先级高于 `configure`，active 态白边与页签虚线聚焦框都得在 map / layout 里压（下拉列表另经 `root.option_add`）；
  - ⑧**图表折线/点抗锯齿**走 `charts._aa_layer`（PIL 2× 超采样贴回 canvas；Tk RGBA 照片转换成本随 alpha 种类数暴涨——LANCZOS 边缘上千种渐变 alpha 曾让一次重绘 107ms/层，`_aa_layer` 内量化到 16 档后 ~23ms、视觉不可辨，勿删），**数值禁科学计数法**走 `_fmt_num`（大数 K/M/B，与模型对比页一致；指标语义值仍走 metric_defs `format_plot`）；
  - ⑨仪表盘折线用分类色**提亮版** `_DASHBOARD_LINE_COLORS`（原 `GROUP_COLORS` 偏暗、画细线灰蒙蒙，提亮后同色相按色识组），标题去 G1–G6 前缀；
  - ⑩趋势图例标签底色随所在头部容器、色点用 10×10 实色块；
  - ⑪`ScrollFrame` 自带按需显示的深色滚动条；
  - ⑫界面偏好（几何/分栏/筛选/上次项目/启动检查开关）经 `core/ui_prefs` 持久化到 `core/app_dirs.prefs_dir()`（发布版 exe 同目录，回退 `~/.tcer/`；早期 `~/.claude/` 自动迁移），与 Claude 数据源 `~/.claude` 分离；
  - ⑬**Toplevel 子窗口不设显式「关闭」按钮**（标题栏 × + Esc 即可,显式关闭按钮冗余;`UpdatePopup` / `RadarPopup` / `ConfirmDeletePopup` / `UploadDialog` 均此模式）。色值表 / 各组件 why / 图表细节见 [doc/style.md](style.md)。
  - ⑭**卡片 hover / 选中换色**：`Card._apply_bg` 除登记控件外，还会递归把「与卡片当前底色相同」的 Frame / Label 一并换色——漏 `bind_to` / `track_bg` 的行容器曾在悬停时露出旧底色色斑（项目聚合卡）；刻意另配底色的子区（展开明细的 PANEL 表格）不受影响。
  - ⑮**卡片文字截断用 `widgets.fit_label`**（按像素宽 + 省略号，宽度变化自动重算；标签 `width=1` + `fill="x", expand=True`）。按字符数截断会切在半个字上（「hover扌」「3.4 小」）。
  - ⑯**数值字体用 `theme.value_font(text)`**：纯数字用等宽粗体，带中文单位 / 说明的值（「12.1 小时」「864 次（8 种）」）改 UI 粗体——等宽字体缺 CJK 字形，逐字回退后字距松散。指标网格列 `uniform="metric"` 等宽，跨分组列线对齐，超长值由 `MetricCell` 按格宽折行。
  - ⑰**窄栏胶囊组用 `widgets.FlowRow`**（place 流式换行），单行 pack 排不下会直接裁掉末尾控件；窄栏工具按钮纯图标（文字进悬浮提示）且先于左侧标签 pack。
  - ⑱**输入框用 `widgets.entry_box`**（同色 Frame 包一层留内衬；不用 highlight 边框，style 契约禁止）。

<a id="ranking"></a>
### 原 #25 排名页（**已移除**，2026-10）

- **效率榜页签与 `views.ScoreRankingView` 已整体下线**（人为拟合评判不符合工具定位；评估职能由 LLM 报告承接）。主区 6 页签：指标看板/模型对比/趋势分析/项目聚合/LLM 报告/术语库；ActivityBar 页组与 `_render_tab` 序号已同步重排。core 评分计算（`metrics.efficiency_score`/`tier`/`score_decompose`）保留供 CSV/上传/服务端。`launch.bat` 优先 `pyw`/`pythonw`（`start` 分离，无残留控制台；文件为 GBK 编码保 cmd 中文注释）——此条仍有效。

## 视图与页签

<a id="views"></a>
### 原 #35 GUI 新视图（v1.6.0）

- 趋势页新「相关」pill = `CorrMatrixChart`（N×N Pearson r，正=ACCENT 深浅/负=WARNING 深浅，点格下钻散点图自动设轴）；仪表盘右键格子换指标（`ui_prefs['dashboard_metrics']` 持久化，重启恢复）；会话卡片摘要行（左主模型短名、右成本金额纯文本；无评级徽标）；SessionColumn 卡片只建一次、搜索/红旗过滤走 pack 复用（~600ms→~1ms），构建分批 after(1)（勿用 after_idle——update_idletasks 会把 idle 批全部同步跑完）。切项目走页签惰性渲染（`app._refresh_views/_render_tab`：只画当前页签，其余标脏、`<<NotebookTabChanged>>` 补渲染）。`SelectableLabel` 自适应高度合并结算（类级待测队列 + 单定时器；tkinter 的 after 回调注册在调度 widget 名下、widget 销毁则定时器失效——须锚定 toplevel 并自愈，见 widgets.py 注释）——逐实例 update_idletasks 曾让效率榜一次刷新卡 2s；时间线弹窗点击回合展开明细 + CHR 虚线/累计净增行/压缩竖线叠加，另含「收敛诊断」双视图（左轴累计净增 × 右轴累计成本剪刀差 + 重试循环横带/最贵回合▼/缓存失效×标记；数据由弹窗调 `metrics.turn_cost_analysis`/`retry_loop_metrics` 现算——两函数返回逐回合序列 `turn_costs`/`cache_invalidation_turns`/`spans`，`compute()` 挂载点与 `SessionReport` 不挂序列，避免 CSV/HTML 护栏连带；非 Claude 源无 `turn_net_locs` 时净增线降级不画）；会话对比顶部叠加雷达 + 逐回合曲线；项目总览气泡散点（X=成本 Y=TCER 气泡=Token，点击选表行）；HTML 项目报告选节导出（`render_project_html(sections=...)`，`HtmlSectionsPopup`）。

<a id="smart-view"></a>
### 智能项目/会话视角（选中驱动，无切换开关）

- **视角 = 当前选中**：有选中会话 ⇔ 会话视角；否则项目视角。唯一写点 `app.TcerGui._set_session_view(sid)`（同步 activity_bar 兼容桩）；「项目/会话」分段控件与项目右键「项目视角/会话视角」均已移除，`FilterBar` 不再持有 `view_mode`。
- **丝滑回程两条**：①再点已选会话卡 = 取消选中回项目汇总（`SessionColumn._select` 识别，`on_select_session(None)`）；②会话视角下点当前项目卡 = 清选中回项目视角，**不重算**（数据没变）。无选中会话时点项目保留「点项目 = 重算」语义（点当前项目即刷新）。项目右键菜单已收敛为「在资源管理器中打开 + 复制项目路径」两项（「刷新此项目」「复制项目名称」及视角切换项均已移除）。
- **不自动选第一个会话**：`_on_analysis` 只在「同项目重分析且上一选中会话仍可见（或处于分批构建待选 `_pending_select_sid`）」时延续选中（会话视角保持）；否则 `_set_session_view(None)` 落回项目视角。`_clear_analysis_view` 同步复位视角，保证「sid=None ⇔ 项目视角」不变量处处成立。
- **选中来源全走同一入口**：会话卡 / 效率榜排名行 / 趋势图定位均触发 `on_select_session`；各消费方（`_render_metrics`/`_update_ranking_view`/`_update_model_compare`/状态栏徽标/「会话记录」按钮）沿用 `mode == "session" and _selected_session_id` 双条件守卫，不变量下行为自洽。
- **指标看板小分类平铺**：`MetricPanel._build_group` 不再按 `group.subgroups` 渲染「· 基础/行为/质量」标题行，G4 整组一张网格（`group.metrics` 即 `_subgrouped` 保证的扁平序）。子类结构保留在 `metric_defs`（HTML 报告 `html_report.py` 仍按子类分节），勿在看板重新加回小分类行。

<a id="real-projects"></a>
### 原 #37 真实项目聚合页签（v1.7.1，「项目聚合」）

- 同一工作目录混用多个 agent 是真实形态（本机实测 42 卡 → 22 个真实项目，主力项目 4–5 卡：claude×2 配置根 + codex/omp 等）。
  - **刻意不改主界面项目列表**——只加独立页签：
  - ①库层 `analyze.RealProject` + `real_projects(refs)`：聚合键 = `normcase+normpath(cwd)`（盘符大小写/斜杠/尾斜杠折叠），**父子路径不合并**（`c:\playground` ≠ `c:\playground\langfuse`）；Claude ref 层无 cwd → `_claude_ref_cwd` 读首会话 meta（`read_session_meta` 走 file_cache；
  - **key 反解不可靠**——Claude 编码把 `.`/空格折叠成 `-` 有损）；读不到 cwd 的 ref 独立成组（`ref:<source>:<key>`），宁可不合并不错合并。
  - **不同盘符同名工程不做自动聚合**（basename 判据误伤率高、纯离线不能读 git remote 验证；将来可做手动别名）。
  - ②GUI `views.RealProjectsView`（第 5 页签，**卡片式三行布局**）：第 1 行项目路径（`_display_cwd` 盘符统一大写——元数据常见小写 `c:\`）+ 右侧成本金额（**与会话卡片同款**：`$` 两位小数、方向着色 >0 红/$0 绿、`FONT_VALUE`；卡片不显示评级徽标，同会话卡约定）；第 2 行 agent 品牌图标条（`source_icon`，与项目卡同款图标库，Claude 自定义根自动用 ccswitch 图标——图标条本身区分两套部署；悬停见来源名，**图标优先于文字标注**）；第 3 行摘要「N 会话  N 请求  N 用户消息  X亿 Token」（空格分隔；请求数口径同 SSOT——按助手响应数，悬停有完整说明），净增/TCER/千行代码成本/缓存命中率放展开明细不在卡面堆砌，Token 大数用 `fmt_approx_cn` 中文量级「21.6亿」。点卡展开各源明细 = **固定列网格**（来源 + 按语义分组排列：总会话/总请求数/总用户消息 → 总 Token/总成本/平均千行代码成本 → 总净增行/平均TCER → 平均缓存命中率 → 项目综合效率分（聚合级，`f"项目{metric_name('score')}"`——与效率榜项目视角同名同派生）；
  - **列名口径前缀（用户约定，优先于「SSOT 全名」守则）**：求和列冠「总」、比率列冠「平均」（实为按合计重算 ≠ 会话算术平均——Simpson 悖论；SSOT 全名与公式在表头悬浮）；表头 + 定宽列 grid 跨行严格对齐，来源列 weight=1 拉伸、数值列右对齐、**除 TCER 外不用缩写**）。
  - **默认按会话数排序**，排序走工具栏 FlatMenu 下拉，同源多卡在标签标注所属根（如 `Claude（.zclaude）`）。
  - ③app `real_projects_scan`：后台扫全部项目，**聚合口径比率一律「分子和÷分母和」重算**（TCER/CPE/CHR/返工/错误率按合计，不取各卡平均——Simpson 悖论），效率分从聚合轴输入重算（聚合 TCER 作产出轴代理、先读后写取中位数，同 server `_agg_metrics` 处理）。页签**不接入 `_refresh_views` 脏标记循环**（不依赖当前分析，首次切入自动扫、工具栏刷新强制重扫）；`_render_tab` 对 idx==4 特例先于 `_current` 检查（未选项目也能看）。

<a id="transcript-reader"></a>
### 会话记录阅读器（#45，`gui/transcript_view.py` + `gui/transcript_md.py`）

复刻 cc-switch 的会话记录查看体验（版式 / 文案取自其 `sessions/reader/*`；MIT，署名见 THIRD_PARTY_NOTICES.md）。数据层是 `core/transcript.py`（`load_transcript` / `flatten_rows` / `find_search_hits` / `build_turn_index` / `transcript_to_markdown`），GUI 只做展示，**不进指标 / CSV**，页头数字全取自 `Transcript`（成本缺失显示「-」）。

- **入口**：会话卡片右键「查看会话记录」→ `SessionColumn._on_right_click` → `TcerGui.show_transcript(report, siblings)`；指标看板会话视角头部「会话记录」按钮或点击「用户提问」指标格 → `show_transcript_for_rendered`（已淘汰旧版 `user_messages` 弹窗，统一迁移至会话记录阅读器；项目汇总视角隐藏会话记录按钮）；`siblings` = 会话列表当前过滤 / 排序结果，窗口内「上一个 / 下一个会话」在其中切换（仅 1 个会话时 `< 1/1 >` 自动隐藏）。已开窗口复用（`app._transcript_win`），不重复开。
- **线程模型**：`load_transcript` 在 daemon worker 里跑；结果经窗口**自有** `queue.Queue` + 主线程 `after(60)` 轮询回填（加载中 30ms），禁裸跨线程 `after`；`except … as e` 里闭包先 `err_text = str(e)`。切会话先 `cancel_event.set()` 取消上一次，`_load_gen` 代号丢弃过期结果；失败显示「加载失败：…」且不崩，可从错误态切回。「显示全部」输出 / 改动全文（`read_content`）及截断思考 / 参数展开（`ThinkingBlock.full_ref` / `ToolCallBlock.args_ref`）同样走 worker（`jsonl` / `file` / `sqlite` 引用按需懒读）。
- **渲染策略**：主区**单个 `tk.Text` 文档流**，不用「一行一控件」。每轮 `tn:<n>` tag；行级跳转点用 `rw:<key>` **mark**，可点击片段用 `a:<n>` mark + 统一 `act` tag（点击时 `mark_previous` 回溯最近的 `a:` mark）——**不要给每行 / 每个动作建 tag**：几万个 tag 的 `tag_delete` 实测要数秒（收起全部 28s），改 mark 后同样场景 < 0.5s；`tn:` tag 不删、跨会话复用。轮次默认折叠，展开 / 重排只重绘该轮（删 `tn` 范围 + 在 `cur` mark 处重插）。首次渲染按「40ms / 4 万字符」预算分批 `after(1)`（勿 `after_idle`），状态栏显示「已加载 x / y 轮」。分轮线 = 一行带删除线的空格（`overstrike`，1px）。
- **提问气泡、排版与浏览器级选区**：
  - ①气泡 = 纯 tag，不嵌 widget（原因见下）。每行 = 无底色前导空格 + 带底色正文（补空格对齐成实心矩形）+ 无底色换行，整行 `justify=right`；按像素宽预折行（`wrap_text`，宽度变化 > 16px 时整篇分批重排）；
  - ②**中日韩行首禁则（Kinsoku）**：`wrap_text`、`_wrap_lines`、`_wrap_md_segments_rich` 均遵守行首禁则（收尾标点 `，。、；：？！）】》」…` 不落行首，开括号不留行尾）；
  - ③**紧贴字形选区（`sel_vis`）与纯净复制（`_clean_selected_text`）**：所有缩进、右侧补白、分轮线、表格框线及 Diff 行号/`+/-` 符号均打 `ui_chrome` tag；原生 `sel` 设为透明，由 `<<Selection>>` 动态计算 `sel - ui_chrome` 交集渲染 `sel_vis` 高亮，拦截 `<<Copy>>`/`Ctrl+C` 自动剥除 `ui_chrome` 与行尾补白，确保选区无左侧锯齿黑块且复制出纯净可运行代码。
  - **教训**：最初用 `window_create` 嵌 `tk.Label` 做气泡，在已建 `TcerGui` 的进程里，Text 尚未排版（宽度几像素）时嵌入窗口，或「先销毁 widget 再删文本」，会让 Tk 8.6.15 绘制时访问违例直接崩进程。要嵌 widget 必须先 `update_idletasks()` 并「先删文本、后销毁」；能用 tag 就别嵌。
- **执行过程、逐轮遥测胶囊与代码块**：
  - ①助手轮头部右侧内嵌**逐轮遥测胶囊**（`模型短名 · tok ($cost) · +added −removed`；满足本轮成本 ≥ 会话均值的 2.2 倍且 ≥ $0.25 或本轮 Token ≥ 2.5 倍且 ≥ 80k 时标紫色 `▲` 热点高亮；点击胶囊在底部状态栏展示 `输入 / 输出 / 缓存读 / 缓存写` 明细）；
  - ②执行过程折叠态：摘要行只列「N 步 · 命令 · 改动 · F 个失败（红）· 耗时」，无整行色条；钉出的失败步骤（≤5）**只露标题行、不自动展开输出**（`core/transcript._step_rows`：`failure and not pinned`），超出的以「… 还有 N 个失败，展开执行过程查看」单列一行；
  - ③围栏代码块支持零依赖语法着色（`syn_kw`/`syn_str`/`syn_cmt`/`syn_fn`/`syn_num`）与 >80 行自动折叠预览；Diff 头部提供一键「复制新代码」（`extract_diff_new_code`）与「复制 Diff」；行内代码若对应本地真实存在的路径则高亮为可点击链接（`md_code_path`），左键打开定位、右键弹资源管理器菜单。
- **表格**：`transcript_md.layout_table(rows, max_width, measure=…)` 按**像素**量宽排列宽（GUI 传粗体 `Font.measure`），总宽超可用宽时从最宽列起收缩、单元格折行**不截断**；支持单元格内行内代码胶囊（`md_code`）与状态符号语义着色（`✓`/`⚠`/`✗`）；每表专属 tab stop tag 对齐，仅表头下方绘 1px 细分隔线、数据行通过斑马纹（`md_tbl_even`）区分，`wrap="none"`。
- **目录与右侧语义概览标尺（`_Outline` + `_MarkerRail`）**：
  - ①`_Outline`（Canvas 自绘）：文字自折行（`_wrap_lines`）、整卡零死角命中（`find_withtag("current")` 反查 `card:<idx>`），点击卡片后锁定选中态 650ms 防滚动回写跳变；卡片步骤行展示 `+added −removed` 代码量徽标及 `◆`（紫色成本热点）/ `◈`（青色跨包切换）标记；
  - ②`_MarkerRail`（滚动条右侧 10px 6 色语义概览标尺）：绿色=代码改动、紫色=高成本/高 Token 热点轮、青色=工作目录模块切换、蓝色=深度长链轮（≥15 步）、金色=搜索命中、红色=会话中断/系统级异常（刻意排除高频普通单步工具报错，避免满屏红点失去信噪比），悬停显示轮次摘要、点击直达对应轮。
- **改动视图（`VIEW_CHANGES`）**：顶部渲染交互式「涉及文件汇总」卡片（聚合全会话被改文件、轮次轨迹、总增删行，编辑 ≥3 次标橙色 `返工 N 次` 徽标；左键跳首处改动并自动展开 Diff，右键弹路径菜单）。
- **查找**（Ctrl+F，输入防抖 150ms）：命中数 / 当前命中取自数据层 `find_search_hits`，渲染层再用 `Text.search` 给已入文部分打 `find_hit` 高亮、当前命中 `find_cur`；命中很多时（> 60 个轮）只自动展开当前命中所在轮（`_eff_search` 过滤集合），其余轮跳到时再展开。Enter / Shift+Enter 上下一个，Esc 先关查找条再关窗。
- **其他交互**：视图 全部 / 对话 / 改动（`RoundedPill`）；页头右侧工具栏精简去重为「显示选项」菜单（含展开全部过程、显示注入上下文、展开/收起全部、导出 Markdown）与「查找」图标；异常轮次按钮支持循环跳转并僅对目标行单行柔和闪烁（`err_flash`）；行内「复制 / 复制回复」就地变绿闪烁 `✓ 已复制`（`act_ok`）；`j` / `k` 跳轮；「最新」浮钮；「复制恢复命令」+ 下拉「进入目录并恢复」。偏好写进控制器共用的 `_ui_prefs`。
- **与 cc-switch 的差异**：无虚拟列表（单 Text + 折叠 + 分批，实测 1500 轮 / 4 万行可用）；气泡与头像不用逐源品牌色（气泡统一中性灰 `BUBBLE_BG`=`CONTROL_BG`，来源只用 `source_icon`）；气泡无圆角 / 悬停复制按钮（右键菜单代替）；步骤详情的「参数」不折叠（只对 MCP / 其他 / 子代理 / 失败无结果的步骤显示）；Markdown 为自写轻量版（标题 / 列表 / 引用 / 围栏 / 表格 / 行内码 / 粗斜体 / 链接，表格按像素 tab stop 对齐、> 64KB 退化纯文本）；不做图片预览、不做「在终端中恢复」/ 删除会话。
- **验收**：`tests/test_transcript_view.py`（Markdown 纯函数 + 完整 `TcerGui` 入口级：右键菜单回调链 → worker → 轮询 → 分批渲染，覆盖目录条数 / 查找 / 切视图 / 导出 / 「显示全部」/ 跳轮 / 取消与复用窗口 / 加载失败）。

## 相图 PhasePortraitWidget（原 #39 ③）

<a id="phase-portrait"></a>

#### ③**GUI 渲染与交互（`gui/views.py` `PhasePortraitWidget`）**
  - *双层顶栏与专业二维相图交互系统（Canvas Interaction Suite）*：第一行标题 + 视图模式切换（`[时序流形] [相速度极限环]` 双态切换）+ 交互操控控制台（`[－] [100%] [＋] [复位]`）+ 右侧四能力胶囊条；第二行 4 个物理遥测徽标（终态定性、改动稳定性 λ、推进阻尼比 ζ、卡诺效率 η、多智能体协同），彻底去除硬编码方括号与第一行统一标签风格，全面禁用 emoji；
  - *极速平滑缩放与漫游（Cursor-Centric Zoom & Full-Range Pan）*：
    - ①严格以鼠标光标所在视口相对位置为锚点进行无偏缩放（`new_pan = (focus - pad) - (focus - pad - pan) * ratio`），彻底消除光标漂移与中心缩放假象；
    - ②**左下狄拉克目标与全域平移**：左下角高亮狄拉克目标点（Ds=0.05, 0.88）与收敛走廊、X 轴底座标注（`0.0 (契合真实意图)` / `1.0 (严重偏离意图)`）严格保留原样，轨迹垂直映射严格沿用原公式 `(pad_t + pan_y + plot_h_eff) - frac * plot_h_eff * 0.82 - 8 * zoom` 并解除旧代码 `max_pan_y=40` 的限制（全域释放为 `min_pan_y = plot_h*(1-zoom)-80, max_pan_y = 80`），放大后顶部终态与底部初始态均可自由平移对焦；
    - ③**毫秒级矢量加速（零 PIL 损耗）**：相图全面采用原生矢量流线绘制（`_aa_layer_flat` + 16ms 滚轮事件节流），单帧耗时 <1ms，彻底摒弃 200~300ms 的超大图 LANCZOS 重采样卡顿，滚轮缩放与拖拽漫游满帧 60 FPS 丝滑顺畅；
    - ④双击画布空白处或单击「复位」一键平滑归位 100%；
  - *质点巡航探针与时间线逐帧步进（Node Inspector & Timeline Scrubber）*：单击任意质点锁定对焦，触发高亮双环发光十字准星，画布左下角浮现常驻 HUD 探针卡片（展示该轮 Ds 变化、相速度、SLDS 相态、认知负债 Ed、势能 V、动力学事件与详细复盘解读），配合 `[< 上轮]` `[下轮 >]` `[居中]` `[X]` 纯文本按钮及键盘方向键 `←` / `→`，实现像录像带回放般的逐轮因果巡航，镜头自动平滑对焦跟随；
  - *物理势能与收敛几何渲染*：6 条马鞍面 Waddington 连续等高线（`theme.PHASE_CONTOUR_LOW/MID/HIGH`，随缩放动态微积分映射）+ 理想向心收敛走廊参考线 + 鞍点势垒金色虚线（`PHASE_BARRIER_CREST`）与突破胶囊 + 止损红线与纠偏胶囊 + 底座 10px SLDS 四相态彩色光谱流带；
  - *质点与因果图元彻底除噪*：高认知负债双层橙红警示光环（`DEBT_GLOW_DANGER`），低负债青绿微光（`DEBT_GLOW_SAFE`）；水床扰动以 16 段二次贝塞尔自适应起拱抛物线虚线架设，拱顶镶嵌胶囊；人类冲量引导线（`U4 关键纠偏`）严格收敛至全图至多 2 条最具向心突破性的关键转折点（杜绝数十条灰线与文本穿透主轨迹），常规需求轮次完全收拢至交互 Tooltip；垂直陡峭段（|dy|>1.3|dx|）文字自适应左右错开；全图文本遵循 2D 欧氏距离防叠（$dx^2 + dy^2 < 38^2$）；
  - *全视口 Hover 补全与说人话*：全图所有图元（势垒脊线与胶囊、止损红线、水床胶囊与弧线、底座相态带、人类指令引线、状态徽标、质点）均绑定自然工程语言 Tooltip，以 ` · ` 统一层级分割，杜绝 `xxx(yyy)` 括号噪音。

## 术语库工作台 TermbaseView 与 GUI 踩坑（原 #40 ⑤⑥）

<a id="termbase-gui"></a>

#### ⑤**GUI 架构与工作台集成（`gui/views.py`, `gui/popups.py`, `gui/app.py`）**
  - *第 7 页签工作台 `TermbaseView`*：活动栏顶部注册 `ui-book` 专属极细矢量图标（`ui-book.png` 16×16 与 `ui-book-22.png` 22×22，见 `scripts/build_icons.py`）；左栏：工具栏（新建、删除、反向查询、歧义探测、术语考古、导入片段、导出 JSON/Markdown、切换库路径）+ 状态胶囊筛选（全部/正式/草稿/废弃）+ 搜索框 + `Card` 列表（State rail 着色：active 青蓝/draft 橙黄/deprecated 灰）+ 空库时贴心引导；右栏：全字段表单编辑器（Slug 实时正则校验、MDA 下拉、负责人、同义词、权威释义、renderings 跨职能网格、misconceptions 动态增删列表、显式保存校验提示）；
  - *会话列表右键直达*：`SessionColumn` 右键菜单接入「术语考古」命令（右键已作用于会话卡片，刻意不带「（本会话）」后缀），支持从主工作台一键对当前选中会话发起历史概念挖掘；
  - *报告收信箱归属*：歧义检测与考古报告一律归入第 6 页签「LLM 报告」（`REPORT_KINDS` 扩展 `terms` 与 `ambiguity` 类别胶囊，卡片边缘专属 State Rail 着色区分）；
  - *弹窗套件*：`TermLookupPopup`（F3 反向查询）、`AmbiguityDetectPopup`（F4 歧义探测）、`TermImportPopup`（JSON 词条片段语法校验与合并）。

<a id="termbase-gui-pitfalls"></a>

#### ⑥**GUI 踩坑与工程教训（血泪经验）**
  - `ScrollFrame` 容器陷阱：`ScrollFrame(parent, bg)` 内部已将 `canvas` 和 `vbar` pack 入父容器，外部**绝不能**对其实例调用 `pack()`（非标准 Tk 控件会抛 AttributeError），内部子控件必须统一挂载在 `self.inner`；
  - 主题色彩与字体 SSOT（`theme.py`）：文字色仅有 `FG`、`FG_WHITE`、`MUTED`，输入框底色使用 `CONTROL_BG`（不存在 `ENTRY_BG` 或 `FG_DIM`）；字体仅有 `FONT_HEADING`、`FONT_UI_BOLD`、`FONT_MONO`（不存在 `FONT_SUBHEADING` 或 `FONT_CODE`）；
  - `RoundedPill.config` 参数陷阱：切换选中态时仅支持 `bg` 与 `fg` 参数，传 `fill` 会抛 `TclError`；
  - 弹窗销毁口径：所有自定义弹窗类内部窗体为 `self._win`，关闭销毁调用 `self._win.destroy()`；
  - **表单枚举与文案纪律（2026-09-22 用户打回后补）**：
    - ①下拉一律 `ttk.Combobox(state="readonly")`（theme 已有 TCombobox 深色化 + app.py option db 处理下拉列表；`tk.OptionMenu` 是白边系统 chrome，同 FlatMenu 禁用理由）——曾整版用 OptionMenu 被打回；
    - ②**界面文案绝不暴露字段名/英文枚举/内部代号**（slug、pref_label、`(active)`、`(mechanics)`、F2/F3/F4、D3 探针、MDA 语义通道、renderings/misconceptions——用户「看不懂没法用」），枚举显示纯中文、术语锚点进 Tooltip；
    - ③显示值↔key 的转换走模块级双向映射表 SSOT（`_STATUS_CN`/`_STATUS_CN_KEY`、`_MDA_CN`/`_MDA_CN_KEY`、role 用 `ROLE_LABELS` 反转），禁止 `split("(")` 解析显示串——显示文案一改解析即碎；
    - ④输入框底色 `CONTROL_BG`（不是 PANEL_2）、小节标题色 `FG_WHITE`（彩色标题违反 VS Code 纪律）、间距从 `theme.PAD_*` 取值、行删除用 `ui-trash` 图标不用「✕」文本符号。

## 补充：旧 AGENTS.md 独有的 GUI 约定

> 以下条目来自旧 AGENTS.md（CLAUDE.md 的较早分叉，2026-10 已改为短索引文件）。其中 CLAUDE.md 原本没有的内容迁入此处；与 CLAUDE.md 重复的条目以后者为准，不再重复收录。

- **子窗口规范**（旧 AGENTS #24 ⑭）：所有弹窗、设置与详情统一经 `widgets.new_window` 创建为原生居中 `Toplevel` 子窗口（暗色标题栏，Esc 键关闭），不占用主工作台页签栏；主编辑区仅保留基础核心视图常驻页签（页签个数以 `app.py` 为准，旧文写 6 个，现已增至 7 个含术语库）。
- **排名页项目/会话双视角**（旧 AGENTS #25）：由 `view_mode` 驱动（对齐指标分类/模型对比）：项目视角右栏展现排名概览 + 项目洞察，会话视角展现选中会话的构成拆解 + 会话洞察；用户点排名行 → `on_ranking_select` 翻转 `view_mode`、同步各页签与后缀（`set_view_mode` 程序化选中用 `_suppress_select` 防重入）。
- **洞察区块渲染**：`ScoreRankingView._build_insights_section` 按 亮点/拖累项/快速改进 分组，共享 `_render_insight_items`；护栏 `tests/test_insights.py` + `test_gui_smoke.py::test_ranking_insights_section_renders`。

## 历史与教训（GUI）

- **高 DPI/聚焦环这类问题必须像素级实测**：Windows 经典 `tk.Button` 的蚂蚁线由系统 `DrawFocusRect` 绘制，`highlightthickness=0` 挡不住，只有 `takefocus=0`；`style.map` 优先级高于 `configure`。凭直觉修会反复被用户打回。
- **不要用 `after_idle` 分批**：`update_idletasks` 会把 idle 批同步全跑完；逐实例 `update_idletasks` 曾让效率榜刷新卡 2s，改为合并结算 + 单定时器。
- **跨线程不要裸 `root.after`**：无 mainloop 时抛 `RuntimeError`（不是 `TclError`），一律走 `queue.Queue` + 主线程 60ms 轮询；失败路径必须清任务表并给状态栏红字，有回归测试钉住。
- **点击绑定要覆盖子控件**：Label 遮挡容器，只绑 Frame 点文字无反应。
- **`ScrollFrame` 不能被外部 `pack()`**，子控件挂 `self.inner`；`RoundedPill.config` 只认 `bg`/`fg`；弹窗内部窗体是 `self._win`；下拉一律 `ttk.Combobox(state="readonly")`，菜单一律 `FlatMenu`。
- **界面文案绝不暴露字段名/英文枚举/内部代号**，枚举↔显示值走模块级双向映射表，禁止 `split("(")` 解析显示串。
- **`messagebox` 必须传 `parent=`**，否则确认框关闭后弹窗被主窗盖住（用户感知=弹窗消失）。
- **组件级测试全绿 ≠ 入口级可用**：新增 GUI 功能验收必须构建完整 `TcerGui` 走真实入口（见 llm-layer.md#delivery-lessons）。
- **`selection_set` 的 `<<TreeviewSelect>>` 依赖事件循环**，程序化选择后须显式调 `_on_select()`。
- **抗锯齿 `_aa_layer` 的 16 档 alpha 量化勿删**（107ms → 23ms/层）；相图缩放用 `_aa_layer_flat` 矢量绘制 + 16ms 节流。
- **`cget()` 返回值进算术/比较前必须经 `str()` / `to_int()`（`widgets.to_int`）归一**：CI 打包版（GitHub Actions 的 Python 3.11 冻结 tkinter）下部分选项返回 `Tcl_Obj` 包装而非 int/str，`int()` 直接 TypeError、`.split()`/`in`/`==` 静默失配——v2.0.0 exe 因此启动即崩（11 处调用点，2026-10-23）。源码 3.14 运行不复现，`winfo_*` 系列两环境均为 int 无需包装（1.9.9 同源 CI 构建大量 winfo 算术未炸可证）。回归测试用 `_FakeTclObj`（无 `__int__`、仅 `__str__`）模拟该形态，无需装 3.11。**发版前务必本机跑一次 CI 产出的 exe**——源码 launch.bat 冒烟覆盖不了冻结 Tcl/Tk 的行为差异。
- **继承 `tk.Widget` / `tk.Canvas` 时的命名冲突铁律（2026-10 踩坑）**：
  - ① `tkinter.BaseWidget._w` 存放底层 Tcl 控件路径名（如 `".!frame.!canvas"`），`tkinter.Misc._options` 是内部选项构造器方法——自定义子类**严禁**使用 `self._w` 或 `self._options` 作为宽高或选项列表属性名（应命名为 `self._width_px` / `self._items`），否则在 `super().__init__` 或后续 `tk.call` 时会直接触发 `TclError: invalid command name` 或 `TypeError: 'list' object is not callable`；
  - ② 需要代理 `tk.Entry` 的 `.delete(first, last)` 与 `.insert(index, s)` 接口的复合输入控件（如 `RoundedEntry`）**必须继承 `tk.Frame`**（内部持 `self._canvas = tk.Canvas(self)`），绝不能直接继承 `tk.Canvas`——否则 `draw_rounded_box` 内部调 `canvas.delete(tag)` 会被子类重写的 `Entry.delete` 拦截并抛出 `TclError: bad entry index`。
- **Tcl/Tk 8.6 与 9.0.4 双轨兼容**：CPython 3.14.7+ 官方安装包与 CI 构建产物升级为 Tcl/Tk 9.0.4（原生 `<TouchpadScroll>` 高精度触控板事件与原生 SVG `tk.PhotoImage`），而本地 Python 3.11–3.14.6 仍可能是 Tcl/Tk 8.6.15；`platform.bind_mousewheel` 按 `tk.TkVersion >= 9.0` 动态加绑 `<TouchpadScroll>`，`widgets.get_rounded_rect_img` 通过三层管线（Tk 9.0 SVG → PIL → 纯标准库 SDF PNG）保证双端零依赖一致渲染。
- **重写 `configure`/`cget` 必须保住 Tk 四种调用形契约**（RoundedFrame / StatusBadge / RoundedKpiChip 经 `widgets._opt_info`/`_config_dict` 统一实现，2026-10 补）：①单键查询 `configure("bg")` 返回 `(key, dbName, dbClass, default, current)` 5 元组（消费方经 `[4]` 取值，裸字符串直接 TypeError）；②dict 形 `configure({"bg": x})` 必须生效（禁止静默丢弃或漏拦虚拟选项）；③无参 `configure()` 返回选项枚举 dict；④`cget("bg")` 返回**构造时父底色**（稳定值）——悬停填充、KINDS 胶囊面这类「画出来的颜色」不是画布 bg，返回它们会误导读父底色的调用方。
- **`SurfaceCard` 不偷换 bg**：曾有 `bg==BG → SURFACE_CARD` 的静默重写特判，调用方以为画 BG 底被换色——特判已删，全部调用点显式传 `bg=theme.SURFACE_CARD, parent_bg=theme.BG`，勿再加回任何参数偷换。
- **Variable trace 必须随 widget 销毁解除**：`ToggleSwitch`/`SegmentedControl`/`RoundedEntry` 的 `trace_add` 在 `<Destroy>` 里 `trace_remove`（各组件 `_release_var_trace`，判 `e.widget is self` 防「<Destroy> 对后代销毁逐个冒泡」误触发）。tk Variable 以 `PY_VARn` 活在解释器级，不解除则闭包把整棵已销毁控件子树钉在内存里，弹窗每开一次漏一棵。
- **`<TouchpadScroll>` 解码勿臆测打包方向**：本地 Tk 8.6 无法实证该虚拟事件的 %d 格式，`platform.bind_mousewheel` 按「官方 API 优先」三级回退：`tk::PreciseScrollDeltas`（Tcl 层字符串脚本绑定 + `%D` 替换，gitk 同款）→ %d 多值列表 → 裸 32 位按 WM_MOUSEWHEEL 惯例取**高** 16 位（曾错取低 16 位，垂直滚动会落在水平分量上）。解码纯函数 `touchpad_dy_from_raw` 有离线单测；真机触控板首验仍要等 Tk 9.0.4 打包版。

<a id="unified-dialog"></a>
### 统一模态对话框与次级窗口收敛（2026-10 大手术第一刀）

- **禁新增 `tkinter.messagebox`**：系统样式与 Monokai Dimmed 主题割裂。全部提示/确认走 `widgets.message_dialog` 及其薄壳 `show_info / show_warning / show_error / ask_yes_no / ask_yes_no_cancel`——样式对齐 ConfirmDeletePopup（语义徽标页头 + SurfaceCard 正文 + 右对齐按钮行），`grab_set + wait_window` 模态阻塞（与 messagebox 语义一致）；`ask_yes_no(danger=True)` 红色警示主按钮（不可恢复删除类）；长文本（traceback/导入清单）传 `scroll=True`。
- **测试 stub 必须 patch 使用方模块的名字**：对话框函数经 `from .widgets import ...` 已在各模块导入时定型为名字绑定，`monkeypatch.setattr(widgets, "ask_yes_no", ...)` 无效——须逐模块 setattr（`test_termbase_gui._msg_box` 是标准写法：遍历 app/popups/popups_analysis/transcript_view/views）。测试环境 `wait_window` 会真阻塞，漏 stub 即整套挂死（曾以 500s timeout 才定位）。
- **已移除次级窗口（功能收敛）**：`SessionDetailPopup`（会话详情）、`UserMsgsPopup`（用户消息）、`RadarPopup`（效率雷达）及其全部入口（会话卡双击、右键「查看详情/查看用户消息/查看效率雷达」、趋势图双击/右键雷达项）。看板「用户消息」格后续亦整体移出 SSOT（见第三刀）。
- **项目右键菜单收敛**：仅「在资源管理器中打开 + 复制项目路径」两项（「刷新此项目」「复制项目名称」已移除；点当前项目卡即刷新）。
- **第三刀（2026-10 续）**：①「用户消息」格移出 SSOT（RealProjects 列名改字面量）；②SessionComparePopup 叠加雷达删除（`RADAR_AXES`/`radar_resolve_axes`/`radar_normalize` 一并删除，逐回合 Token 曲线保留）；③「设置与工具」提纯为「设置」（仅 LLM 设置/检查更新/自启检查），六个分析工具迁至状态栏版本号旁「分析 ▾」菜单（`StatusBar._open_analysis_menu` + `FilterBar._build_analysis_menu`）；④BaselinesPopup/AdvancedPopup（跳过 LOC 开关）及整条个人基准校准链路删除（`_no_loc` 恒 False，核心参数保留）；⑤FilterBar 任务类型选择器与设置菜单任务单选移除（`get_params` 恒回 AUTO，状态栏「自动任务类型」后缀删除）。- **会话卡片去色（2026-10，C 方案）**：日期圆点与左侧 rail 原按综合效率分评级变色（绿=优秀/良好、橙=待改进/低效、灰=中等），评分体系移除后颜色失去可解释性——整体移除，视觉状态只保留选中高亮。会话卡当前刻意「无状态着色」；若将来加颜色，必须是可解释的客观信号（如中断 abort_reasons）且先在此处登记约定。- **第四刀（2026-10 续）**：看板头部两枚上下文胶囊（`_tag_time` 时间/会话数、`_tag_meta` 来源/分支/子代理/推理）移除——项目视角与视角无关的冗余信息，同数据在会话卡与状态栏已有。看板头部现为：标题·回合 + 耗时 + 会话记录按钮（左）‖ 简要版/完整版切换（右）。
