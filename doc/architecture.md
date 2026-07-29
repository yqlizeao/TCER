# 架构与工程规范

## 源码模块

| 模块 | 职责 |
|------|------|
| `tcer/core/reader.py` | Claude JSONL 发现/解析、isMeta 过滤、head/tail 取样、时间戳归一化、message.usage 聚合 |
| `tcer/core/codex_reader.py` | Codex JSONL 发现/解析、cwd 项目分组、token_count 聚合、工具调用/运行环境/上下文/限流/补丁事件统计、apply_patch LOC |
| `tcer/core/opencode_reader.py` | OpenCode SQLite/旧 storage JSON 发现、project/session/message/part 读取、Token/工具/summary diff 聚合 |
| `tcer/core/grok_reader.py` | Grok（grok build CLI）`updates.jsonl` ACP 流发现/解析、URL 编码 cwd 分组、`turn_completed` token 聚合、工具映射、search_replace LOC |
| `tcer/core/omp_reader.py` | omp（Oh My Pi）`~/.omp/agent/sessions` JSONL 发现/解析、header cwd 分组、`message.usage` 单响应聚合、子代理 `<stem>/` 折叠、edit/write LOC |
| `tcer/core/paths.py` | 定位 `~/.claude`（含 `.zclaude` 等自定义 `CLAUDE_CONFIG_DIR` 兄弟目录自动识别）/ `~/.codex` / `~/.local/share/opencode` / `~/.grok` / `~/.omp`、项目哈希编解码、统一项目引用 |
| `tcer/core/loc.py` | git-free 代码量统计：session_loc（会话内工具调用回放，含 originalFile F1 修正；不读磁盘） |
| `tcer/core/metrics.py` | 全部公式：TCER/CHR/CPE/CAF/TTAF/TA-TCER/churn/CTEI(三因子) + 评级 + 逐模型成本 |
| `tcer/core/pricing.py` | 逐模型计价：从 `tcer/config/model_pricing.json`（≈162 模型）解析 $/MTok |
| `tcer/core/models.py` | 数据类：TokenUsage / ModelUsage / TurnStat / ToolOp / SessionMeta / SessionReport |
| `tcer/core/analyze.py` | 编排层：Claude 独立路径（子代理折叠）+ 非 Claude 四源共享骨架 `_analyze_source_project` + `_SourceAdapter` 钩子（见下） |
| `tcer/core/parse_util.py` | 跨 reader 共享解析小工具（as_int / first_str） |
| `tcer/core/file_cache.py` | 进程级 mtime/size LRU 缓存（scan/usage/loc） |
| `tcer/core/format.py` | 纯值格式化器（千分位/百分比/时间戳/模型名） |
| `tcer/core/export.py` | JSON/CSV/Markdown 序列化 + CTEI 排名数据 + 文本条形图（`_CSV_FIELDS`/`_CSV_EXCLUDED` 有漂移护栏测试） |
| `tcer/core/audit.py` | 闭环审计：`analyze` 结果对照原始会话文件重算（`python -m tcer.audit`） |
| `tcer/core/upload_client.py` / `upload_prefs.py` | 显式 opt-in 的上传（纯离线原则的唯一例外，另有负责人） |
| `tcer/gui/` | Tkinter 图形界面（MVC 架构） |

### 非 Claude 源的 SourceAdapter

Codex / OpenCode / Grok / omp 共享 `analyze._analyze_source_project` 一条「逐会话 → 聚合」流水线；源差异收敛为 `_SourceAdapter` 的六个钩子：`resolve`（项目解析）、`sessions`（会话句柄枚举——Codex/Grok 为文件 Path，OpenCode 为 session id）、`read_meta`、`usage_of`、`loc_of`、`session_key`（可选 `subagents_of` 统计折叠子代理数，omp 用之）。file_cache 的失效 key 在钩子内构造（如 Grok 必须并入 signals.json / events.jsonl 旁路文件签名）。**新增数据源 = 实现一个 adapter**。Claude 因子代理折叠与 cwd-keyed 扫描保持独立路径，但复用 `_mk_report` / `_MetricCtx` / LOC 聚合等共享件。


## omp 支持

omp（[oh-my-pi](https://omp.sh)）会话按 `~/.omp/agent/sessions/<dir-encoded>/<ts>_<sessionId>.jsonl` 发现（`PI_CODING_AGENT_DIR` 重定位 agent 基目录、`PI_CONFIG_DIR` 重定位 `~/.omp` 根），按 header `cwd` 聚合为项目。GUI 默认统一展示 Claude/Codex/OpenCode/Grok/omp 五源混合。

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
2. **GUI 全中文、代码用缩写**：界面显示完整中文（如「缓存命中率」而非「CHR」），仅 TCER 保留英文缩写。代码标识符可用缩写（chr/ctei/ncpi…）。
3. **运行方式**：`python -m tcer`（从仓库根目录，绿色免安装）。
4. **库层不动**：tcer/core/ 下模块有完整测试覆盖，改动需谨慎。GUI 改动集中在 tcer/gui/。
5. **纯离线**：不依赖任何版本管理工具（git 等）、不做任何联网操作。所有数据来自本地 `~/.claude/`、`~/.codex/`、`~/.local/share/opencode/` 与 `~/.grok/`。

## Codex 支持

Codex 会话按 `~/.codex/sessions/YYYY/MM/DD/*.jsonl` 发现，并按 `session_meta.payload.cwd` 聚合为项目。GUI 默认统一展示 Claude / Codex / OpenCode 项目，同时提供来源切换器。

Codex v1 只读分析本地 JSONL，不读取 SQLite 日志库、不删除 Codex 会话。Token 来自 `event_msg.token_count.payload.info.last_token_usage`：`cached_input_tokens` 映射为缓存命中，缓存创建记为 0，`reasoning_output_tokens` 单独展示且为输出子集（不重复计费）。任务时长优先使用 `task_complete.duration_ms`，首字延迟来自 `time_to_first_token_ms`。`task_started` / `task_complete` 是**回合生命周期**事件，只更新 `task_count` / `completed_task_count`（任务完成率），**不得**写入 `tool_calls["Task"]`（那是 Claude 子代理工具）。用户消息默认只统计数量、图片数量，打开弹窗时再按需读取正文。

Codex 深度指标来自官方 Codex 本地 transcript 与 `openai/codex` 协议源码中已持久化的 JSONL 字段：`session_meta` 提供 CLI 版本、来源、模型供应商、git 分支/提交；`turn_context` 提供模型、审批策略、沙箱策略、协作模式、推理强度和上下文窗口；`token_count.rate_limits` 提供限流快照；`response_item` / `event_msg` 提供 Web 搜索、上下文压缩、补丁应用、工具失败、任务完成/中断等事件。所有字段均为可空提取：旧 Codex 记录或 Claude 会话缺失时显示 `-`。

LOC 只从可解析 `apply_patch` 计算（`function_call` 与 live `custom_tool_call.input` 均可）；按文件累计 `+/-` 行，并跟踪会话内自返工（本会话先加后删的行 → `rework_deleted`）。无 `apply_patch` 的会话 `net_loc=0`（已知零，不污染项目聚合 TCER）。`apply_patch` 映射为 Edit 时，`ToolOp.path` 取补丁体中首个 `*** Update/Add/Delete File:` 路径；窗口使用率用**峰值单轮输入** ÷ `model_context_window`。

## OpenCode 支持

OpenCode 当前本地数据目录按官方文档位于 `~/.local/share/opencode/`。TCER 优先以只读 SQLite URI 打开 `opencode*.db`，读取开源仓库 `session/sql.ts` 定义的 `project` / `session` / `message` / `part` 表；同时兼容旧版 `storage/session/{projectID}/{sessionID}.json` 的项目发现与基础 Token/LOC 读取。

OpenCode 会话**优先按 `session.directory` 分组**（`project.worktree` 常为 `/` 的 global 项目会误合并无关目录）；展示路径取有效 cwd。Token 使用 `session.tokens_*` 汇总：OpenCode 把 `tokens_reasoning` 存在输出之外，reader **折入 `output_tokens`**（仍单独记 `reasoning_output_tokens`），使成本按输出价计推理、且 `reasoning_output_ratio` ∈ [0,1]。峰值输入来自 `part` 的 `step-finish.tokens`（input+cache），禁止用会话累计当峰值。模型使用 `session.model` 与 assistant message 中的 provider/model；用户消息正文从 user text part 按需读取。工具行为来自 `part`（live 嵌套 `state.input` + camelCase `filePath`/`oldString`/`newString`），映射到 TCER 通用工具名。

OpenCode LOC：有 `summary_additions/deletions/files` 时用 summary；**live 数据 summary 常为 0** 时回放 edit/write tool part（与 Claude Edit 同构）。仅无 summary 且无编辑工具的会话将 TCER / CPE / CTEI 显示为 `-`。OpenCode 与 Codex 一样只读分析，不删除会话。

## Grok 支持

Grok（x.ai 的 grok build CLI）会话按 `~/.grok/sessions/<URL编码cwd>/<UUIDv7>/` 发现，并按解码后的 cwd 聚合为项目。GUI 默认统一展示 Claude / Codex / OpenCode / Grok 项目，并提供来源切换器。

Grok 的权威对话日志是 `updates.jsonl`——一条 ACP / JSON-RPC 通知流。Token 用量来自每个 turn 恰好一条的 `turn_completed.usage`：`cachedReadTokens` 映射为缓存命中，缓存创建记为 0，`reasoningTokens` 单独展示且按输出价计费；`modelUsage` 提供按模型分桶（混用多模型精确），`apiDurationMs` 累加为会话活动时长。**无 Claude 式多行重复携带 usage 的去重问题**——直接累加即可；错误回合的空 usage 计入 `empty_usage_skipped` 不虚增回合数。工具调用来自 `tool_call` 的 `_meta["x.ai/tool"].name`（`read_file`→Read、`search_replace`→Edit、`write`→Write、`grep_search`→Grep、`list_dir`→Glob、`bash`→Bash、`task`→Task），错误按 `tool_call_update.rawOutput.exit_code` 归因。用户消息正文从 `user_message_chunk.content.text` 按需读取。

LOC 只从可解析的 `search_replace` / `write` 计算，经与 Claude 相同的 `_LocAccumulator` 回放（Edit/Write 语义：净增行差 + 自返工 `rework_deleted` + high_churn）。无编辑工具的会话 net_loc=0（已知零，不污染项目聚合）。Grok 与 Codex/OpenCode 一样只读分析，不删除会话。详见 [doc/data-format.md](data-format.md#grok-数据格式grok-build-cli)。

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

测试覆盖 reader / codex_reader / opencode_reader / grok_reader / paths / metrics / pricing / loc / export / baselines / metric_defs / audit / analyze / html_report，另有三道护栏：`test_models_merge.py`（反射断言 `TokenUsage.merge` 覆盖全部字段）、`test_gui_smoke.py`（无头构建全部图表模式与弹窗，无显示环境自动 skip）、`test_export.py` 的 CSV 字段漂移断言。Codex fixture 覆盖 cwd 分组、标题读取、token 去重、缓存映射、工具失败、apply_patch（含 custom_tool_call）LOC 与 ToolOp 路径、运行环境、上下文窗口峰值、首字延迟、限流、Web 搜索、图片输入和补丁成功率；OpenCode fixture 覆盖 SQLite 项目发现（含 directory 分组）、session 元数据、Token/缓存/推理折入输出、step-finish 峰值、工具错误、用户消息、图片输入、summary 为空时 tool 回放 LOC 与 analyze 聚合；Grok fixture 覆盖 turn_completed token/缓存/推理聚合、按模型分桶、多 turn 累加、错误回合空 usage 跳过、工具映射与错误归因、search_replace/write LOC、URL 编码 cwd 分组、summary 元数据读取与 analyze 聚合。闭环审计见 `python -m tcer.audit`（`tcer/core/audit.py`）。
