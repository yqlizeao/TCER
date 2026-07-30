# TCER — Token-to-Code Efficiency Ratio

## 项目目标

基于真实 AI 编程助手会话数据，构建多维 AI 编程效率计量体系（TCER/CTEI）。支持五个数据来源：Claude Code（`~/.claude`）、Codex（`~/.codex`）、OpenCode（`~/.local/share/opencode`）、Grok / grok build CLI（`~/.grok`）、Oh My Pi / omp（`~/.omp`）。

- **GUI-only**：`python -m tcer` 启动桌面界面
- **纯离线**：不依赖 git、不做联网操作，数据来自本地 JSONL / SQLite 文件
- **零依赖**：纯 Python ≥3.11 标准库

## 快速开始

``bash
python -m tcer            # 启动 GUI
python -m pytest tests/   # 运行测试
python -m tcer.audit      # 闭环审计：真实本地会话 vs 原始 JSONL 重算
``

### 闭环审计（开发必用）

改 `reader` / `analyze` / `loc` / 计价后，用真实 `~/.claude` 等数据验证，避免脑测：

``bash
python -m tcer.audit --list
python -m tcer.audit --source claude --project TCER --top 5 -v
python -m tcer.audit --source all --project TCER --json audit-out.json
python -m tcer.audit --all-projects --top 2 --no-loc   # 批跑全部项目（可加 --source）
python -m tcer.audit --all-projects --skip-empty --top 1 -q --summary-json -
python -m tcer.audit --ci --summary-json audit-summary.json   # 等价 CI 预设
# 退出码 0/1；cost_sum_per_model 校验逐模型成本加总
``

库入口：`tcer.core.audit.audit_project` / `audit_ref` / `summarize`（pytest 也可调）。检查项含：会话 Token 与文件重算一致、子代理折叠、LOC/unseen_writes、聚合 Token 求和、聚合禁用 CTEI、Grok 无裸 `grep` 工具名、`cost_usd` 不崩。`paths.project_has_sessions` 统一判断空项目（GUI 置灰 + audit `--skip-empty`）。

## 仓库结构

``
TCER/
├── tcer/                  Python 包
│   ├── core/              核心库（reader / loc / metrics / pricing / models / paths / analyze / export / format / audit …）
│   ├── gui/               GUI（app / theme / metric_defs / widgets / views / charts / popups / popups_analysis / html_report）
│   └── config/            配置（model_pricing.json / composite_baselines.json）
├── tests/                 测试（`python -m pytest tests/`）
└── doc/                   详细文档
    ├── metrics.md         指标公式与计算步骤
    ├── data-format.md     JSONL 数据格式与 LOC 原理
    └── architecture.md    MVC 架构与工程规范
``

## 指标分类（6 组 · 以 `metric_defs.GROUPS` 为准）

GUI 指标按关注维度分为 6 组（扁平，无层级关系）。数量以代码 SSOT 为准：

| 组 | 名称 | 内容 |
|---|------|------|
| G1 | 会话概况 | 元数据（时长、模型、回合、工具调用、用户消息等） |
| G2 | Token 用量 | 原始消耗（输入/输出/缓存/推理/峰值输入/窗口使用率） |
| G3 | 缓存效率 | 缓存利用率比率 |
| G4 | 代码产出与质量 | LOC、返工率、工具行为比率、搜索后编辑比等 |
| G5 | 成本分析 | 金钱代价 |
| G6 | 综合评分 | 效率指标 + CTEI 评分 + 基准参数 |

**字体颜色**：白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考。

> 指标公式、计算步骤、算例：[doc/metrics.md](doc/metrics.md)

## 指标唯一真理源（SSOT）

`tcer/gui/metric_defs.py` 是**所有指标展示的唯一真理源**——名称 / 提示 / 单位 / 格式 / 好坏方向 / 取值，4 个页签（指标分类 / 排名 / 趋势 / 模型对比）与全部 popups（雷达等）都从这里取，禁止各处再自管：

- **会话/项目级**：`raw_value(report, key)`（图表用数值）、`format_value(key, native)`（显示串）、`display(report, key)`、`report_values(report)`。每个 `Metric` 带 `fmt` 规格（`int`/`pct`/`float:N`/`money`/`text`）。
- **逐模型**：`MODEL_GROUPS` + `model_raw` / `model_display` / `model_tip`，同义指标转调 `format_value` 与会话级逐字节一致；**例外**：模型对比是 N 列并排，Token 数等大数量级用 K/M 紧凑显示（布局需要），比率/百分比仍与网格一致。
- **CTEI 因子分解**：`CTEI_FACTORS`（名称/公式）。
- **评级体系**：`core/metrics.GRADE_BANDS`（名称+阈值，best→worst）是 grade 的唯一源；`grade()`、排名分布条、趋势 CTEI 带都从它派生。
- **源能力感知**：`metric_defs._SOURCE_SUPPORT` 标注「数据源根本不产生该字段」的指标（如 Claude 无独立推理 token、Codex 无缓存写入、上下文窗口/TTFT/限流为 Codex 独有）。`display()` 返回「不适用」、`raw_value()` 返回 None，网格/图表/HTML 报告自动继承；「不适用」与「-」一样参与空单元格折叠，但颜色置灰。部分支持（如 OpenCode 多回合 peak 留 0）**不**标注，仍显示 "-"。
- **HTML 报告**：`gui/html_report.py`（无 Tk 依赖，可无头测试）渲染项目级/会话级自包含单文件 HTML，数值全部走 `metric_defs.display`，与网格逐字节一致；GUI 导出菜单含项目/会话两组入口。图表组件在 `gui/charts.py`（趋势/散点/仪表板/时段热力图，从 views 拆出，views re-export 保持旧 import 路径）。


## 工程规范

1. **禁止中间产物**：用完即删，不提交。经验写入本文件。
2. **GUI 全中文**：界面完整中文，仅 TCER 保留英文缩写。代码用缩写。
3. **纯离线**：GUI 不暴露任何需要 git 或网络的功能。**例外**：上传功能（`upload_client`/`upload_prefs`）是用户显式 opt-in 的网络行为，未配置时零联网。
4. **库层不动**：`tcer/core/` 有完整测试覆盖，改动需谨慎。
5. **运行方式**：`python -m tcer`（绿色免安装）。

## 关键注意事项

1. **按 message.id 去重**：一次 API 响应被拆成多行写入 JSONL，每行重复携带 usage。必须按 id 只计一次（实测 55.9% 重复计数）。**边界**：空字符串 `""` 视为无 id，逐条计数。**ccswitch 兼容**：mimo 消息第一行是 thinking 桩（usage=0），第二行才有真实 usage；零 usage 行会释放 id 锁（`seen.discard`），允许后续行贡献真实 token。**Grok 差异**：grok build 每 turn 恰好一条 `turn_completed` 携带权威 usage，无多行重复问题，直接累加；错误回合的空 usage（字段为 null）计入 `empty_usage_skipped`，不虚增回合数。工具名映射含 `grep_search`→Grep，并兼容实机短名 `grep`（否则探索比漏计）。**Codex 差异**：`token_count` 事件会重复投递相同的 `last_token_usage`（实测虚增 1.5–2.5%），必须对单调递增的 `total_token_usage` 做差分，`last` 仅作无 total 时的回退。**Grok user chunk**：同一条用户消息可能拆成相邻多条 `user_message_chunk`（或重复投递），相邻连续的 user chunk 只计 1 条 user_msgs，任何其他更新（`turn_completed`/`retry_state` 等）终止合并。**OpenCode peak**：session 聚合计数是多步总和，仅单回合会话可作 peak_input 近似；多回合且无 `step-finish` 快照时 peak 留 0（ratio 显示 None，勿用总和虚增窗口占用）。详见 [doc/data-format.md](doc/data-format.md)。
2. **LOC 不依赖 git、不读真实仓库**：净增代码只来自会话内工具调用回放。首次 Write 假定 old=0 并 `unseen_writes++`（F1 暴露）；Claude 的 `toolUseResult.originalFile` 到达后按会话数据回溯修正真实原行数（`note_write_original`，scan 与 session_loc_full 两路一致）。**产品边界：磁盘先验（disk_prior）、tree_loc 全树扫描、git 校准均已按「纯离线仅析会话数据」定位移除**；会话日志里记录的 git 分支/提交等元数据属会话数据，保留。Grok `write`/`search_replace` 走同一 `_LocAccumulator`。**产出文件分类**（`loc.py`）：闸门 `_is_code` = 代码(`CODE_SUFFIXES` 纯源码) ∪ 文本(`TEXT_SUFFIXES`=.md/.txt/.rtf/.rst/.org/.adoc/.tex/.csv，**策划文档也算产出**) ∪ 配置(`CONFIG_SUFFIXES`=.json/.yaml/.toml)，三类都计入 net_loc/TCER；`_is_doc_file` 把散文类文本(.md/.txt/.rst/…，**不含 .csv** 数据)单列记入「文档行」。**Office 二进制(.docx/.xlsx/.pptx)刻意排除**——AI 的 Write/Edit 是文本行模型，无法对二进制产生行增量（策划通常 .md/.txt 起草后自行转 Office）。Codex `_loc_scan` 用共享 `_is_test_file`/`_is_doc_file`（曾硬编码 .md，致 .txt 漏归文档行）。
3. **逐模型计价**：TokenUsage.per_model 按 message.model 分桶，混用多模型会话也精确。价表 `tcer/config/model_pricing.json`（≈175 模型）。**四级匹配**（`pricing._match_id`，按优先级）：①精确 ②归一化精确（小写、去 `-`/`_`、`5p2`→`5.2` 且 `5-6`→`5.6`，先于前缀以防 `glm-5p2` 误中 `glm-5`、`gpt-5-6-sol` 误中 `gpt-5`）③前缀（`claude-opus-4-8[1m]`→`claude-opus-4-8`）④反向前缀（短名 `claude-opus-4-6`→带日期 key）。每条先试原 id 再试末段 path（剥 `z-ai/`、`accounts/fireworks/models/` 等供应商前缀）。`pricing.normalize()` 把 per_model key 归一化到价表规范 key；`pricing.table_key()` 返回 None 即走 default 回退（GUI 价表浮窗据此标"默认配置价"）。匹配候选含 path 尾段与 **mode 后缀剥离**（`-thinking`/`-reasoner`，如 `claude-opus-4-6-thinking`→`claude-opus-4-6-20260206`；不剥 `-high`/`-reasoning` 以免误绑）。`pricing.unmatched_models` / `metrics.unmatched_pricing_models` 列出回退模型；状态栏与模型/成本弹窗提示。**Grok**：`turn_completed.usage.modelUsage` 同样按模型分桶（如 `grok-4.5`）；工具名优先 `x.ai/tool`，否则 `rawInput.variant` / `kind` / title（后端 WebSearch 无 tool 名时归 WebSearch）。
4. **过滤 `<synthetic>`**：ccswitch 在 429 限流或系统占位时注入伪 assistant 消息，`model` 字段为 `<synthetic>`，usage 全为零。reader 层直接过滤，不计入 `models` 和 `per_model`。
5. **子代理并入父会话**：Token 与 LOC 保留真实成本，不单独计为 session。
6. **时序分析**：`ToolOp(turn, tool, path)` 记录每个工具调用的回合序号和文件路径。**搜索后编辑比**按回合就近匹配（搜索后 3 回合内出现 Write/Edit 即算跟进，不绑定具体文件——真实 Grep/Glob 的 `path` 多为目录）；**先读后写率**等仍用 file_path。merge 时 rebase turn 编号保证聚合后时序连续。
7. **任务类型体系**：3 大类（代码创作/代码维护/非编码），每类有 TTAF 系数。`ntcer = tcer / ttaf` 归一化后可跨任务类型公平比较。`ta_tcer` 保留为向后兼容别名。
8. **返工率 = 自返工率**：churn 只计「本会话先写入、随后又被自己删除/替换」的行（`loc.SessionLoc.rework_deleted`，封顶于本会话已写入该文件的行数）；删除会话之外的既有代码属正常编辑，不计入。
9. **CTEI 三因子（聚合有效）**：`CTEI = (TCER/基准) × (CPE基准/CPE) × (1+CHR×0.5)`。历史第四因子 NCPI（净增÷代码库行数）因需扫描真实仓库已随产品定位移除，PSAC/阶段调整同删；三因子皆可聚合，**聚合层不再禁用 CTEI/评级**（audit 改为校验聚合 CTEI 与公式重算一致）。
10. **自定义 Claude 配置目录自动识别**：用户常以 `CLAUDE_CONFIG_DIR=%USERPROFILE%\.zclaude`（或其他自定义名）启动 Claude Code 以隔离 `.claude`。该环境变量只在 Claude 进程内、TCER 读不到；故 `paths.claude_config_dirs()` 以规范目录（`CLAUDE_CONFIG_DIR` 或 `~/.claude`）为锚，扫描其**父目录**里所有结构匹配 Claude 的兄弟目录（`projects/<hash>/*.jsonl` 指纹），全部视为 Claude 根。`list_projects()` 跨所有根查找，**同 hash 跨根各成一条**（每根独立成卡，`ProjectRef.config_root` 标所属根；`discover_jsonl(hash, roots=[根])` 按根分会话，`roots=None` 默认仍跨根 union 兼容 CLI）；结果按 `(home, CLAUDE_CONFIG_DIR)` 进程级缓存——会话期间新建的自定义配置目录需重启 TCER 才会出现。**Windows**：盘符大小写导致同一根内 `C--GitHub-X` 与 `c--GitHub-X` 两文件夹时，`project_hash_key` 折叠为一项（根内 casefold 并集保留），但跨根不再合并。自定义根（如 `.claude-proxy`）的项目用 ccswitch 图标（`views.project_icon_key` 按 `paths.is_custom_claude_root` 判定）。
11. **任务类型 SSOT**：`TASK_CATEGORIES` / TTAF 只来自 `config/composite_baselines.json`（`metrics._refresh_composite_globals`）。分析入口默认 `code_creation`；`resolve_task_type` 把空值/未知/`feature` 等合法化，`coerce_task_type` 给公式层（未知→None，不静默套创作系数）。`task_type=auto`（GUI「自动」）按会话 `infer_task_type`（net_loc/探索比/Edit 比/读写比）推断，聚合取众数。个人基准默认至少 `MIN_BASELINE_SESSIONS=10` 条完整会话。
12. **Claude 单次扫描**：`reader.scan_session` 一趟 JSONL 同时产出 TokenUsage + SessionLoc；`analyze` 进程内按 path 缓存，避免 usage 与 LOC 双读。GUI `reanalyze` 用 `cancel_event` 协作取消上一次分析。
13. **high_churn 合并**：子代理折叠/项目聚合用 `loc.merge_session_locs`，按合并后的 `file_edit_counts` 重算 `high_churn_files`（同路径不重复计）。
14. **mtime 缓存**：`tcer.core.file_cache` 按 `(path, mtime_ns, size, variant)` 缓存 scan/usage（LRU，上限 512）。取消靠 `cancel_check` 在 factory 内抛异常实现，部分扫描天然不入缓存——因此 GUI 的可取消分析**同样走缓存**（勿再用 `cancel_check is None` 做缓存开关）。四源均已接入：Claude `scan_session`、Codex/Grok usage+loc、OpenCode usage+loc（SQLite 会话 key 到 db 文件，粗粒度；legacy 会话 key 到自身 JSON）。Claude 日期过滤复用 cwd-keyed 扫描，同文件不再双扫。测试可用 `file_cache.clear()`。
15. **用户消息懒加载**：分析只计 `user_msgs` 数量；Claude 与 Codex 一样弹窗/上传时再 `read_user_messages`（含 subagent 文件）。
16. **缓存写 TTL 分档计价**：Claude `usage.cache_creation.ephemeral_1h_input_tokens` 是 1h 缓存写子集（单价 2×input，5m 为 1.25×input）。`TokenUsage.cache_write_1h_tokens` 记录该子集，`metrics._cost_from` 按 `CACHE_1H_PREMIUM=0.6` 加溢价（价表 cache_write 视为 5m 率）。实测本机 60% 缓存写走 1h 档——漏掉会系统性低估成本。
17. **深度信号解析**：Claude `type:"system"` 子类型：`turn_duration.durationMs`（真实回合耗时→回填 `turn_stats`，不含用户暂停）、`api_error`（429→限流命中）、`compact_boundary`（压缩）；`usage.server_tool_use`（网页搜索/抓取）；行级 `version/gitBranch/effort/permissionMode` → SessionMeta。Grok 每会话另读 `signals.json`（窗口/TTFT/ITL/取消/评价/git 落地/回退行；`contextTokensUsed` **覆盖** peak_input——turn_completed.usage 是回合内多补全总和，按回合累计的 peak 虚高 10×+）与 `events.jsonl`（`permission_resolved.wait_ms` 审批等待）。改支持范围后同步 `metric_defs._SOURCE_SUPPORT`。
18. **逐回合时间线**：`TokenUsage.turn_stats: list[TurnStat]`（turn/ts/4 token/duration_ms/tool_calls/errors），四源填充：Claude 逐响应、Codex 逐 token_count 差分步（task_complete 回填耗时）、Grok 逐 turn_completed、OpenCode 逐 step-finish。merge 时 rebase turn（聚合对象时间线是串接非并发）。GUI「会话时间线」弹窗与会话级 HTML 报告消费。
19. **F1 修正（originalFile）**：Claude 工具结果行 `toolUseResult.originalFile` 携带 Write 前真实原文。`_LocAccumulator.pending_f1` 记录按 old=0 记账的首个 Write，`note_write_original` 回溯修正（覆写→按 new−orig 重算并撤销 unseen；确认新文件→只撤销 unseen）。`scan_session` 与 `session_loc_full` 两条路径都做同一修正（审计交叉验证要求逐字节一致）。`toolUseResult.userModified` → 人工修正计数（采纳信号）；OpenCode `session.revert` / Grok `hasReverted` → `revert_events`。
20. **Codex 工具错误识别**：exit code 有两种实测格式（`Process exited with code N` / `Exit code: N`），exit code 权威；无码时只认显式失败前缀（`execution error`/`error:`/`failed:`/traceback），禁止全文 `error` 子串匹配（误报）。
21. **SourceAdapter**：非 Claude 四源（Codex/OpenCode/Grok/omp）共用 `analyze._analyze_source_project` 骨架 + `_SourceAdapter` 钩子（resolve/sessions/read_meta/usage_of/loc_of/session_key），file_cache key 构造在钩子内（Grok 必须并入 signals.json/events.jsonl 旁路文件签名，否则会话结束后补写的信号不失效）。新增数据源 = 写一个 adapter。共享小工具在 `core/parse_util`（as_int/first_str）。
22. **护栏测试**：`tests/test_models_merge.py` 反射断言 `TokenUsage.merge` 覆盖全部字段（新增字段忘改 merge 会当场失败，容器/极值字段进 `_SPECIAL` 表）；`tests/test_gui_smoke.py` 无头构建全部图表模式与弹窗（无显示环境自动 skip）；`tests/test_export.py` 断言 `report_row_dict` 每个键必须归入 `_CSV_FIELDS` 或 `_CSV_EXCLUDED`。
23. **模型对比分摊**：`compare_models` 行为/产出/质量指标按 token 占比加权（`_weight_sum`），单模型会话权重 1.0 与旧「主模型全额归因」一致，混合会话按占比拆分不再丢弃。
24. **GUI 视觉一致性**：①Windows 高 DPI 感知在 `app._enable_windows_hidpi`（**必须在创建 Tk 之前**调用，否则整窗被位图拉伸发糊）+ `_apply_tk_scaling`；②按钮一律用 `widgets.flat_button`（`primary=True`=强调色主操作），禁止再手写 `tk.Button(... relief="flat" ...)`；③间距从 `theme.PAD_XS/S/M/L`（2/4/8/12）取值；④`ScrollFrame` 自带按需显示的深色滚动条；⑤Combobox 深色化 = ttk style + `root.option_add`（下拉列表不吃 style）双管齐下；⑥界面偏好（几何/分栏/筛选/上次项目）经 `core/ui_prefs` 持久化到 `~/.claude/tcer_ui.json`，关闭时保存、启动时恢复（`last_project` 一次性生效）。例外：UploadDialog 与删除确认弹窗的按钮保持原样（前者归上传负责人，后者红色警示是刻意的）。
25. **排名页**：CTEI 三因子化后默认即可计算，排名页默认就有数据；仅当会话无 CTEI（no_loc 或无净增行/成本）时回退按 TCER 排名并显示提示条。`launch.bat` 优先 `pyw`/`pythonw`（`start` 分离，无残留控制台；文件为 GBK 编码保 cmd 中文注释）。
26. **omp（Oh My Pi）数据格式**：会话存于 `~/.omp/agent/sessions/<dir-encoded>/<ts>_<sessionId>.jsonl`（`PI_CODING_AGENT_DIR` 重定位 agent 基目录、`PI_CONFIG_DIR` 重定位 `~/.omp` 根；`omp_sessions_dir` 解析同 omp `dirs.ts`，未复刻 Linux XDG 重定向）。JSONL 每行一个 `SessionEntry`：首行 `type:"title"`（定宽标题槽，跳过）、`type:"session"` 头（`id`/`cwd`/`title`/`timestamp` 为 **ISO-8601 字符串**，`parse_timestamp_ms` 兼容）、`type:"model_change"`（`model` 为 `"provider/modelId"`，`pricing.normalize` 经 `rsplit("/")` 剥供应商前缀）、`type:"message"`（`message.role` ∈ `user`/`assistant`/`toolResult`）。assistant 携带**每响应一个** `usage={input,output,cacheRead,cacheWrite,totalTokens,cost:{total}}`（无 Claude 多行重复，直接累加）+ `contextSnapshot.promptTokens`（peak_input）+ `duration`/`ttft`（回填 turn_stats / time_to_first_token_ms + `ttft_ms_samples`）；content 块 `thinking`/`text`/`toolCall={id,name,arguments}`；`toolResult={toolCallId,toolName,content,details,isError}`。LOC：`write` 取 `arguments.content`+`details.resolvedPath`（unseen_writes 同 Grok）；`edit`/`ast_edit` 取 `details.{oldText,newText,path}` 经同一 `_LocAccumulator`（净增行差 + 自返工）；`snapshotsPruned`（无 oldText/newText）的 edit 跳过。`custom`/`custom_message` 等非 message 行忽略。**子代理折叠**：omp 子代理会话存于主文件同名的 `<stem>/` 子目录（`..._<uuid>.jsonl` → `..._<uuid>/SubAgent.jsonl`），`_is_subagent_file` 按「父目录名 == 某 main 文件 stem」识别，`sessions_for_project`/`session_paths` 只返回 main，`aggregate_usage`/`_loc_scan`/`read_user_messages` 把子代理合并入父（真实成本保留，不单独计 session），`_SourceAdapter.subagents_of` 钩子统计折叠数（`n_subagents`），file_cache key 并入子代理文件签名。env 优先级：`PI_CODING_AGENT_DIR` > `PI_CONFIG_DIR` > `OMP_HOME`（legacy）> 默认 `~/.omp`。能力映射见 `metric_defs._SOURCE_SUPPORT`（omp 与 Claude 同为 Anthropic 语义：cache 读写分列、无独立 reasoning；另支持 ttft/ttft_p95/web_searches，不支持 context_window/compactions/rate_limit）。
27. **Codex 会话发现（resume 去重 + 头部快读）**：Codex `resume` 会**复用同一 `session_id` 写新 rollout 文件并重放整段历史**（`rollout-<新时间戳>-<同一uuid>.jsonl`），一个逻辑会话 = 多个累积快照文件（最新为超集）。`codex_reader.list_project_refs` 必须按 `session_id` 折叠：`_dedupe_by_session_id` 每组只留 `mtime+size` 最大的文件（最完整快照）。**只去重不合并求和**——累积快照求和会重复计 token（违背 #1 铁律）；`session_index.jsonl` 实测 stale（7 条 vs 磁盘 15 唯一 id），不可作去重依据。**性能**：项目列表只需 `cwd`+`session_id`（都在第 1 行 `session_meta` 头），`_session_head_meta` 只读头不扫整文件（实测 24×：整文件 `read_session_meta` 5.7ms/文件 → 头读 0.2ms/文件），重 Codex 用户启动从扫全部字节降到 O(文件数)。头读不进 file_cache（已够快且避免挤占共享 512 LRU）。全路径（GUI/CLI/audit/upload）经 `session_paths` 单点生效。
28. **`tool_variants` 补录技能身份**：`tool_calls` 按工具名计数，但 `Skill`/`Task`/`Agent` 三者的工具名不含被调用的技能/子代理，`reader.record_tool_variant` 从入参补录 `"Skill:dataviz"` / `"Agent:Explore"` → 次数（`_VARIANT_KEYS` 表驱动）。**仅 Claude 源**（其余 CLI 未以已解析的形状暴露该身份）；MCP 服务器不需要它——`mcp__<server>__<tool>` 键本身就带服务器名，五源通用。web 端据此构建 Skill / 子代理 / MCP 三个对比维度。
29. **web 聚合口径与桌面端一致**：`web/backend/db._agg_metrics` 的比率一律「分子和 ÷ 分母和」重算（返工率按 `code_added` 加权、工具错误率按工具调用数加权），**不取算术平均**（否则 5k 与 2M token 的会话等权，触发 Simpson 悖论）；无分母的比率（先读后写率/搜索后编辑比，分母来自工具时序、库里没有）退化为中位数并由 `_stat` 标注。**聚合 CTEI 按规则 9 重算**（`ctei(聚合TCER, 聚合CPE, 聚合CHR)`，与 audit 的 `aggregate_ctei_recompute` 同一口径），不对各会话 CTEI 取平均。决策实验室（`web/backend/analysis.py`）在此之上做「任务类型 × 会话规模」分层 + bootstrap 区间 + 证据分级，区间跨 0 一律不排名。
> 完整架构说明：[doc/architecture.md](doc/architecture.md)
> 数据格式细节：[doc/data-format.md](doc/data-format.md)
