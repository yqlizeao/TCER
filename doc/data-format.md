# 数据格式与获取方式

## 本地文件路径

Claude Code 将所有会话数据存储在 `~/.claude/` 目录下（Windows: `%USERPROFILE%\.claude\`）：

```
~/.claude/
├── sessions/<pid>.json              # 进程级 session 元数据
├── projects/<project-hash>/<sessionId>.jsonl   # ★ 核心：完整对话数据
├── projects/<project-hash>/<sessionId>/         # session 子目录
│   └── subagents/                   # subagent 对话数据
├── history.jsonl                    # 全局 prompt 历史
├── settings.json                    # 用户设置
└── file-history/                    # 文件变更历史
```

**project-hash** 格式：将项目路径中的 `\`、`/`、`.`、`:` 替换为 `-`，例如 `c:\GitHub\TCER` → `c--GitHub-TCER`。

### 自定义配置目录（`.zclaude` 等）

启动 Claude Code 时传 `CLAUDE_CONFIG_DIR=%USERPROFILE%\.zclaude` 会让它改用 `.zclaude`（结构同 `.claude`）存数据，常用于不污染 `.claude`。该环境变量只在 Claude 进程内，TCER 读不到，因此 TCER 用**结构指纹**自动发现：以规范目录（`CLAUDE_CONFIG_DIR` 或 `~/.claude`）为锚，扫描其父目录下所有含 `projects/<hash>/*.jsonl` 的兄弟目录，全部当作 Claude 根。`list_projects()` 跨所有根查找，**同一项目 hash 在多个配置目录里各成一条**（`discover_jsonl(hash, roots=[根])` 按根分会话，不再跨根归并；`roots=None` 默认跨根 union 兼容 CLI）；只存在于自定义目录的项目也会出现。无需任何手动配置。

## Session 元数据（`sessions/<pid>.json`）

```json
{
  "pid": 3452,
  "sessionId": "199bfa09-b516-499e-b25f-6c52729bdc83",
  "cwd": "c:\\GitHub\\TCER",
  "startedAt": 1781779083553,
  "version": "2.1.181",
  "kind": "interactive",
  "entrypoint": "claude-vscode"
}
```

## 对话数据格式（`projects/<hash>/<sessionId>.jsonl`）

JSONL 格式，每行一个 JSON 对象。实测（cc 2.1.x）顶层 `type` 全景：

| type | 说明 | TCER 解析 |
|------|------|----------|
| `user` | 用户消息 / 工具结果行 | ✅ 消息计数、`is_error`、行级 `toolUseResult`（见下） |
| `assistant` | 助手回复（★含 token 用量） | ✅ usage 按 `message.id` 去重、tool_use/thinking 块、逐回合 `turn_stats` |
| `system` | 8 种子类型（见下表） | ✅ 部分子类型 |
| `ai-title` | 自动生成的 session 标题 | ✅ 取最新（tail 优先） |
| `last-prompt` / `mode` / `permission-mode` | 输入回显 / 模式切换 | ❌ |
| `attachment` | 17 种子类型（todo_reminder / plan_mode / edited_text_file…） | ❌ |
| `queue-operation` | 用户在 AI 运行时排队输入 | ❌（`iter_messages` 跳过） |
| `file-history-delta` / `file-history-snapshot` | Claude 自带文件版本历史 | ❌ |

### `type:"system"` 子类型（TCER 解析 3 种）

| subtype | 载荷 | TCER 用途 |
|---|---|---|
| `turn_duration` | `durationMs`, `messageCount` | ✅ **真实回合耗时**（不含用户暂停）→ 回填 `turn_stats`（稀疏，非每回合都有） |
| `api_error` | `error.status`(429=限流), `retryAttempt` | ✅ 429 → 限流命中 |
| `compact_boundary` | `compactMetadata.{trigger,preTokens}` | ✅ 压缩次数 |
| `stop_hook_summary` / `model_refusal_fallback` / `away_summary` / `local_command` / `informational` | hook 耗时/模型降级/… | ❌ 未解析 |

### 工具结果行的 `toolUseResult`（user 行的顶层兄弟字段）

| 字段 | TCER 用途 |
|---|---|
| `originalFile` | ✅ Write 前的真实原文 → **F1 修正**（`note_write_original` 回溯重算覆写增删） |
| `userModified` | ✅ AI 写完后被人手动改过 → 「人工修正」采纳信号 |
| `structuredPatch` | ✅ Claude 自算 diff 的 +/- 行 → `patch_diff_added/deleted`，回放 LOC 的独立交叉校验 |
| `filePath` / `stdout` / `stderr` / `interrupted` / `oldTodos` / `newTodos` 等 | 部分未解析 |

## Token 用量字段

每条 `assistant` 消息的 `message.usage` 字段：

```json
{
  "input_tokens": 2,
  "cache_creation_input_tokens": 43447,
  "cache_read_input_tokens": 0,
  "output_tokens": 1021,
  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 43447},
  "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}
}
```

- `input_tokens` — 非缓存输入 token（$3.00/MTok）
- `cache_creation_input_tokens` — 写入缓存（5m 档 $3.75/MTok）
- `cache_creation.ephemeral_1h_input_tokens` — ★1h TTL 子集，单价 2×input（=5m 的 1.6 倍），TCER 按 `CACHE_1H_PREMIUM` 加溢价计价——实测大部分缓存写走 1h 档，漏掉会系统性低估成本
- `cache_read_input_tokens` — 从缓存读取（$0.30/MTok）
- `output_tokens` — 输出（$15.00/MTok）
- `server_tool_use` — Claude 侧网页搜索/抓取次数（TCER 计入网页搜索指标）

行级元数据（每条 user/assistant 行都携带）：`version`（CLI 版本）、`gitBranch`、`effort`（推理强度）、`permissionMode` → TCER 填入 SessionMeta 对应字段。

## LOC 不依赖 git

净增代码来自会话内文件改写工具调用（Write/Edit/MultiEdit/NotebookEdit），逐条回放统计增删。

- **零外部依赖**：不需要 git，任何文件夹都能算
- **按会话精确归因**：不再有「提交落在时间窗间隙」的误差
- **忠实反映生成量**：计入多次重写/实验，通常大于最终进 git 的净增

代码库累计行数由 `tree_loc` 扫描工作目录得到（跳过 .git/node_modules/__pycache__ 等）。

### F1 风险（Write 覆写已有文件）与 originalFile 修正

`session_loc` 对每个会话从空的 `file_lines={}` 开始。Write 首次遇到某文件时先假设原大小为 0（并记入 `pending_f1`）；随后若工具结果行带 `toolUseResult.originalFile`（Write 前真实原文），`note_write_original` 回溯修正：覆写 → 按 `new−orig` 重算增删并撤销 `unseen_writes`；确认新文件 → 只撤销计数。**只有 originalFile 缺失的 Write 才残留 F1 暴露**，`unseen_writes` 是残留暴露面的上界。Edit 只看 delta，不受影响。

## 子代理处理

子代理（subagent）会话并入其父会话：Token 与 LOC 计入父会话（保留真实成本），不单独计为一个 session。文件位于 `<sessionId>/subagents/agent-*.jsonl`。

## 时间戳格式

JSONL 中时间戳可能是三种格式：
- 毫秒整数（>1e12）
- 秒整数
- RFC3339 字符串

`reader.parse_timestamp_ms` 自动归一化为毫秒。

## 工具调用统计

`reader.aggregate_usage` 从 assistant 消息的 `content` 中提取 `tool_use` 块，按 `message.id` 去重后统计每种工具的调用次数。

---

## Grok 数据格式（grok build CLI）

x.ai 的 grok build CLI 把会话持久化在 `~/.grok/sessions/`（`GROK_HOME` 可覆盖），按 **URL 编码的工作目录**分目录：

```
~/.grok/sessions/<URL编码cwd>/<UUIDv7>/
  summary.json     # 元数据：info.id / info.cwd / generated_title / current_model_id
                   #          / created_at / agent_name / reasoning_effort / sandbox_profile
  updates.jsonl    # ★权威 ACP 对话流（JSON-RPC 通知），token 用量与工具调用都在此
  chat_history.jsonl  # 原始发给模型的消息
  events.jsonl     # 轻量事件：TCER 解析 permission_resolved（decision/wait_ms → 审批等待）
  signals.json     # 聚合信号（≈68 字段）。TCER 解析：contextWindowTokens（窗口）、
                   #   contextTokensUsed（覆盖 peak_input——turn 累计 peak 虚高 10×+）、
                   #   minTimeToFirstTokenMs / itlP50Ms / itlP99Ms、cancellation/regenerationCount、
                   #   positive/negativeRatings、gitCommit/prCreated/prMergedCount、
                   #   agentLines*Reverted（回退行）、hasReverted（revert_events）
  rewind_points.jsonl / terminal/*.log / subagents/   # 未解析
```

`<URL编码cwd>` 例：`C:\playground\langfuse` → `C%3A%5Cplayground%5Clangfuse`（`%3A`=`:`、`%5C`=`\`）。

### updates.jsonl

每行一条 JSON-RPC 通知：`{"timestamp": <epoch秒>, "method": "session/update", "params": {"sessionId", "update": {...}, "_meta": {...}}}`。`params.update.sessionUpdate` 决定记录类型：

| sessionUpdate | 说明 | 关键字段 |
|---|---|---|
| `user_message_chunk` | 用户消息 | `content.text`、`_meta.modelId` |
| `agent_thought_chunk` | 推理流 | 计入 `thinking_count` |
| `agent_message_chunk` | 助手回复文本 | — |
| `turn_completed` | ★唯一 token 用量来源 | `usage`（见下） |
| `tool_call` | 工具发起 | `title`、`rawInput`、`_meta["x.ai/tool"]` |
| `tool_call_update` | 工具流式结果 | `rawOutput.exit_code`（错误归因）、`status` |

### Token 用量（`turn_completed.usage`）

每个 turn 恰好一条 `turn_completed`（无 Claude 式多行重复携带 usage 的去重问题），直接累加：

```json
"usage": {
  "inputTokens": 30305, "outputTokens": 116, "cachedReadTokens": 26368,
  "reasoningTokens": 73, "modelCalls": 1, "apiDurationMs": 3322,
  "modelUsage": { "grok-4.5": { ...同字段... } }
}
```

- 非缓存输入 = `inputTokens - cachedReadTokens`；缓存命中 = `cachedReadTokens`；缓存创建记 0（Grok 无独立写缓存计价）。
- `reasoningTokens` 单独展示，按输出价计费。
- `modelUsage` 提供按模型分桶（混用多模型会话精确）；`apiDurationMs` 累加为会话活动时长。
- **边界**：错误回合的 `turn_completed` 可能带空 usage（字段为 `null`）→ 计入 `empty_usage_skipped`，不虚增回合数。

### 工具映射

`_meta["x.ai/tool"].name`（规范名）映射到 TCER 通用工具分类：

| Grok 工具 | TCER 分类 |
|---|---|
| `read_file` / `search_replace` / `write` | Read / Edit / Write |
| `grep_search` / `list_dir` / `bash`·`run_terminal_command` | Grep / Glob / Bash |
| `task` / `web_search` / `web_fetch` | Task / WebSearch / WebFetch |
| `search_tool`·`use_tool`（MCP）等 | 取原始工具名 |

### LOC

`search_replace` / `write` 经与 Claude 相同的 `_LocAccumulator` 回放：`search_replace`→Edit（净增行差 + 自返工）、`write`→Write（`unseen_writes` / F1 同 Claude）。无编辑工具的会话 `net_loc=0`（已知零）。


## omp 数据格式（Oh My Pi / omp）

omp（[oh-my-pi](https://omp.sh)）把会话持久化在 `~/.omp/agent/sessions/`（`PI_CODING_AGENT_DIR` 重定位 agent 基目录、`PI_CONFIG_DIR` 重定位 `~/.omp` 根），按**目录编码的工作目录**分目录：

```
~/.omp/agent/sessions/<dir-encoded>/<ts>_<sessionId>.jsonl   # ★权威会话日志
~/.omp/agent/sessions/<dir-encoded>/<ts>_<sessionId>/        # 子代理会话（同 stem 子目录）
  *.jsonl
~/.omp/agent/blobs/<sha256>      # 大字符串/图片外部化（未解析）
```

`<dir-encoded>`：cwd 在 home 内 → `-<相对>`；在 temp 内 → `-tmp-<相对>`；其他 → `--<绝对>--`（`/ \ :` 替换为 `-`）。

### 会话 JSONL

每行一个 `SessionEntry`（首行是定宽 `type:"title"` 标题槽，解析时跳过）：

| type | 说明 | 关键字段 |
|---|---|---|
| `session` | 头（每文件一个） | `id` / `cwd` / `title` / `timestamp`(ISO-8601 字符串) / `version` |
| `model_change` | 活动模型 | `model`（`"provider/modelId"`，计价剥前缀） |
| `message` | 回合流 | `message.role` ∈ `user`/`assistant`/`toolResult` |
| `custom` / `custom_message` / `compaction` / `mode_change` 等 | 忽略 | — |

### Token 用量（`message.usage`，仅 assistant）

每条 assistant 消息携带**一个** `usage`（一次 API 响应一个，无 Claude 多行重复），直接累加：

```json
"usage": { "input": 100, "output": 20, "cacheRead": 40, "cacheWrite": 10,
           "totalTokens": 170, "cost": { "total": 0.01 } }
"contextSnapshot": { "promptTokens": 1500 }   // 单回合全量输入 → peak_input
"duration": 900, "ttft": 200                   // 回填 turn_stats.duration_ms / time_to_first_token_ms(+ttft_ms_samples)
```

- 语义同 Anthropic：`input` 不含缓存，`cacheRead`/`cacheWrite` 分列上报；无独立 reasoning token（并入输出）。
- 全零 usage 的 assistant 计入 `empty_usage_skipped`，不虚增回合数。
- `cost.total` 累加为 `reported_cost_usd`。

### 内容块与工具结果

assistant `content`：`thinking`（`thinking`/`thinkingSignature`）/ `text`（`text`）/ `toolCall`（`{id,name,arguments}`）。
`toolResult` 消息：`{toolCallId,toolName,content,details,isError,timestamp}`；`isError=true` → `tool_errors` + `tool_errors_by_tool`。

### 工具映射

| omp 工具 | TCER 分类 |
|---|---|
| `read` / `write` / `edit`·`ast_edit` | Read / Write / Edit |
| `grep`·`search` / `glob`·`find` / `bash`·`eval`·`ssh` | Grep / Glob / Bash |
| `todo` / `task` / `web_search` / `ask` | TodoWrite / Task / WebSearch / AskUserQuestion |

### LOC

`write` 取 `arguments.content` + `details.resolvedPath`（unseen_writes 同 Grok）；`edit`/`ast_edit` 取 `details.{oldText,newText,path}` 经同一 `_LocAccumulator`（净增行差 + 自返工）。`details.snapshotsPruned`（无 oldText/newText）的 edit 跳过。无编辑工具的会话 `net_loc=0`。

### 子代理折叠

omp 子代理会话存于主文件同名的 `<stem>/` 子目录，`_is_subagent_file` 识别后由 `aggregate_usage`/`_loc_scan`/`read_user_messages` 合并入父（真实成本保留，不单独计 session）。
