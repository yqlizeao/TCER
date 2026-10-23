# TCER — Token-to-Code Efficiency Ratio

## 项目目标

基于真实 AI 编程助手会话数据，构建多维 AI 编程效率计量体系（TCER + 综合效率分）。支持七个数据来源：Claude Code（`~/.claude`）、Codex（`~/.codex`）、OpenCode（`~/.local/share/opencode`，仅 SQLite `opencode.db`）、Oh My Pi / omp（`~/.omp`）、Pi / earendil-works/pi（`~/.pi`）、Antigravity（`~/.gemini/antigravity-cli`）、WorkBuddy / CodeBuddy（`~/.workbuddy-ai` 等）。**已移除**（2026-10）：Grok 数据源与 Grok 独有指标（xAI 模型价格保留）、OpenCode 旧版磁盘 JSON 存储兼容。

- **GUI-only**：`python -m TCER` 启动桌面界面
- **纯离线**：不依赖 git、不做联网操作，数据来自本地 JSONL / SQLite 文件
- **零依赖**：纯 Python ≥3.11 标准库

**优先级：第一是数据的真实与完整性，第二是性能。** 任何缓存、降采样、头读、并行优化都不得改变数值结果；拿不准的数宁可显示「不适用」/「-」，不要编一个。

## 快速开始

```bash
python -m TCER            # 启动 GUI
python -m pytest tests/        # 运行测试
python -m tcer.audit           # 闭环审计：真实本地会话 vs 原始 JSONL 重算
```

### 闭环审计（开发必用）

改 `reader` / `analyze` / `loc` / 计价后，用真实 `~/.claude` 等数据验证，避免脑测：

```bash
python -m tcer.audit --list
python -m tcer.audit --source claude --project TCER --top 5 -v
python -m tcer.audit --source all --project TCER --json audit-out.json
python -m tcer.audit --all-projects --top 2 --no-loc   # 批跑全部项目（可加 --source）
python -m tcer.audit --all-projects --skip-empty --top 1 -q --summary-json -
python -m tcer.audit --ci --summary-json audit-summary.json   # 等价 CI 预设
# 退出码 0/1；cost_sum_per_model 校验逐模型成本加总
```

库入口：`tcer.core.audit.audit_project` / `audit_ref` / `summarize`（pytest 也可调）。检查项含：会话 Token 与文件重算一致、子代理折叠、LOC/unseen_writes、聚合 Token 求和、聚合综合效率分与公式重算一致（`aggregate_score_recompute`）、`cost_usd` 不崩、`abort_reasons` 求和 == `aborted_task_count`、`turn_stats_match_total`、`all_models_priced`。`paths.project_has_sessions` 统一判断空项目（GUI 置灰 + audit `--skip-empty`）。

## 仓库结构

```
TCER/
├── TCER/             Python 包
│   ├── core/              核心库（reader / loc / metrics / pricing / models / paths / analyze / export / format / audit / 各源 reader …）
│   ├── gui/               GUI（app / theme / metric_defs / widgets / views / charts / popups / popups_analysis / html_report）
│   └── config/            配置（model_pricing.json / composite_baselines.json）
├── server/                团队服务端（backend / frontend / concepts）
├── tests/                 测试（`python -m pytest tests/`）
└── doc/                   详细文档（见下表）
```

| 文档 | 内容 |
|------|------|
| [doc/architecture.md](doc/architecture.md) | MVC 架构、模块职责、SourceAdapter、file_cache、护栏测试 |
| [doc/data-format.md](doc/data-format.md) | 各数据源 JSONL/SQLite 格式、去重、LOC 回放、子代理、版本漂移基线 |
| [doc/metrics.md](doc/metrics.md) | 指标公式、综合效率分 v2、计价、任务类型、算例 |
| [doc/style.md](doc/style.md) / [doc/gui-notes.md](doc/gui-notes.md) | GUI 样式规范 / GUI 实现细节与踩坑 |
| [doc/llm-layer.md](doc/llm-layer.md) | LLM 解读、相空间动力学、Jev 双引擎、术语库 |
| [doc/server.md](doc/server.md) / [doc/concept-alignment-server.md](doc/concept-alignment-server.md) | server 聚合口径 / 团队概念对齐设计 |
| [doc/release-ops.md](doc/release-ops.md) | 检查更新、直接更新、配置存储 |

## 指标分类（6 组 · 以 `metric_defs.GROUPS` 为准）

扁平 6 组：G1 会话概况 · G2 Token 用量 · G3 缓存效率 · G4 代码产出与质量 · G5 成本分析 · G6 综合评分（效率指标 + 综合效率分三正交轴 + 基准参数）。数量以代码 SSOT 为准。**字体颜色**：白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考。公式与算例：[doc/metrics.md](doc/metrics.md)。

## 指标唯一真理源（SSOT）

`TCER/gui/metric_defs.py` 是**所有指标展示的唯一真理源**（名称/提示/单位/格式/好坏方向/取值），四个页签与全部 popups 都从这里取，禁止各处再自管：

- **会话/项目级**：`raw_value` / `format_value` / `display` / `report_values`；每个 `Metric` 带 `fmt`（`int`/`pct`/`float:N`/`money`/`text`）。
- **逐模型**：`MODEL_GROUPS` + `model_raw` / `model_display` / `model_tip`，同义指标转调 `format_value`；唯一例外：模型对比大数用 K/M 紧凑显示。
- **综合效率分构成**：`SCORE_AXES`。**评级体系**：`core/metrics.SCORE_TIER_BANDS`（来自 config `score_tiers`）是唯一源，`tier()`/排名条/趋势带均派生，勿手写阈值。
- **源能力感知**：`_SOURCE_SUPPORT` 标注「数据源根本不产生该字段」→ `display()` 返回「不适用」、`raw_value()` 返回 None；部分支持**不**标注，仍显示 "-"。改数据源支持范围后必须同步它。
- **HTML 报告**：`gui/html_report.py`（无 Tk 依赖）数值全走 `metric_defs.display`，与网格逐字节一致；图表组件在 `gui/charts.py`。

## 工程规范

1. **禁止中间产物**：用完即删，不提交。经验写入本文件（细节写 doc/）。
2. **GUI 全中文**：界面完整中文，仅 TCER 保留英文缩写；代码用缩写。
3. **纯离线**：GUI 不暴露需要 git 或网络的功能。**例外两处，均为用户显式 opt-in、未配置时零联网零入口**：①上传（`upload_client`/`upload_prefs`）；②LLM 语义解读（`llm_client`/`llm_prefs`，见 #38）。
4. **库层不动**：`TCER/core/` 有完整测试覆盖，改动需谨慎；改完跑 audit。
5. **运行方式**：`python -m TCER`（绿色免安装，不装包不改 PATH）。

## 关键不变量

编号沿用历史编号（doc/ 各节标题「原 #N」可追溯）。每条：规则 + 一句理由；细节见链接。

### 数据真实与完整性（第一优先）

1. **按 message.id 去重**：一个 API 响应被拆成多行重复携带 usage，必须按 id 只计一次（实测虚增 55.9%）；tool_use id 另行去重防 rewind 双计；`""` 视为无 id；零 usage 行释放 id 锁；`usage.estimated` 占位不计。Codex 对累计 `total_token_usage` 做差分（`last` 仅回退）；cache_write 是 input 的子集。OpenCode 多回合无 `step-finish` 时 peak 留 0。详见 [data-format.md#dedup](doc/data-format.md#dedup)。
2. **LOC 不依赖 git、不读磁盘**：净增只来自会话内工具调用回放；首写假定 old=0 并 `unseen_writes++`，`originalFile` 到达再回溯修正。产出 = 代码 ∪ 文本(.md/.txt 等) ∪ 配置(.json/.yaml/.toml)；Office 二进制刻意排除。磁盘先验/tree_loc/git 校准已移除。详见 [data-format.md#loc](doc/data-format.md#loc)。
3. **逐模型计价**：`TokenUsage.per_model` 按 message.model 分桶；价表录**标准价/闲时档**，多轨价记 `_note`；四级匹配（精确→归一化→前缀→反向前缀）；价差必须厂商官方页裁决，未匹配走 default 并提示。详见 [metrics.md#pricing](doc/metrics.md#pricing)。
4. **过滤 `<synthetic>`**：伪 assistant 消息（零 usage）在 reader 层丢弃，不进 `models`/`per_model`。[data-format.md#synthetic](doc/data-format.md#synthetic)。
5. **子代理并入父会话**：Token/LOC 保留真实成本，不单独计 session；Codex/OpenCode/omp/Pi 各有折叠规则，file_cache key 必须并入子代理签名。[data-format.md#subagents](doc/data-format.md#subagents)。
6. **时序分析**：`ToolOp(turn, tool, path)`；搜索后编辑比按回合就近（3 回合内）匹配、不绑文件；merge 时 rebase turn。[metrics.md#temporal](doc/metrics.md#temporal)。
7. **任务类型**：3 大类各有 TTAF，`ntcer = TCER / ttaf` 跨任务公平比较。[metrics.md#task-type](doc/metrics.md#task-type)。
8. **返工率 = 自返工率**：只计本会话先写后又被自己删/替换的行；删除会话外既有代码不计。[metrics.md#rework](doc/metrics.md#rework)。
9. **综合效率分 v2**（取代已移除的 CTEI）：产出/成本/质量三正交轴 → 半饱和 → 按 net_loc 证据收缩 → 加权 0–100；聚合有效，聚合分**重算**不取平均。详见 [metrics.md#score-v2](doc/metrics.md#score-v2)。
10. **自定义 Claude 配置目录**：以规范目录的父目录兄弟扫描识别（`.zclaude` 等），同 hash 跨根各成一卡；进程级缓存，新建目录需重启。[data-format.md#claude-roots](doc/data-format.md#claude-roots)。
11. **任务类型 SSOT**：`TASK_CATEGORIES`/TTAF 只来自 `composite_baselines.json`；基准参数默认 `None`→调用时读实时全局（**禁**用 `=TCER_BASELINE` 这类 def 时冻结的默认值）；个人基准至少 `MIN_BASELINE_SESSIONS=5` 条。[metrics.md#task-ssot](doc/metrics.md#task-ssot)。
13. **high_churn 合并**：子代理折叠/聚合用 `loc.merge_session_locs`，合并后重算。[metrics.md#high-churn](doc/metrics.md#high-churn)。
15. **「用户提问」口径 SSOT `core/user_input.py`**（cc-switch 规则）：注入/斜杠/中断/`!cmd` 不是提问，`<system-reminder>`/IDE 上下文块剥除后剩余文字才是；指标、弹窗、周报、上传、LLM、阅读器全经它，**只读主会话**（子代理派发 prompt 不是用户消息）；`api-request-blob` 存档记录不是会话事件。正文懒加载。[data-format.md#user-msgs-lazy](doc/data-format.md#user-msgs-lazy)。[data-format.md#user-msgs-lazy](doc/data-format.md#user-msgs-lazy)。
16. **缓存写 TTL 分档**：1h 缓存写子集按 `CACHE_1H_PREMIUM=0.6` 加溢价，漏掉会系统性低估成本。[metrics.md#cache-ttl](doc/metrics.md#cache-ttl)。
17. **深度信号**：`turn_duration` 是整轮墙钟，**不得**用于 `output_tps`；`totalCostUSD` 取会话内最大值；未知 type/子类型当前忽略。[data-format.md#signals](doc/data-format.md#signals)。
18. **逐回合时间线**：`TokenUsage.turn_stats`，merge 时 rebase；`output_tps`（自报逐响应 duration）仅 `{omp,pi}`；`est_output_tps`（日志时间戳估算、含首字等待、门槛 输出≥200/耗时≥1s、Σ/Σ）支持 `{claude,codex,opencode,omp,pi}`，字段 `est_gen_output_tokens/est_gen_duration_ms`；首字延迟显示逐回合中位数。[data-format.md#turn-stats](doc/data-format.md#turn-stats)。
19. **F1 修正**：`toolUseResult.originalFile` 回溯修正首写行数，`scan_session` 与 `session_loc_full` 必须逐字节一致。[data-format.md#f1](doc/data-format.md#f1)。
20. **Codex 工具错误**：exit code 权威；无码只认显式失败前缀，**禁止**全文 `error` 子串匹配。[data-format.md#codex-errors](doc/data-format.md#codex-errors)。
23. **模型对比分摊**：`compare_models` 按 token 占比加权（`_weight_sum`）。[metrics.md#compare-models](doc/metrics.md#compare-models)。
26. **omp 格式**：`~/.omp/agent/sessions`；usage 每响应一个直接累加；`snapshotsPruned` 的 edit 不能跳过（用 diff 回退）；子代理在主文件同名子目录；辅助用量（`model_usage` 等）暂不并入。[data-format.md#omp](doc/data-format.md#omp)。
27. **Codex 会话发现**：resume 复用同一线程 id 写累积快照 → 按**线程 id** 折叠只留最大文件，**只去重不求和**；项目列表头读（不进 file_cache）；`.jsonl.zst` 需 Python 3.14+。[data-format.md#codex-discovery](doc/data-format.md#codex-discovery)。
28. **`tool_variants`**：Skill/Agent 身份补录，仅 Claude 源；MCP 键自带服务器名。[data-format.md#tool-variants](doc/data-format.md#tool-variants)。
31. **Pi 格式**：omp 的上游原生，`pi_reader` 是薄壳复用 omp 解析层；reasoning 是 output 子集，**不**再加进 output。[data-format.md#pi](doc/data-format.md#pi)。
32. **LOC 按结果裁决**：编辑类调用等 tool_result 且**非 `is_error`** 才回放；`originalFile` 修正延迟一行；replace_all 少计是已知边界，由「LOC 可信度」暴露。[data-format.md#loc-outcome](doc/data-format.md#loc-outcome)。
33. **数据源版本漂移基线**（2026-10-22）：Claude 2.1.307 / Codex 0.163 / OpenCode 1.19.4 / omp 18.10.4 / Antigravity / WorkBuddy（schema 未经上游核实）；升级先比对基线再改。[data-format.md#version-baseline](doc/data-format.md#version-baseline)。

### 评分、指标族与基准

42. **Antigravity / WorkBuddy 数据源**：Antigravity 读 SQLite + 零依赖 Protobuf wire 解码（只读 `mode=ro` 连接，避免干扰运行中的 CLI）；WorkBuddy 无官方 schema（字段假设未经上游核实），按会话触及的业务路径提取最深共同工程根做项目聚合。[data-format.md#antigravity](doc/data-format.md#antigravity) · [data-format.md#workbuddy](doc/data-format.md#workbuddy)。
43. **洞察引擎 `core/insights.py`**：纯离线确定性规则（不调 LLM），`session_insights`/`project_insights` 产出亮点/拖累/建议；只在指标确有数据时判断，不臆测。[metrics.md#insights](doc/metrics.md#insights)。

34. **新指标族**：压缩代价、自报成本偏差、LOC 可信度、重试循环、单回合成本、人机时间、分段效率、项目级分析（热点文件/模型切换/工具画像）。[metrics.md#new-metrics](doc/metrics.md#new-metrics)。
36. **评分门控**：`net_loc≤0` 的会话**不评分**（三轴 None）；评级带按真实分位校准；逐源基准解析链 逐项目>逐源>全局；同模型跨源对照要求每源 ≥3 个有产出会话。[metrics.md#score-gating](doc/metrics.md#score-gating)。

### 性能与架构

12. **单次扫描**：Claude `reader.scan_session`、omp/pi `omp_reader.scan_file`（+`scan_folded` 折叠子代理）一趟出 usage+LOC（omp 含 meta）；公开的 `aggregate_usage`/`session_loc_full`/`_loc_scan` **刻意保持不走缓存的独立实现**，供 audit 与快路径交叉验证，勿改成调快路径。GUI `reanalyze` 用 `cancel_event` 协作取消。[architecture.md#single-scan](doc/architecture.md#single-scan)。
14. **mtime 缓存**：`file_cache` 按 `(normcase(abspath), mtime_ns, size, variant)` LRU（上限 4096；不用 `Path.resolve`——Windows 每次开句柄）；key 必须含旁路/子代理文件签名；可取消分析同样走缓存；返回共享引用勿改。**不做项目级结果缓存**（键需覆盖全部参数+配置版本，漏项即错数）。[architecture.md#file-cache](doc/architecture.md#file-cache)。
44. **性能改动的验收口径**：改读取/聚合层前先生成全项目黄金快照（每个 SessionReport + TokenUsage 全字段，浮点按 repr），改后逐字段 diff 必须完全一致（正在写入的活跃会话所在项目除外）；`parse_timestamp_ms` 等热路径改动须重跑 `tests/test_perf_equivalence.py` 对拍。[architecture.md#perf-acceptance](doc/architecture.md#perf-acceptance)。
21. **SourceAdapter**：非 Claude 各源共用 `analyze._analyze_source_project` + `_SourceAdapter` 钩子；新增数据源 = 写一个 adapter。[architecture.md#source-adapter](doc/architecture.md#source-adapter)。
22. **护栏测试**：`TokenUsage.merge` 反射断言、GUI 无头冒烟、`report_row_dict` ↔ CSV 字段归类；新增字段必改 merge。[architecture.md#guard-tests](doc/architecture.md#guard-tests)。

### GUI

24. **GUI 样式**（SSOT `gui/theme.py`，规范见 [style.md](doc/style.md)）：按钮 `widgets.flat_button`、菜单 `widgets.FlatMenu`、间距 `theme.PAD_*`、**禁 emoji 当图标**、Windows 高 DPI 须在创建 Tk 前设置、聚焦环全局消除（`takefocus=0`）、Toplevel 不设「关闭」按钮、折线抗锯齿 `charts._aa_layer`、数值禁科学计数法。[gui-notes.md#style](doc/gui-notes.md#style)。
25. **排名页**：综合效率分默认可算；无 score 时回退按 TCER 排名并提示。[gui-notes.md#ranking](doc/gui-notes.md#ranking)。
35. **GUI 新视图**：相关矩阵、仪表盘自定义、页签惰性渲染、SelectableLabel 合并结算（勿逐实例 `update_idletasks`）、时间线收敛诊断。[gui-notes.md#views](doc/gui-notes.md#views)。
37. **项目聚合页签**：按 `normcase+normpath(cwd)` 聚合，**父子路径不合并**，读不到 cwd 的 ref 独立成组；比率「分子和÷分母和」重算。[gui-notes.md#real-projects](doc/gui-notes.md#real-projects)。
45. **会话记录阅读器**（`gui/transcript_view.py`）：单个 `tk.Text` 文档流 + mark/tag，加载走 worker + 窗口自有队列 60ms 轮询；**往 Text 里嵌 widget（`window_create`）在窗口未排版时会让 Tk 8.6.15 崩进程，气泡一律纯 tag**；纯展示，数字全取自 `Transcript`。[gui-notes.md#transcript-reader](doc/gui-notes.md#transcript-reader)。

### opt-in 联网、LLM 与术语库（解读层，不进指标）

30. **更新与配置存储**：配置放 `app_dirs.prefs_dir()`（发布版 exe 同目录，回退 `~/.tcer/`，**不用 `~/.claude/`**）；检查更新只查公开 Release、不发用户数据；自更新后须立即 `SystemExit`。[release-ops.md#update-config](doc/release-ops.md#update-config)。
38. **LLM 解读层**：①未配置零联网；②输出只进解读层，**永不进**评分/CSV；③纯 urllib，不发采样参数；**每次数据出境必须弹知情确认、无「记住选择」**；会话右键走有界线程池 + UI 队列回填 + (session_id,kind) 去重；`audit_warnings` 机械审计反谄媚；语义审计只审自由生成叙事。[llm-layer.md#llm-interpret](doc/llm-layer.md#llm-interpret)。
39. **相空间动力学 / Jev 双引擎**：`_DYNAMICS_SYSTEM` 禁 LaTeX，正文平实化（禁物理黑话）；Jev 题面稳定（SHA-256 缓存）、分类题必带 none/other 逃生口、证据来自确定性遥测；相图交互见 [gui-notes.md#phase-portrait](doc/gui-notes.md#phase-portrait)。[llm-layer.md#dynamics](doc/llm-layer.md#dynamics)。
40. **团队术语库**：`termbase.find_terms_in_text` 是全系统唯一匹配引擎（拉丁词用严格词边界环视，**禁子串匹配**）；歧义阈值 `AMBIGUITY_FLAG_THRESHOLD=0.5`；术语数据**绝不**进指标/CSV；测试全离线 mock；新 GUI 功能验收必须构建完整 `TcerGui` 走真实入口，组件级绿 ≠ 可用。[llm-layer.md#termbase](doc/llm-layer.md#termbase) · [gui-notes.md#termbase-gui](doc/gui-notes.md#termbase-gui) · [教训](doc/llm-layer.md#delivery-lessons)。

### server

29. **server 聚合口径与桌面端一致**：比率一律「分子和÷分母和」重算，不取算术平均（Simpson 悖论）；聚合综合效率分重算。[server.md#aggregation](doc/server.md#aggregation)。
41. **团队概念对齐**：仅处理 `semantic_consent=1` 的实名会话；术语匹配复用客户端引擎；披露层 `disclosure.py` 唯一出口；概念模块不读效率指标列（红线 R1）。[server.md#concepts](doc/server.md#concepts) · [设计](doc/concept-alignment-server.md)。

> 编号说明：#1–#43（42、43 原见旧 AGENTS.md） 全部保留编号，仅按主题分组排列；原条目全文在 doc/ 对应「原 #N」小节。
