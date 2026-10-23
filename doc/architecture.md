# 架构与工程规范

## 源码模块

| 模块 | 职责 |
|------|------|
| `tcer/core/reader.py` | Claude JSONL 发现/解析、isMeta 过滤、head/tail 取样、时间戳归一化、message.usage 聚合 |
| `tcer/core/codex_reader.py` | Codex JSONL 发现/解析、cwd 项目分组、token_count 聚合、工具调用/运行环境/上下文/限流/补丁事件统计、apply_patch LOC |
| `tcer/core/opencode_reader.py` | OpenCode SQLite（仅 `opencode.db`）发现、project/session/message/part 读取、Token/工具/summary diff 聚合 |
| `tcer/core/omp_reader.py` | omp（Oh My Pi）`~/.omp/agent/sessions` JSONL 发现/解析、header cwd 分组、`message.usage` 单响应聚合、子代理 `<stem>/` 折叠、edit/write LOC |
| `tcer/core/pi_reader.py` | Pi（earendil-works/pi）`~/.pi/agent/sessions` 薄壳，复用 omp 解析层（`durationMs`、`provider`+`modelId`、`cacheWrite1h`/`reasoning`） |
| `tcer/core/antigravity_reader.py` | Antigravity（agy CLI）`~/.gemini/antigravity-cli` 会话发现/解析、新旧两套工具参数名 LOC |
| `tcer/core/workbuddy_reader.py` | WorkBuddy / CodeBuddy 会话发现/解析与按目录项目聚合 |
| `tcer/core/paths.py` | 定位 `~/.claude`（含 `.zclaude` 等自定义 `CLAUDE_CONFIG_DIR` 兄弟目录自动识别）/ `~/.codex` / `~/.local/share/opencode` / `~/.omp` / `~/.pi` / `~/.gemini/antigravity-cli` / WorkBuddy 根、项目哈希编解码、统一项目引用 |
| `tcer/core/loc.py` | git-free 代码量统计：session_loc（会话内工具调用回放，含 originalFile F1 修正；不读磁盘） |
| `tcer/core/metrics.py` | 全部公式：TCER/CHR/CPE/CAF/TTAF/NTCER/churn + 综合效率分 v2（`score_axes`/`efficiency_score`）+ 评级 + 逐模型成本 |
| `tcer/core/pricing.py` | 逐模型计价：从 `tcer/config/model_pricing.json`（≈350 模型，当前 347 条）解析 $/MTok |
| `tcer/core/models.py` | 数据类：TokenUsage / ModelUsage / TurnStat / ToolOp / SessionMeta / SessionReport |
| `tcer/core/analyze.py` | 编排层：Claude 独立路径（子代理折叠）+ 非 Claude 各源共享骨架 `_analyze_source_project` + `_SourceAdapter` 钩子（见下） |
| `tcer/core/parse_util.py` | 跨 reader 共享解析小工具（as_int / first_str） |
| `tcer/core/file_cache.py` | 进程级 mtime/size LRU 缓存（scan/usage/loc） |
| `tcer/core/format.py` | 纯值格式化器（千分位/百分比/时间戳/模型名） |
| `tcer/core/export.py` | JSON/CSV/Markdown 序列化 + 综合效率分排名数据 + 文本条形图（`_CSV_FIELDS`/`_CSV_EXCLUDED` 有漂移护栏测试） |
| `tcer/core/audit.py` | 闭环审计：`analyze` 结果对照原始会话文件重算（`python -m tcer.audit`） |
| `tcer/core/upload_client.py` / `upload_prefs.py` | 显式 opt-in 的上传（纯离线原则的例外之一，另有负责人） |
| `tcer/core/llm_client.py` / `llm_prefs.py` / `llm_prompts.py` | 显式 opt-in 的 LLM 语义解读（纯 urllib 调 OpenAI-compatible 端点；数据出境范围多选 metrics/dialog/tools；结果仅展示不参与指标计算；未配置零联网零入口） |
| `tcer/gui/` | Tkinter 图形界面（MVC 架构） |

### 非 Claude 源的 SourceAdapter

Codex / OpenCode / omp / Pi / Antigravity / WorkBuddy 共享 `analyze._analyze_source_project` 一条「逐会话 → 聚合」流水线；源差异收敛为 `_SourceAdapter` 的六个钩子：`resolve`（项目解析）、`sessions`（会话句柄枚举——Codex/omp/Pi 等为文件 Path，OpenCode 为 session id）、`read_meta`、`usage_of`、`loc_of`、`session_key`（可选 `subagents_of` 统计折叠子代理数，omp 用之）。file_cache 的失效 key 在钩子内构造（如 Codex / omp 必须并入子代理文件签名，否则子代理追写后折叠结果不失效）。**新增数据源 = 实现一个 adapter**。Claude 因子代理折叠与 cwd-keyed 扫描保持独立路径，但复用 `_mk_report` / `_MetricCtx` / LOC 聚合等共享件。


## omp 支持

omp（[oh-my-pi](https://omp.sh)）会话按 `~/.omp/agent/sessions/<dir-encoded>/<ts>_<sessionId>.jsonl` 发现（`PI_CODING_AGENT_DIR` 重定位 agent 基目录、`PI_CONFIG_DIR` 重定位 `~/.omp` 根），按 header `cwd` 聚合为项目。GUI 默认统一展示 Claude/Codex/OpenCode/omp/Pi/Antigravity/WorkBuddy 七源混合。

omp 的权威日志是该 JSONL：一个有序 `SessionEntry` 事件流。Token 用量来自每条 assistant 消息的 `usage`（`{input,output,cacheRead,cacheWrite,totalTokens,cost:{total}}`，语义同 Anthropic）——一次 API 响应一个，无 Claude 多行重复，直接累加；`contextSnapshot.promptTokens` 作 peak_input；`duration`/`ttft` 回填逐回合时间线与 TTFT（含 p95 样本）。`model_change.model` 为 `"provider/modelId"`，`pricing.normalize` 经 `rsplit("/")` 剥前缀后按模型分桶。

LOC 只从可解析的 `write`/`edit`/`ast_edit` 计算，经与 Claude 相同的 `_LocAccumulator` 回放：`write` 取 `arguments.content`+`details.resolvedPath`（unseen_writes）；`edit` 取 `details.{oldText,newText,path}`（净增行差 + 自返工）；`snapshotsPruned` 的 edit 跳过。无编辑工具的会话 `net_loc=0`。

子代理折叠：omp 子代理会话存于主文件同名的 `<stem>/` 子目录，`_is_subagent_file` 识别后由 `aggregate_usage`/`_loc_scan`/`read_user_messages` 合并入父（真实成本保留，不单独计 session）；`_SourceAdapter.subagents_of` 钩子统计折叠数，file_cache key 并入子代理文件签名。
## GUI MVC 架构

```
tcer/gui/
├── __init__.py     # main() 入口
├── __main__.py     # python -m tcer.gui 兼容入口
├── app.py          # 控制器：状态/后台线程/事件装配
├── theme.py        # 颜色/字体/Style 常量（无 Tk 依赖）
├── platform.py     # 跨平台字体/文件管理器/滚轮（无 Tk 依赖）
├── metric_defs.py  # 指标元数据 SSOT（标签/单位/说明/格式/好坏方向/源支持,无 Tk 依赖）
├── widgets.py      # 通用组件：Tooltip/ScrollFrame/Card/MetricCell
├── views.py        # 面板：FilterBar/ProjectColumn/SessionColumn(含搜索)/MetricPanel/排名/模型对比
├── charts.py       # 图表：TrendChart(趋势/散点/仪表板/时段热力四模式)/选择器/悬浮提示
├── popups.py       # 弹窗：详情/工具/模型/成本/基准/记忆/雷达/上传等
└── html_report.py  # 自包含 HTML 报告渲染（项目级/会话级,无 Tk 依赖,可无头测试）
```

### 分层职责

| 层 | 模块 | 职责 |
|----|------|------|
| Model | tcer/core/* | 数据采集、公式、编排 |
| 格式/导出 | core/format.py + core/export.py | 值格式化、JSON/CSV/Markdown |
| 数据定义 | gui/metric_defs.py | 指标元数据（中文标签/单位/说明/分层） |
| 通用件 | gui/widgets.py | 可复用 Tk 组件 |
| 面板 | gui/views.py | 各面板、图表、弹窗 |
| 控制器 | gui/app.py | 状态、后台线程、事件 |

## 工程规范

1. **禁止新增中间产物**：截图、临时脚本、草稿 md 等用完即删，不提交。必要的经验/理解写入 CLAUDE.md。
2. **GUI 全中文、代码用缩写**：界面显示完整中文（如「缓存命中率」而非「CHR」），仅 TCER 保留英文缩写。代码标识符可用缩写（chr/cpe/ntcer…）。
3. **运行方式**：`python -m tcer`（从仓库根目录，绿色免安装）。
4. **库层不动**：tcer/core/ 下模块有完整测试覆盖，改动需谨慎。GUI 改动集中在 tcer/gui/。
5. **纯离线**：不依赖任何版本管理工具（git 等）、不做任何联网操作。所有数据来自本地 `~/.claude/`、`~/.codex/`、`~/.local/share/opencode/`、`~/.omp/`、`~/.pi/`、`~/.gemini/antigravity-cli/` 与 WorkBuddy 目录。

## Codex 支持

Codex 会话按 `~/.codex/sessions/YYYY/MM/DD/*.jsonl` 发现，并按 `session_meta.payload.cwd` 聚合为项目。GUI 默认统一展示 Claude / Codex / OpenCode 项目，同时提供来源切换器。

Codex v1 只读分析本地 JSONL，不读取 SQLite 日志库、不删除 Codex 会话。Token 来自 `event_msg.token_count.payload.info.last_token_usage`：`cached_input_tokens` 映射为缓存命中，缓存创建记为 0，`reasoning_output_tokens` 单独展示且为输出子集（不重复计费）。任务时长优先使用 `task_complete.duration_ms`，首字延迟来自 `time_to_first_token_ms`。`task_started` / `task_complete` 是**回合生命周期**事件，只更新 `task_count` / `completed_task_count`（任务完成率），**不得**写入 `tool_calls["Task"]`（那是 Claude 子代理工具）。用户消息默认只统计数量、图片数量，打开弹窗时再按需读取正文。

Codex 深度指标来自官方 Codex 本地 transcript 与 `openai/codex` 协议源码中已持久化的 JSONL 字段：`session_meta` 提供 CLI 版本、来源、模型供应商、git 分支/提交；`turn_context` 提供模型、审批策略、沙箱策略、协作模式、推理强度和上下文窗口；`token_count.rate_limits` 提供限流快照；`response_item` / `event_msg` 提供 Web 搜索、上下文压缩、补丁应用、工具失败、任务完成/中断等事件。所有字段均为可空提取：旧 Codex 记录或 Claude 会话缺失时显示 `-`。

LOC 只从可解析 `apply_patch` 计算（`function_call` 与 live `custom_tool_call.input` 均可）；按文件累计 `+/-` 行，并跟踪会话内自返工（本会话先加后删的行 → `rework_deleted`）。无 `apply_patch` 的会话 `net_loc=0`（已知零，不污染项目聚合 TCER）。`apply_patch` 映射为 Edit 时，`ToolOp.path` 取补丁体中首个 `*** Update/Add/Delete File:` 路径；窗口使用率用**峰值单轮输入** ÷ `model_context_window`。

## OpenCode 支持

OpenCode 当前本地数据目录按官方文档位于 `~/.local/share/opencode/`。TCER 优先以只读 SQLite URI 打开 `opencode*.db`，读取开源仓库 `session/sql.ts` 定义的 `project` / `session` / `message` / `part` 表。仅支持 SQLite。task 工具派生的子会话（`session.parent_id`）折叠进顶层父会话。

OpenCode 会话**优先按 `session.directory` 分组**（`project.worktree` 常为 `/` 的 global 项目会误合并无关目录）；展示路径取有效 cwd。Token 使用 `session.tokens_*` 汇总：OpenCode 把 `tokens_reasoning` 存在输出之外，reader **折入 `output_tokens`**（仍单独记 `reasoning_output_tokens`），使成本按输出价计推理、且 `reasoning_output_ratio` ∈ [0,1]。峰值输入来自 `part` 的 `step-finish.tokens`（input+cache），禁止用会话累计当峰值。模型使用 `session.model` 与 assistant message 中的 provider/model；用户消息正文从 user text part 按需读取。工具行为来自 `part`（live 嵌套 `state.input` + camelCase `filePath`/`oldString`/`newString`），映射到 TCER 通用工具名。

OpenCode LOC：有 `summary_additions/deletions/files` 时用 summary；**live 数据 summary 常为 0** 时回放 edit/write tool part（与 Claude Edit 同构）。仅无 summary 且无编辑工具的会话将 TCER / CPE / 综合效率分 显示为 `-`。OpenCode 与 Codex 一样只读分析，不删除会话。

## 关键设计决策

### LOC 的 git-free 设计

净增 LOC 来自会话自身文件改写工具调用，不依赖 git。历史方案曾用 `git log --numstat`，已废弃：git 净增只反映最终提交、受提交习惯影响、且时间窗归因不可靠。

首次 `Write` 假定 old=0 并计入 `unseen_writes`（F1 暴露）；当工具结果行携带 `toolUseResult.originalFile`（Claude）时按**会话数据**回溯修正真实原行数。产品边界：TCER **绝不读取用户的真实仓库/工作目录**——磁盘先验、tree_loc 全树扫描、git 校准均已按产品定位移除，全部计量只来自会话数据。Edit / `search_replace` 始终用 old/new 行差。

### 按 message.id 去重

一次 assistant API 响应常被拆成多行写入 JSONL（thinking / text / 每个 tool_use 各一行），每行重复携带 usage。必须按 message.id 只计一次，否则 token 成倍虚高（实测全局 55.9% 重复计数）。tool_use 块各自唯一，LOC 统计不受影响。

### 逐模型计价

TokenUsage.per_model 按 message.model 分桶，merge 自动合并。cost_usd 对每个分桶用各自价表算成本再相加，混用多模型的会话也精确。

### 自定义 Claude 配置目录的多根发现

`CLAUDE_CONFIG_DIR` 是 Claude Code 的启动期参数（如 `.zclaude`），不在 TCER 进程环境里，故 TCER 无法直接读它。改为**结构指纹发现**：`paths.claude_config_dirs()` 以规范目录为锚，扫描其父目录下所有含 `projects/<hash>/*.jsonl` 的兄弟目录，全部视为 Claude 根。`list_projects()` 跨根**独立成条**（同 hash 每根各一条，`ProjectRef.config_root` 标所属根），`discover_jsonl(hash, roots=[根])` 按根分会话——同项目跨多个配置目录各自独立、不再归并，自定义目录独有的项目也会出现（`roots=None` 默认跨根 union，兼容 CLI 裸 hash）。按 `(home, CLAUDE_CONFIG_DIR)` 进程级缓存避免重复扫描。测试把 `CLAUDE_CONFIG_DIR` 指到 tmp 时父目录即 tmp，天然不污染真实 home。

## 测试覆盖

测试覆盖 reader / codex_reader / opencode_reader / paths / metrics / pricing / loc / export / baselines / metric_defs / audit / analyze / html_report，另有三道护栏：`test_models_merge.py`（反射断言 `TokenUsage.merge` 覆盖全部字段）、`test_gui_smoke.py`（无头构建全部图表模式与弹窗，无显示环境自动 skip）、`test_export.py` 的 CSV 字段漂移断言。Codex fixture 覆盖 cwd 分组、标题读取、token 去重、缓存映射、工具失败、apply_patch（含 custom_tool_call）LOC 与 ToolOp 路径、运行环境、上下文窗口峰值、首字延迟、限流、Web 搜索、图片输入和补丁成功率；OpenCode fixture 覆盖 SQLite 项目发现（含 directory 分组）、session 元数据、Token/缓存/推理折入输出、step-finish 峰值、工具错误、用户消息、图片输入、summary 为空时 tool 回放 LOC 与 analyze 聚合。闭环审计见 `python -m tcer.audit`（`tcer/core/audit.py`）。

## 实现不变量详述（自 CLAUDE.md 迁移）

> 本节自 CLAUDE.md 迁移而来（2026-10 瘦身）：CLAUDE.md 只保留一行硬规则，完整细节、实测数据与历史缘由在此。条目前缀「原 #N」= 瘦身前 CLAUDE.md 的「关键注意事项」编号，可据此追溯。

<a id="single-scan"></a>
### 原 #12 Claude 单次扫描

- `reader.scan_session` 一趟 JSONL 同时产出 TokenUsage + SessionLoc；`analyze` 进程内按 path 缓存，避免 usage 与 LOC 双读。GUI `reanalyze` 用 `cancel_event` 协作取消上一次分析。

<a id="file-cache"></a>
### 原 #14 mtime 缓存

- `tcer.core.file_cache` 按 `(path, mtime_ns, size, variant)` 缓存 scan/usage/meta（LRU，上限 4096——512 时「全部项目总览」工作集（44 项目 ≈675 条）发生轮换，热态总览退化到近冷态 4.3s；2048 后 0.25s；2026-10 体检时 53 项目工作集已达 1779 条（87%），升到 4096。claude `read_session_meta` 与 omp 同样入缓存，返回共享引用勿改）。取消靠 `cancel_check` 在 factory 内抛异常实现，部分扫描天然不入缓存——因此 GUI 的可取消分析**同样走缓存**（勿再用 `cancel_check is None` 做缓存开关）。各源均已接入：Claude `scan_session`、Codex/omp/pi usage+loc、OpenCode usage+loc（SQLite 会话 key 到 db 文件，粗粒度）。Claude 日期过滤复用 cwd-keyed 扫描，同文件不再双扫。测试可用 `file_cache.clear()`。


<a id="perf-acceptance"></a>
### 性能优化记录与验收口径（2026-10）

- **验收口径**：数据真实第一、性能第二。改读取/聚合层前，对 `paths.list_project_refs("all")` 全部有会话项目跑 `analyze_project`，把每个 SessionReport 与 TokenUsage 全字段（含 per_model / turn_stats / tool_ops）递归序列化为黄金快照（浮点按 `repr`），含 LOC 与 no_loc 两种模式；改后逐字段 diff 必须完全一致。正在写入的活跃会话所在项目无法冻结（基线自跑两次也不同），从比对中排除，改用单测与对拍覆盖。
- **omp/pi 单遍解析**：`omp_reader.scan_file` 一次解码出 meta、usage、loc（file_cache key `("omp_scan", is_subagent)`），`scan_folded` 折叠子代理；analyze 的 omp/pi usage 与 loc 钩子共用 `("omp_folded"/"pi_folded", 子代理签名)` 一条缓存。实测 omp 冷态 6.0s → 3.8s。
- **`TokenUsage.merge`**：最大回合号用 `max(map(attrgetter))`；对方无 `tool_ops`/`turn_stats` 时跳过整段重基准。热态 merge 0.655s → 0.259s（cProfile 口径）。
- **`parse_timestamp_ms`**：字符串分支抽为 `lru_cache(131072)` 的 `_parse_ts_str`；含 `:` 的串直接走 RFC3339 路径（`int()`/`float()` 都不接受冒号，输出不变）。新旧实现对本机 386,274 个不同时间戳取值对拍 0 差异（`tests/test_perf_equivalence.py`）。
- **`file_cache._sig`**：`Path.resolve()` → `normcase(abspath())`，省掉 Windows 每次开句柄（≈28µs × 5000 次）；代价是符号链接别名最多多缓存一份，正确性不受影响。
- **`metrics._is_code_search_tool`**：纯函数加 `lru_cache`（≈29 万次调用）。
- **刻意不做**：项目级 ProjectAnalysis 缓存（热态全量仅 0.64s，而键需覆盖全部参数、每个会话及子代理/旁路文件签名、价表与 composite_baselines 配置版本，漏项即错数）；Claude 解码层替换（`scan_once` 仅省 ≈5% 且要重现 Extra data 判定；字节流会改变 `errors="replace"` 语义）。
- **实测（53 项目 / 934 会话文件）**：冷态 11.38s → 9.2s，热态 0.79s → 0.64s；Claude 冷态的 json 解码与逐行解析是当前下限。

<a id="source-adapter"></a>
### 原 #21 SourceAdapter

- 非 Claude 各源（Codex/OpenCode/omp/pi/antigravity/workbuddy）共用 `analyze._analyze_source_project` 骨架 + `_SourceAdapter` 钩子（resolve/sessions/read_meta/usage_of/loc_of/session_key），file_cache key 构造在钩子内（omp/Codex 必须并入子代理文件签名，否则子代理追写后折叠结果不失效）。新增数据源 = 写一个 adapter。共享小工具在 `core/parse_util`（as_int/first_str）。

<a id="guard-tests"></a>
### 原 #22 护栏测试

- `tests/test_models_merge.py` 反射断言 `TokenUsage.merge` 覆盖全部字段（新增字段忘改 merge 会当场失败，容器/极值字段进 `_SPECIAL` 表）；`tests/test_gui_smoke.py` 无头构建全部图表模式与弹窗（无显示环境自动 skip）；`tests/test_export.py` 断言 `report_row_dict` 每个键必须归入 `_CSV_FIELDS` 或 `_CSV_EXCLUDED`。

## metric_defs SSOT 详述（原 CLAUDE.md「指标唯一真理源」节）

<a id="metric-defs-ssot"></a>

- `tcer/gui/metric_defs.py` 是**所有指标展示的唯一真理源**——名称 / 提示 / 单位 / 格式 / 好坏方向 / 取值，4 个页签（指标分类 / 排名 / 趋势 / 模型对比）与全部 popups（雷达等）都从这里取，禁止各处再自管：
- **会话/项目级**：`raw_value(report, key)`（图表用数值）、`format_value(key, native)`（显示串）、`display(report, key)`、`report_values(report)`。每个 `Metric` 带 `fmt` 规格（`int`/`pct`/`float:N`/`money`/`text`）。
- **逐模型**：`MODEL_GROUPS` + `model_raw` / `model_display` / `model_tip`，同义指标转调 `format_value` 与会话级逐字节一致；
  - **例外**：模型对比是 N 列并排，Token 数等大数量级用 K/M 紧凑显示（布局需要），比率/百分比仍与网格一致。
- **综合效率分构成**：`SCORE_AXES`（三正交轴名称/说明，排名页得分构成面板消费）。
- **评级体系**：`core/metrics.SCORE_TIER_BANDS`（名称+阈值，best→worst，来自 config `score_tiers`）是评级的唯一源；`tier()`、排名分布条、趋势评分带都从它派生。
- **源能力感知**：`metric_defs._SOURCE_SUPPORT` 标注「数据源根本不产生该字段」的指标（如 Claude 无独立推理 token、Codex 无缓存写入、上下文窗口/TTFT/限流为 Codex 独有）。`display()` 返回「不适用」、`raw_value()` 返回 None，网格/图表/HTML 报告自动继承；「不适用」与「-」一样参与空单元格折叠，但颜色置灰。部分支持（如 OpenCode 多回合 peak 留 0）**不**标注，仍显示 "-"。
- **HTML 报告**：`gui/html_report.py`（无 Tk 依赖，可无头测试）渲染项目级/会话级自包含单文件 HTML，数值全部走 `metric_defs.display`，与网格逐字节一致；GUI 导出菜单含项目/会话两组入口。图表组件在 `gui/charts.py`（趋势/散点/仪表板/时段热力图，从 views 拆出，views re-export 保持旧 import 路径）。
- **综合效率分轴分解**（旧 AGENTS）：`SCORE_AXES`（产出效率/成本/质量三正交轴，名称/公式/白话 tip），键与 `export.score_decompose` 对齐。
- **计价匹配示例补充**（旧 AGENTS）：mode 后缀剥离如 `claude-opus-4-6-thinking`→`claude-opus-4-6-20260206`。

## 历史与教训（工程）

- **缓存上限要按工作集定**：file_cache 先后从 512 升到 2048 再到 4096——全部项目总览的工作集（44 项目≈675 条 → 53 项目≈1779 条）超限后 LRU 顺序全扫是最坏情形，几乎全 miss（热态 0.25s 退化到 4.3s）。改动前先实测工作集条数。
- **取消靠异常、缓存要照常走**：`cancel_check` 在 factory 内抛异常，部分扫描天然不入缓存；不要再用 `cancel_check is None` 做缓存开关。
- **file_cache key 必须并入旁路文件签名**：子代理文件等被折叠的文件追写后，父会话结果必须失效。
- **新增字段忘改 merge 会静默丢数**：`TokenUsage.merge` 反射护栏测试、`report_row_dict` ↔ `_CSV_FIELDS/_CSV_EXCLUDED` 护栏、GUI 无头冒烟三道护栏是用事故换来的，别绕过。
- **性能次于数据真实**：任何缓存、头读、降采样优化都不得改变数值结果；用 `python -m tcer.audit` 逐字节对账验证。

