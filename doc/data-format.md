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
| cost-state 行 | `totalCostUSD`（自报会话累计成本，含子代理；取会话内最大值，勿取末条、勿求和；`hasUnknownModelCost=true` 或 0 时留空） | ✅ → `reported_cost_usd` / `cost_vs_reported` |
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

## Codex 数据格式（OpenAI Codex CLI / Desktop）

Codex 把会话持久化在 `~/.codex/sessions/`（`CODEX_HOME` 可覆盖），按日期递归分目录，文件名 `rollout-<ISO时间>-<sessionId>.jsonl`：

```
~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<sessionId>.jsonl   # ★权威会话日志
~/.codex/sessions/session_index.jsonl                              # 会话 id→标题索引（实测常 stale，仅取标题）
```

每行一个事件 `{"timestamp", "type", "payload"}`。顶层 `type`：`session_meta`（头）/ `turn_context`（模型/窗口/审批策略）/ `response_item`（模型输出项：message / reasoning / function_call / custom_tool_call / *_output）/ `event_msg`（运行时事件：user_message / task_started / task_complete / turn_aborted / token_count / patch_apply_end / …）。

### resume 去重

Codex `resume` 复用同一 `session_id` 写**新 rollout 文件并重放整段历史**（`rollout-<新ts>-<同uuid>.jsonl`）——一个逻辑会话 = 多个累积快照，最新为超集。`list_project_refs` 按 `session_id` 折叠，每组只留 `mtime+size` 最大者（`_dedupe_by_session_id`）。**去重键用 `payload.id`（线程 id）而非 `session_id`**：新版 `session_id` 是根线程 id，子代理线程与父共享它；新版文件名为 `rollout-<ts>-<thread>_<rollout>.jsonl`，仅取线程段。7 天以上的冷 rollout 会被原地压成 `*.jsonl.zst`，reader 同时发现并经标准库 `compression.zstd` 读取（该模块 Python 3.14+ 才有，3.11–3.13 静默跳过压缩会话）。**子代理折叠**：子代理线程是独立 rollout，`session_meta` 带 `parent_thread_id` 或 `source={"subagent":{...}}`，父文件存在时折叠进最顶层父会话（子代理 user_msgs/图片清零），父文件缺失的孤儿仍独立。**只去重不求和**（累积快照求和会重复计 token）。项目列表只读第 1 行 `session_meta` 头取 `cwd`/`session_id`（`_session_head_meta`，不扫全文件）。

### Token 用量（`event_msg` / `token_count`）

`payload.info` 内含 `last_token_usage` 与 `total_token_usage`（`input_tokens`/`cached_input_tokens`/`output_tokens`/`reasoning_output_tokens`）。Codex 会**重复投递相同的 `last_token_usage`**（实测虚增 1.5–2.5%），故对单调递增的 `total_token_usage` **做差分**累加，`last` 仅作无 total 时回退；差分为负（total 重置）时退 `last` 并重新基准。新版另有 `cache_write_input_tokens`，与 `cached_input_tokens` 一样是 `input_tokens` 的**子集**，故非缓存输入 = `input_tokens − cached_input_tokens − cache_write_input_tokens`（旧数据无该字段读 0）。`rate_limits.{primary,secondary}.used_percent` → 配额峰值；`task_complete.{duration_ms,time_to_first_token_ms}` → 会话时长 / TTFT。

### 工具映射与错误归因

`function_call` / `custom_tool_call` 的 `name` 经 `_CODEX_NAME_MAP` 映射：`apply_patch`→Edit、`shell_command`/`write_stdin`→Bash、`update_plan`→TodoWrite、`view_image`→Read。`shell_command`/`exec_command` 再按命令首 token 细分 Grep/Glob/Read/Bash（`rg`/`grep`/`select-string`→Grep，`find`/`ls`/`rg --files`→Glob，`cat`/`get-content`/`sed`→Read）。工具错误：`*_output.output` 内 `Process exited with code N` / `Exit code: N` 权威判错；无退出码时只认显式失败前缀（`execution error` / `error:` / `failed:` / traceback），禁止全文 `error` 子串匹配（误报）。

### LOC

从 `apply_patch` 的 `*** Begin Patch` 补丁体回放（`_patch_file_deltas`），经与 Claude 相同的自返工口径累加（本会话先写后删的行计 `rework_deleted`）。`apply_patch` 无 `file_path` 参数——路径取自补丁体的 `*** Update/Add/Delete File:` 头。

### ★ Codex Desktop cli 0.146+ 格式变更（v1.0.19 适配）

新版 Codex Desktop（实测 `cli_version: 0.146.0-alpha`，`originator: "Codex Desktop"`）改了两处结构，旧解析会导致该来源会话「代码产出与质量」指标**全为 0**（Token 侧不受影响）：

1. **文件编辑挪位**：不再记为 `response_item` 的 `apply_patch` 调用，改为**只出现在** `event_msg` / `patch_apply_end.changes`：

   ```json
   {"type":"event_msg","payload":{"type":"patch_apply_end","success":true,
     "changes":{
       "D:\\...\\test.gd": {"type":"update","unified_diff":"@@ -321,3 +321,3 @@\n-old\n+new\n"},
       "D:\\...\\new.gd":  {"type":"add","content":"line1\nline2\n"},
       "D:\\...\\old.gd":  {"type":"delete","content":"x\ny\n"}}}}
   ```

   TCER `_loc_scan` 改**双来源**：`update`→`unified_diff` 的 +/- 行、`add`/`delete`→`content` 全行（`_patch_apply_end_deltas`）。旧 rollout 同时带 `apply_patch` response_item 与 `patch_apply_end` 结果事件，**优先 response_item，仅当无可解析 apply_patch 时才回退 `patch_apply_end`**，避免双计；`success:false` 的 patch 跳过。

2. **工具调用套 JS 壳**：所有工具改走 `custom_tool_call name="exec"`，真实命令埋在 `input` 的 JS 源里：

   ```
   const r = await tools.shell_command({command:"rg -n \\"性格|标签\" .\\project"}); text(r)
   ```

   `_classify_tool` 识别 `exec` 后用正则（`_EXEC_SHELL_RE`，容忍 `command` 键无引号）抽出内层命令，再走共享的 `_classify_shell_command`（Grep/Glob/Read/Edit/Bash）。提取只做常规反转义（`\"`/`\\`/`\n`），**不用 `unicode_escape`**——否则中文命令体会 mojibake。

> alpha 版格式可能未定稿；双格式兼容 + 回退对当前过渡是稳的，若 Codex 后续再改结构仍需重新适配。

---

## OpenCode 数据格式

OpenCode 把会话持久化在 `~/.local/share/opencode/opencode.db`（SQLite，TCER 以只读 URI 打开），读 `project` / `session` / `message` / `part` 表；**仅支持 SQLite**。

- **子会话折叠**：task 工具派生的子会话带 `session.parent_id`，`sessions_for_project` 只返回顶层，`aggregate_usage`/`_loc_scan` 递归折叠子会话（真实成本保留，不单独计 session），`subagent_ids` 供 `n_subagents`。
- **Token**：按逐 assistant 消息 `modelID` 的 `tokens` 分桶，余量记会话主模型；`tokens_reasoning` 折入输出。峰值输入只取 `step-finish` 快照，多回合无快照时留 0。
- **LOC**：只回放 `state.status == "completed"` 的工具（无 `state` 的旧形状放行）。`edit` 优先用 `state.metadata.filediff.{additions,deletions}`（消除 replaceAll 少计）；`apply_patch` 读 `metadata.files[{filePath,type,additions,deletions}]`，缺失时回退解析 `patchText`（`type=add` 视为确认新文件）；`write` 的 `metadata.exists === false` 撤销 `unseen_writes`。

## Antigravity 数据格式（agy CLI）

会话存于 `~/.gemini/antigravity-cli`。工具参数名新旧两套都认：新版 `write_file{Path,Content}` / `edit_file{Path,Target,Replacement}` / `read_file{Path}`，旧版 `TargetFile` / `CodeContent`（只认旧名会让 LOC 全丢）；`call_mcp_tool{ServerName,ToolName}` 记为 `mcp__<server>__<tool>`。摘要库的 `parent_conversation_id` / `nesting_depth` / `agent_name` 疑似子代理字段，实测全空，暂未做折叠。回合键（turn_stats / tool_ops / turn_net_locs）与其余各源一致从 0 起。

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
| `thinking_level_change` | 推理强度 | `thinkingLevel`（如 `high`）→ `reasoning_effort`；末个非空值胜出 |
| `message` | 回合流 | `message.role` ∈ `user`/`assistant`/`toolResult` |
| `compaction` | 上下文压缩 | → `compaction_count` / `compaction_turns` |
| `mode_change` | 模式切换 | `plan` → 计划模式 |
| `custom` / `custom_message` 等 | 忽略 | — |

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
- `stopReason == "error"` 且 `errorMessage` 含 429 / too many requests → 限流命中。
- `stopReason == "aborted"`（用户中断 / 运行中止）计入 `aborted_task_count`，`errorMessage`（如「Interrupted by user」）作 `abort_reasons` 键——语义同 Codex 的 `turn_aborted`。

### 内容块与工具结果

assistant `content`：`thinking`（`thinking`/`thinkingSignature`）/ `text`（`text`）/ `toolCall`（`{id,name,arguments}`）。
user `content` 里的 `{type:"image", mimeType, data}`（内联 base64 图片）计入 `image_count`（图片输入指标，同 Codex/OpenCode 口径）。
`toolResult` 消息：`{toolCallId,toolName,content,details,isError,timestamp}`；`isError=true` → `tool_errors` + `tool_errors_by_tool`。

### 工具映射

| omp 工具 | TCER 分类 |
|---|---|
| `read` / `write` / `edit`·`ast_edit` | Read / Write / Edit |
| `grep`·`search` / `glob`·`find` / `bash`·`eval`·`ssh` | Grep / Glob / Bash |
| `todo` / `task` / `web_search` / `ask` | TodoWrite / Task / WebSearch / AskUserQuestion |

### LOC

`write` 取 `arguments.content` + `details.resolvedPath`（首写 `unseen_writes++`）；`edit`/`ast_edit` 优先取 `details.{oldText,newText,path}`，缺失时依次回退 `arguments.edits[{oldText,newText}]`、`perFileResults[*].diff`、`details.diff`（`+N|text` / `-N|text` 行号前缀 diff）重建新旧文本，经同一 `_LocAccumulator`（净增行差 + 自返工）。`details.snapshotsPruned`（无 oldText/newText）的 edit 跳过。无编辑工具的会话 `net_loc=0`。

### 子代理折叠

omp 子代理会话存于主文件同名的 `<stem>/` 子目录，`_is_subagent_file` 识别后由 `aggregate_usage`/`_loc_scan`/`read_user_messages` 合并入父（真实成本保留，不单独计 session）。


## Pi 数据格式（earendil-works/pi 上游）

Pi（[earendil-works/pi](https://github.com/earendil-works/pi) 的 `pi-coding-agent`，bin 名 `pi`）是 omp 的**上游原生**（omp 是其 fork）。会话格式与 omp 同族，TCER 的 `pi_reader` 直接复用 `omp_reader` 的全部解析逻辑，仅以下差异：

- **目录**：`~/.pi/agent/sessions/`（仅 `PI_CODING_AGENT_DIR` 重定位 agent 基目录；**无 `PI_CONFIG_DIR`**，默认根固定 `~/.pi`）。
- `model_change` 用 `provider` + `modelId` 两个字段（omp 为单个 `model`），reader 两种都读。
- **无定宽 `type:"title"` 标题槽** —— 首行直接是 `type:"session"` 头。
- assistant 消息**无** omp-fork 加的 `contextSnapshot` / `ttft` → `peak_input` 留 0、TTFT 不可用；逐响应耗时字段为 `durationMs`（omp 为 `duration`），因此 Pi 支持 latency / `output_tps` / `est_output_tps`。
- usage 多两个字段：

```json
"usage": { "input": 100, "output": 20, "cacheRead": 40, "cacheWrite": 10,
           "cacheWrite1h": 4, "reasoning": 8,
           "totalTokens": 170, "cost": { "total": 0.01 } }
```

  `cacheWrite1h`（1h 缓存写子集，按 `CACHE_1H_PREMIUM` 溢价计价）+ `reasoning`（推理 token，填 `reasoning_output_tokens`）。按上游 `types.ts`，`output` 已含 `reasoning`，reader 只填比率字段、不再加进 `output_tokens`（已闭环）。

`SessionEntry` 行类型、工具映射、LOC 回放、子代理折叠均与 omp 一致（同 schema），详见上节。能力映射（`metric_defs._SOURCE_SUPPORT`）：pi 支持 reasoning / cache / web_search，**不支持** ttft / ttft_p95（数据无此字段）。

## 实现不变量详述（自 CLAUDE.md 迁移）

> 本节自 CLAUDE.md 迁移而来（2026-10 瘦身）：CLAUDE.md 只保留一行硬规则，完整细节、实测数据与历史缘由在此。条目前缀「原 #N」= 瘦身前 CLAUDE.md 的「关键注意事项」编号，可据此追溯。

<a id="dedup"></a>
### 原 #1 按 message.id 去重

- 一次 API 响应被拆成多行写入 JSONL，每行重复携带 usage。必须按 id 只计一次（实测 55.9% 重复计数）。
  - **tool_use id 去重（2.1.227+）**：rewind/重连后 Claude Code 会把历史（同 message.id + 同 tool_use id 的行及其 tool_result）重发进同一 jsonl——token 有 message.id 保护，但 tool_calls/tool_ops/LOC 回放按 tool_use id 去重（`seen_tool_uses`），否则双计。
  - **MCP 归因键在行顶层**（2.1.207+，行内无 toolUseResult）：`attributionMcpServer/Tool` 的读取必须在 toolUseResult 分支之外。
  - **边界**：空字符串 `""` 视为无 id，逐条计数。
  - **ccswitch 兼容**：mimo 消息第一行是 thinking 桩（usage=0），第二行才有真实 usage；零 usage 行会释放 id 锁（`seen.discard`），允许后续行贡献真实 token。
  - **Codex 差异**：`token_count` 事件会重复投递相同的 `last_token_usage`（实测虚增 1.5–2.5%），必须对单调递增的 `total_token_usage` 做差分，`last` 仅作无 total 时的回退。新版 `cache_write_input_tokens` 与 `cached_input_tokens` 一样是 `input_tokens` 的**子集**（非缓存输入 = input − cached − cache_write，旧数据无该字段读 0）。
  - **`usage.estimated`（Claude 2.1.3xx）**：流中断时客户端本地估算的占位 usage（`estimated:true`，无 stop_reason，常紧随 `<synthetic>` api_error）不是计费返回——reader 清零后走 `empty_usage_skipped`，不计回合、不进 per_model。
  - **OpenCode peak**：session 聚合计数是多步总和，仅单回合会话可作 peak_input 近似；多回合且无 `step-finish` 快照时 peak 留 0（ratio 显示 None，勿用总和虚增窗口占用）。详见 [doc/data-format.md](data-format.md)。

<a id="synthetic"></a>
### 原 #4 过滤 `<synthetic>`

- ccswitch 在 429 限流或系统占位时注入伪 assistant 消息，`model` 字段为 `<synthetic>`，usage 全为零。reader 层直接过滤，不计入 `models` 和 `per_model`。

<a id="subagents"></a>
### 原 #5 子代理并入父会话

- Token 与 LOC 保留真实成本，不单独计为 session。
  - **Codex 子代理**（新版多 agent）：子代理线程是独立 rollout，`session_meta` 带 `parent_thread_id` 或 `source={"subagent":{...}}`；父线程文件存在时 `list_project_refs` 过滤、`aggregate_usage`/`_loc_scan`（主文件）递归折叠到最顶层父会话（子代理 user_msgs/图片清零——派发 prompt），`_codex_*_of` 的 file_cache key 并入子代理文件签名；父文件缺失的孤儿与无 parent 的 review/compact 子代理仍独立。
  - **OpenCode 子代理**：task 工具派生的子会话（`session.parent_id`）由 `sessions_for_project` 只返回顶层、`aggregate_usage`/`_loc_scan` 递归折叠，`subagent_ids` 供 `n_subagents`；子会话与父同库，db 级缓存 key 已覆盖。

<a id="claude-roots"></a>
### 原 #10 自定义 Claude 配置目录自动识别

- 用户常以 `CLAUDE_CONFIG_DIR=%USERPROFILE%\.zclaude`（或其他自定义名）启动 Claude Code 以隔离 `.claude`。该环境变量只在 Claude 进程内、TCER 读不到；故 `paths.claude_config_dirs()` 以规范目录（`CLAUDE_CONFIG_DIR` 或 `~/.claude`）为锚，扫描其**父目录**里所有结构匹配 Claude 的兄弟目录（`projects/<hash>/*.jsonl` 指纹），全部视为 Claude 根。`list_projects()` 跨所有根查找，**同 hash 跨根各成一条**（每根独立成卡，`ProjectRef.config_root` 标所属根；`discover_jsonl(hash, roots=[根])` 按根分会话，`roots=None` 默认仍跨根 union 兼容 CLI）；结果按 `(home, CLAUDE_CONFIG_DIR)` 进程级缓存——会话期间新建的自定义配置目录需重启 TCER 才会出现。
  - **Windows**：盘符大小写导致同一根内 `C--GitHub-X` 与 `c--GitHub-X` 两文件夹时，`project_hash_key` 折叠为一项（根内 casefold 并集保留），但跨根不再合并。自定义根（如 `.claude-proxy`）的项目用 ccswitch 图标（`views.project_icon_key` 按 `paths.is_custom_claude_root` 判定）。

<a id="user-msgs-lazy"></a>
### 原 #15 用户消息懒加载

- 分析只计 `user_msgs` 数量；正文在弹窗/周报/上传/LLM 回退时懒读。
- **「用户提问」口径 SSOT：`core/user_input.py`**（2026-10 统一，规则移植自 cc-switch `push_user_texts`）。提问 = 真人发出、非注入、剥除注入片段后仍有可见文字（或内联图片）的 user 消息。Claude 首个非空文字块定类别：`[Request interrupted by user` → 中断事件；`<command-…>` 含 `<command-name>` → 斜杠命令事件（计 `slash_command_count`，不计提问）；`<bash-input>` → 用户 `!cmd`；`<bash-stdout/stderr>` → 命令输出；`<local-command-*>`/`<task-notification>` → 注入；其余剥除 `<system-reminder>` 与 IDE 上下文块（`<ide_opened_file>`/`<ide_selection>` 等）后剩余文字即提问，文字原样保留（用户粘贴的 HTML 不剥标签）；`isSidechain`/`isCompactSummary` 行与旧版「This session is being continued…」续接摘要不是提问。
  - 指标（`user_msgs`/`first_prompt_chars`/斜杠/纠正）、弹窗、标题回退、`read_dialogue`、`read_conversation` 的用户文字、会话记录阅读器全部经 `classify_claude_record`；跨源读正文统一走 `user_input.read_user_messages(source, path, sid)`。
  - **只读主会话**：子代理文件的 user 消息是派发 prompt（AI 写给 AI），Claude `subagents/`、omp/pi `<stem>/`、Codex/OpenCode 子会话一律不并入（上传的 `user_messages` 同样只含主会话；`conversation` 里子代理过程照常上传，但其派发 prompt 不再标成 user）。
  - 口径修正实测（本机 Claude）：旧版按前缀整条丢弃，VS Code 插件首块为 IDE 上下文的真人提问漏计 179 条；`api-request-blob` 存档副本虚增 175 条（见下）。
- **`api-request-blob` 不是会话事件**：新版 Claude Code 按 hash 存档发往 API 的请求消息副本（system/assistant/user 皆有，无 usage/id）。`reader.iter_messages` 与元数据头读统一跳过（`_NON_EVENT_TYPES`），否则提问/图片/思考计数被历史消息重算。

<a id="loc"></a>
### 原 #2 LOC 不依赖 git、不读真实仓库

- 净增代码只来自会话内工具调用回放。首次 Write 假定 old=0 并 `unseen_writes++`（F1 暴露）；Claude 的 `toolUseResult.originalFile` 到达后按会话数据回溯修正真实原行数（`note_write_original`，scan 与 session_loc_full 两路一致）。
  - **产品边界：磁盘先验（disk_prior）、tree_loc 全树扫描、git 校准均已按「纯离线仅析会话数据」定位移除**；会话日志里记录的 git 分支/提交等元数据属会话数据，保留。
  - **产出文件分类**（`loc.py`）：闸门 `_is_code` = 代码(`CODE_SUFFIXES` 纯源码) ∪ 文本(`TEXT_SUFFIXES`=.md/.txt/.rtf/.rst/.org/.adoc/.tex/.csv，**策划文档也算产出**) ∪ 配置(`CONFIG_SUFFIXES`=.json/.yaml/.toml)，三类都计入 net_loc/TCER；`_is_doc_file` 把散文类文本(.md/.txt/.rst/…，**不含 .csv** 数据)单列记入「文档行」。
  - **Office 二进制(.docx/.xlsx/.pptx)刻意排除**——AI 的 Write/Edit 是文本行模型，无法对二进制产生行增量（策划通常 .md/.txt 起草后自行转 Office）。Codex `_loc_scan` 用共享 `_is_test_file`/`_is_doc_file`（曾硬编码 .md，致 .txt 漏归文档行）。

<a id="f1"></a>
### 原 #19 F1 修正（originalFile）

- Claude 工具结果行 `toolUseResult.originalFile` 携带 Write 前真实原文。`_LocAccumulator.pending_f1` 记录按 old=0 记账的首个 Write，`note_write_original` 回溯修正（覆写→按 new−orig 重算并撤销 unseen；确认新文件→只撤销 unseen）。`scan_session` 与 `session_loc_full` 两条路径都做同一修正（审计交叉验证要求逐字节一致）。`toolUseResult.userModified` → 人工修正计数（采纳信号）；OpenCode `session.revert` → `revert_events`（Grok `hasReverted` 随 Grok 源 2026-10 移除）。

<a id="loc-outcome"></a>
### 原 #32 LOC 按结果裁决（is_error 不回放）

- （已知边界：Edit 的 replace_all 多处替换只按一次匹配计行差，LOC 系统性少计——无文件内容无法数出现次数，由「LOC 可信度」指标暴露偏差）：编辑类 tool_use（Write/Edit/MultiEdit/NotebookEdit）在 `scan_session`/`session_loc_full` 中先按 `tool_use_id` 缓冲（`pending_edits`），等对应 tool_result 到达且**非 `is_error`** 才回放——失败/被用户拒绝的调用不产生行增量、不污染 `file_lines` 基线、不计 `unseen_writes`。结果未到的（截断/进行中会话）在文件末尾按成功回放（宁可多计不可漏计）。连带顺序约束：`toolUseResult.originalFile` 的 F1 修正必须**延迟一行**处理（Write 记账现在发生在结果行，先记账再修正，否则 `pending_f1` 未建立修正丢失）。`on_tool_use(name, inp, turn=None)` 可选回合号 → 逐回合 LOC 流水 `TokenUsage.turn_net_locs`（分段 TCER 用；session_loc_full 无回合概念不产出，SessionLoc 两条路径仍逐字节一致）。

<a id="signals"></a>
### 原 #17 深度信号解析

- Claude `type:"system"` 子类型：`turn_duration.durationMs`（**整轮墙钟**耗时→回填 `turn_stats`；含该轮多次 API 补全 + 工具执行 + 用户等待，`messageCount` 可达 400+，**非单次生成耗时**，故不用于 `output_tps` 输出吞吐——Claude 该指标显示「不适用」）、`api_error`（429→限流命中）、`compact_boundary`（压缩）；`usage.server_tool_use`（网页搜索/抓取）；行级 `version/gitBranch/effort/permissionMode` → SessionMeta。
  - **cost-state 行（2.1.3xx）**：`totalCostUSD` 是 Claude 自报的会话**累计**成本（含子代理开销）——取会话内最大值（实测有非单调样本，勿取末条、勿求和），只读主会话文件，最后一条 `hasUnknownModelCost=true` 或值为 0 时留 None → `reported_cost_usd` → `cost_vs_reported`（本机实测价表÷自报中位 1.00）。新顶层 type（`api-request*`/`atis-latch`/`last-prompt`/`permission-mode`/`ai-title`/`agent-name`/`file-history-*`/`fork-context-ref`）与 system 子类型（`away_summary`/`scheduled_task_fire`/`local_command`/`informational`/`model_refusal_fallback`）当前忽略。改支持范围后同步 `metric_defs._SOURCE_SUPPORT`。

<a id="turn-stats"></a>
### 原 #18 逐回合时间线

- `TokenUsage.turn_stats: list[TurnStat]`（turn/ts/4 token/duration_ms/tool_calls/errors），各源填充：Claude 逐响应、Codex 逐 token_count 差分步（task_complete 回填耗时）、omp/pi 逐响应、OpenCode 逐 step-finish。merge 时 rebase turn（聚合对象时间线是串接非并发）。GUI「会话时间线」弹窗与会话级 HTML 报告消费。
  - **输出吞吐 `output_tps`**（G2，token/秒）= Σ output ÷ Σ 单次补全 `duration_ms`（秒），仅 `{omp,pi}` 支持（逐响应自报 duration，含首字等待；omp 字段 `duration`、Pi 字段 `durationMs`）；Claude 只有轮级墙钟、Codex 的 `task_complete.duration_ms` 是任务级墙钟、OpenCode 无逐补全耗时，均显示「不适用」（`_SOURCE_SUPPORT`，compute 同判定置 None）。跨源可比的估算速度见 `est_output_tps`（[metrics.md#est-speed](metrics.md#est-speed)；Claude/Codex/OpenCode/omp/pi，耗时含首字等待）。

<a id="codex-errors"></a>
### 原 #20 Codex 工具错误识别

- exit code 有两种实测格式（`Process exited with code N` / `Exit code: N`），exit code 权威；无码时只认显式失败前缀（`execution error`/`error:`/`failed:`/traceback），禁止全文 `error` 子串匹配（误报）。

<a id="codex-discovery"></a>
### 原 #27 Codex 会话发现（resume 去重 + 头部快读）

- Codex `resume` 会**复用同一线程 `id` 写新 rollout 文件并重放整段历史**（旧名 `rollout-<新时间戳>-<同一uuid>.jsonl`，新名 `rollout-<ts>-<thread>_<rollout>.jsonl`——`_session_id_from_filename` 只取线程段），一个逻辑会话 = 多个累积快照文件（最新为超集）。
  - **去重键用 `payload.id`（线程 id）而非 `session_id`**：新版 `session_id` 是根线程 id，子代理线程与父共享它，按它去重会把子代理误并进父会话（旧数据二者相等，兼容）。`codex_reader.list_project_refs` 必须按线程 id 折叠：`_dedupe_by_session_id` 每组只留 `mtime+size` 最大的文件（最完整快照）。
  - **只去重不合并求和**——累积快照求和会重复计 token（违背 #1 铁律）；`session_index.jsonl` 实测 stale（7 条 vs 磁盘 15 唯一 id），不可作去重依据。
  - **性能**：项目列表只需 `cwd`+`session_id`（都在第 1 行 `session_meta` 头），`_session_head_meta` 只读头不扫整文件（实测 24×：整文件 `read_session_meta` 5.7ms/文件 → 头读 0.2ms/文件），重 Codex 用户启动从扫全部字节降到 O(文件数)。头读不进 file_cache（已够快且避免挤占共享 LRU）。全路径（GUI/CLI/audit/upload）经 `session_paths` 单点生效。
  - **冷 rollout 压缩**：上游 `rollout/src/compression.rs` 把 7 天以上的 rollout 原地压成 `*.jsonl.zst`——`discover_sessions` 同时发现 `.jsonl.zst`（同名明文仍在时跳过压缩件），`_open_text` 经标准库 `compression.zstd` 读；
  - **该模块 Python 3.14+ 才有**，3.11–3.13 下 `zstd_supported()` 为 False、压缩会话静默跳过（零依赖约束下的已知边界）。其余新形态：`function_call_output.output` 可为 `input_text` 内容项数组（`_output_text` 拼接后再取 exit code）、`local_shell_call` 响应项按 Bash 计、`turn_started/turn_complete` 作 `task_*` 别名。`archived_sessions/` 未纳入。

<a id="tool-variants"></a>
### 原 #28 `tool_variants` 补录技能身份

- `tool_calls` 按工具名计数，但 `Skill`/`Task`/`Agent` 三者的工具名不含被调用的技能/子代理，`reader.record_tool_variant` 从入参补录 `"Skill:dataviz"` / `"Agent:Explore"` → 次数（`_VARIANT_KEYS` 表驱动）。
  - **仅 Claude 源**（其余 CLI 未以已解析的形状暴露该身份）；MCP 服务器不需要它——`mcp__<server>__<tool>` 键本身就带服务器名，五源通用。server 端据此构建 Skill / 子代理 / MCP 三个对比维度。

<a id="omp"></a>
### 原 #26 omp（Oh My Pi）数据格式

- 会话存于 `~/.omp/agent/sessions/<dir-encoded>/<ts>_<sessionId>.jsonl`（`PI_CODING_AGENT_DIR` 重定位 agent 基目录、`PI_CONFIG_DIR` 重定位 `~/.omp` 根；`omp_sessions_dir` 解析同 omp `dirs.ts`，未复刻 Linux XDG 重定向）。JSONL 每行一个 `SessionEntry`：首行 `type:"title"`（定宽标题槽，跳过）、`type:"session"` 头（`id`/`cwd`/`title`/`timestamp` 为 **ISO-8601 字符串**，`parse_timestamp_ms` 兼容）、`type:"model_change"`（`model` 为 `"provider/modelId"`，`pricing.normalize` 经 `rsplit("/")` 剥供应商前缀）、`type:"thinking_level_change"`（`thinkingLevel` 如 `high` → `reasoning_effort`，末个非空值胜出，pi 同源继承）；assistant 的 `stopReason=="aborted"`（用户中断/运行中止）计入 `aborted_task_count`、`errorMessage` 作 `abort_reasons` 键（语义同 Codex `turn_aborted`，pi 共享同一 `_aggregate_single`），`aborted_tasks` 指标 `_SOURCE_SUPPORT` 含 `{codex,omp,pi}`、`type:"message"`（`message.role` ∈ `user`/`assistant`/`toolResult`）。assistant 携带**每响应一个** `usage={input,output,cacheRead,cacheWrite,totalTokens,cost:{total}}`（无 Claude 多行重复，直接累加）+ `contextSnapshot.promptTokens`（peak_input）+ `duration`/`ttft`（回填 turn_stats / time_to_first_token_ms + `ttft_ms_samples`）；content 块 `thinking`/`text`/`toolCall={id,name,arguments}`；user content 的 `{type:"image",mimeType,data}` 内联图片计入 `image_count`（图片输入，`_SOURCE_SUPPORT` 含 `{codex,opencode,omp,pi}`）；`toolResult={toolCallId,toolName,content,details,isError}`。LOC：`write` 取 `arguments.content`+`details.resolvedPath`（首写 unseen_writes++）；`edit`/`ast_edit` 经 `omp_reader._edit_changes` 按序取 `details.{oldText,newText,path}` → Pi 的 `arguments.edits[{oldText,newText}]` → `perFileResults[*].diff` → `details.diff`（`+N|text`/`-N|text`，Pi 为 `+ 12 text`）重建新旧文本，经同一 `_LocAccumulator`（净增行差 + 自返工）。
  - **`snapshotsPruned` 的 edit 不能跳过**——实测 omp 一半以上成功 edit 只剩 diff（旧版跳过致 LOC 少计过半）。`compaction` 条目（`tokensBefore`/`tokensAfter`）→ `compaction_count`/`compaction_turns`/压缩代价；限流命中认 `errorStatus==429` 或错误文本含 429/too many requests。
  - **未并入的辅助用量**：omp `model_usage`（如 jev-latest）、Pi `usage`/`compaction.usage`/`branch_summary.usage`/`toolResult.usage` 是主对话外的真实用量，当前不计（并入会破 audit `turn_stats_match_total`/`all_models_priced`，需先加辅助用量字段）。`custom`/`custom_message` 等非 message 行忽略。
  - **子代理折叠**：omp 子代理会话存于主文件同名的 `<stem>/` 子目录（`..._<uuid>.jsonl` → `..._<uuid>/SubAgent.jsonl`），`_is_subagent_file` 按「父目录名 == 某 main 文件 stem」识别，`sessions_for_project`/`session_paths` 只返回 main，`aggregate_usage`/`_loc_scan`/`read_user_messages` 把子代理合并入父（真实成本保留，不单独计 session），`_SourceAdapter.subagents_of` 钩子统计折叠数（`n_subagents`），file_cache key 并入子代理文件签名。env 优先级：`PI_CODING_AGENT_DIR` > `PI_CONFIG_DIR` > `OMP_HOME`（legacy）> 默认 `~/.omp`。能力映射见 `metric_defs._SOURCE_SUPPORT`（omp 与 Claude 同为 Anthropic 语义：cache 读写分列、无独立 reasoning；另支持 ttft/ttft_p95/web_searches，不支持 context_window/compactions/rate_limit）。`read_session_meta` 走 file_cache（要全文件扫 title 变更/首条用户消息，30 会话项目重复分析曾花 150ms，缓存后近零）。 **prompt 行为信号**（斜杠命令 `slash_command_count`、纠正措辞 `correction_msg_count`、首条消息长度 `first_prompt_chars`）与 Claude 同构（user 文本消息），在 `_aggregate_single` 的 user 分支采集；判定逻辑（`is_slash_command`/`is_correction`/`CORRECTION_RE`）移至 `parse_util` 作跨源 SSOT（Claude/omp/pi 共用一张正则避免漂移）；子代理的 user 消息是 Task 派发 prompt，`_aggregate_single(is_subagent=True)` 跳过这三个信号（同 Claude 的 `is_sub` 守卫），成本计数仍保留。`_SOURCE_SUPPORT` 三键含 `{claude,omp,pi}`。

<a id="pi"></a>
### 原 #31 Pi 数据格式（earendil-works/pi 上游）

- Pi 是 omp（#26）的**上游原生**（omp 是其 fork，同族 JSONL 但有实质差异；`model_change` 用 `provider`+`modelId`（omp 用 `model`），两者都读）。会话存于 `~/.pi/agent/sessions/<dir-encoded>/<ts>_<sessionId>.jsonl`（**仅** `PI_CODING_AGENT_DIR` 重定位 agent 基目录；
  - **无 `PI_CONFIG_DIR`**，默认根固定 `~/.pi`，`paths.pi_sessions_dir`）。与 omp 的差异：
  - ①**无定宽 `type:"title"` 标题槽**，首行直接是 `type:"session"` 头（`_session_line` 照常定位）；
  - ②assistant **无** omp-fork 加的 `contextSnapshot`/`ttft` → `peak_input` 留 0、TTFT 不可用；逐响应耗时字段名为 `durationMs`（omp 为 `duration`），故 Pi 支持 latency/output_tps/est_output_tps；
  - ③usage 多两字段 `cacheWrite1h`（1h 缓存写子集）+ `reasoning`（推理 token）。实现：`pi_reader` 是**薄壳**，逐函数复用 `omp_reader` 解析层（同 schema），仅 override `discover_sessions`/`list_project_refs`/`resolve_project`/`sessions_for_project`/`read_session_meta`（source/entrypoint="pi"）。两增强字段由共享的 `omp_reader._add_turn_usage` 读取（**向后兼容**：omp 数据无此二字段读 0，行为不变；故 omp 测试不回归）：`cacheWrite1h` → `cache_write_1h_tokens` + bucket 第 5 参（1h 溢价计价见 #16）、`reasoning` → `reasoning_output_tokens`。
  - **reasoning/output 归并（已闭环）**：上游 `packages/ai/src/types.ts` 明示 reasoning 是 `output` 的子集（docs/message-types.md：「already included in output; do not add it again」），omp 本机 32757 条响应 `input+output+cacheRead+cacheWrite == totalTokens` 全成立——reader 只填比率字段、**不**加进 output。工具映射 `_PI_TOOL_MAP = _OMP_TOOL_MAP`（同源子集，未知名 pass-through）；子代理折叠复用 `_is_subagent_file`（布局相同则折叠，否则当主会话不崩）。能力映射（`metric_defs._SOURCE_SUPPORT`）：pi 支持 reasoning/reasoning_ratio/web_searches/cache_write/cache_write_ratio/cache_efficiency，**不支持** ttft/ttft_p95（无 ttft 字段）及 context_window/rate_limit 等。GUI 显示名「Pi」，与 omp 并存（不同目录/用户群）。

<a id="version-baseline"></a>
### 原 #33 数据源版本漂移基线（2026-10-22 校准）

- 各源按此基线核对，下次升级先比对再改。
  - ①**Claude Code 2.1.311**（2026-10-27 覆盖审计：本机 699 文件 / 22.5 万行逐值普查，登记表 `core/coverage.CLAUDE_REGISTRY`，`python -m tcer.audit --coverage`）：所有行类型 / system 子类型 / attachment 子类型 / 内容块 / usage 键均已登记为「已处理」或「已知忽略（附理由）」，无未知项；计价相关取值 `speed`/`service_tier` 全为 standard、`inference_geo` 仅 ""/global/not_available（白名单外即报）；`api-request`（2.1.300+）只记主线程/子代理/sdk 请求，**标题生成、离开摘要等附带 LLM 调用的用量不落盘，本地无法计入**；请求 7905 / 有用量回复 7615，差额为报错/拒绝/中断请求，是否计费本地无法判断。2.1.307 时的记录：见 #1 `usage.estimated`、#17 cost-state；工具名 `Agent` 取代 `Task`（`_VARIANT_KEYS` 已覆盖二者）；`server_tool_use` 块名新增 `analyze_image`/`webReader`；子代理布局、message.id 重复、1h/5m 缓存档、`usage.iterations`（全为单项）无漂移。
  - ②**Codex rust-v0.163.0**（上游源码，本机数据停在 0.142.5，**新格式仅 mock 验证**）：见 #1 cache_write、#5 子代理、#27 线程 id 去重与 `.jsonl.zst`；未确认：子代理 rollout 的 `subagent_history_start_ordinal` 继承段若含 token_count 可能轻微重复计。
  - ③**OpenCode v1.19.4**（本机 db 1.19.2）：仅 SQLite（`session`/`message`/`part`）。LOC：只回放 `state.status=='completed'` 的工具（同 #32 口径，无 state 的旧形状放行）；edit 优先 `state.metadata.filediff.{additions,deletions}`（消除 replaceAll 少计）、`apply_patch` 读 `metadata.files[{filePath,type,additions,deletions}]`（缺失回退解析 `patchText`，`type=add` 视为确认新文件）、write 的 `metadata.exists===false` 撤销 unseen；混合模型按逐 assistant 消息 `modelID` 的 `tokens` 分桶、余量记会话主模型。上游 dev 分支的 v2 存储（`session_v2`/`session_message`、`opencode-next.db`）**未发布未实现**。
  - ④**omp 18.10.4 / Pi**：见 #26（diff 回退、compaction、429）、#31（`durationMs`、`provider+modelId`、reasoning 已闭环）。
  - ⑤**Antigravity**（`~/.gemini/antigravity-cli`，本机 26 会话）：新版工具参数 `write_file{Path,Content}`/`edit_file{Path,Target,Replacement}`/`read_file{Path}` 与旧 `TargetFile`/`CodeContent` 两套都认（旧版只认旧名，LOC 全丢）；`call_mcp_tool{ServerName,ToolName}` 记为 `mcp__<server>__<tool>`（#28）。摘要库新增 `parent_conversation_id`/`nesting_depth`/`agent_name` 疑似子代理字段，本机全空，未做折叠。
  - ⑥**WorkBuddy**：无官方公开 transcript schema，reader 字段假设来自作者样本、**未经上游核实**；`paths.workbuddy_roots` 额外认产品自身的 `WORKBUDDY_CONFIG_DIR`/`CODEBUDDY_CONFIG_DIR`。依赖本机真实目录的测试在目录缺失时 skip。

## 补充：旧 AGENTS.md 独有的数据源条目

> 以下条目来自旧 AGENTS.md（CLAUDE.md 的较早分叉，2026-10 已改为短索引文件）。其中 CLAUDE.md 原本没有的内容迁入此处；与 CLAUDE.md 重复的条目以后者为准，不再重复收录。

<a id="antigravity"></a>
### 原 AGENTS #33 Antigravity 数据格式（Google DeepMind Antigravity CLI / agy）

- 会话索引存于 `~/.gemini/antigravity-cli/conversation_summaries.db`（表 `conversation_summaries` 含 `workspace_uris`、`last_modified_time` 等），会话详情存于 `conversations/<uuid>.db`（`ANTIGRAVITY_DIR` / `ANTIGRAVITY_CLI_DIR` / `GEMINI_CLI_DIR` 重定位基目录，`paths.antigravity_dir`）。**cc-switch 隔离终端**（「以此供应商打开终端（环境隔离）」，2026-10-27 实测）把 agy 数据放在 `~/.cc-switch/agy/<provider_id>/antigravity-cli`，目录结构与默认根完全一致；`paths.antigravity_roots()` = 默认根 + 该处全部 profile（设了上述环境变量则只认环境变量、不扫 cc-switch），`antigravity_reader._workspace_sessions` 跨根按规范化工作区合并成一张项目卡，同一会话 id 出现在多个根时只计一次（取 mtime 最新的文件）。Claude/Codex 等其他 app 的隔离目录布局尚无实测数据，未接入。

**2026-10-27 全量核对（34 个会话）**：①**并行工具调用**：step 15 字段 20 下可重复多个字段 7，`_extract_tool_calls` 逐个计（只取首个曾漏 490 次调用，含 185 次编辑/写入）；②**step 132 字段 5.4 回显原调用**（call_id/名/参数，与 step 15 逐字节一致、按调用顺序到达）——LOC 改为按结果裁决（#32）：结果成功（status 0/3/5）才回放，status 7 失败编辑（`Could not successfully apply`）不计；③**step status**：3=完成、5=完成但 payload 被裁剪（step 15 调用体清空、step 14 只剩 19.3.1 提问副本，实测仅 703d5cd2 前 178 步）、7=失败；status 5 不算工具错误，调用由结果回显补回，提问取 19.3.1；提问兜底扫描严格解码并拒控制字符，不再把 protobuf 片段当正文；④**上下文压缩 step 23** 是一次真实 LLM 调用，usage 只在步骤元数据 5.28.2、不进 `gen_metadata`（实测 82 次全漏，计入后成本 +2.7%）：按响应 id 与 gen 去重后计入，记录不带模型 → 按前一次生成的模型计价（推断），同时填 `compaction_count/compaction_turns`；⑤`manage_task` 是后台命令（`run_command` 异步，任务号 `<会话id>/task-N`，结果以 step 101 系统消息回传）的查看/停止，**不是子代理**，不再映射为 `Task`；⑥`/resume` 续写同一会话 id 的同一个 .db（无 idx 断档、无重复响应 id），按文件计会话正确；子代理表 `parent_references` 与摘要库子代理字段本机全空；⑦辅助 LLM 调用（安全监控 `sir_flash_lite`/`severe_internal_risks_agy`、会话标题生成）不写会话库，经代理供应商时实测全部失败，无隐藏成本。纯 Python 零依赖轻量 Protobuf wire format 解码器（`_parse_proto_fields`）解析 SQLite 中的二进制 blob：`gen_metadata` 存储权威逐生成 usage（`uncached_input`、`cached_input`、`total_output`、`reasoning`，每响应一条；字段 2 = 未命中缓存输入、5 = 缓存命中、3 = 输出（= 9 思考 + 10 回复）。**口径**：`input_tokens` 只记未命中部分、缓存命中单列 `cache_read_input_tokens`（`total_input` 才是二者之和）——2026-10-23 前曾把二者之和记进 `input_tokens`，缓存被计两遍且按全价计费，实测单会话 Token ×2、成本 ×7.7，修正后与 cc-switch 逐位一致；`peak_input_tokens` 仍取逐生成全量输入），`steps` 表存储事件流（`step_type==14` 用户消息/斜杠命令/纠错检测；`step_type==15` 工具调用，映射 `view_file`→Read, `write_to_file`→Write, `replace_file_content`→Edit, `run_command`→Bash 等；`step_type==132` 工具结果输出与 exit code 检测）。LOC 回放：Write 与 Edit 经同一 `_LocAccumulator` 计算真实净增代码行与自返工率。能力映射：支持 reasoning/reasoning_ratio/slash_commands/web_searches，Gemini 无独立缓存写计费因此 cache_write 标注不适用。只读模式（`mode=ro`）连接 SQLite 避免干扰正在运行的 CLI 进程。

<a id="workbuddy"></a>
### 原 AGENTS #34 WorkBuddy 数据格式与按目录项目级自动聚合（WorkBuddy / CodeBuddy）

- 会话为 JSONL 流（默认 `~/.workbuddy` 或 `WORKBUDDY_DIR`/`WORKBUDDY_SESSIONS_DIR`）。行类型含：
  - ①`session-meta` 与 `ai-title`（取末行权威标题；无则提取首条用户原话）；
  - ②`message`（用户提问自动剔除 `<system-reminder>` 注入块、格式化 `<message_quote>` 引用、解开 `<user_query>` 纯净文本；助手文本响应）；
  - ③`reasoning`（思考过程，提取 `rawContent` 或 `providerData.reasoning`）；
  - ④`function_call`（每个步骤在 `providerData.rawUsage` 中携带权威 usage，按 `messageId` 去重累加，模型名归一到 `pricing` 表；工具映射含 `PowerShell`/`Bash`/`Read`/`Edit`/`Write`/`Grep`/`Glob`/`ToolSearch`/`present_files`/`DeferExecuteTool`）；
  - ⑤`function_call_result`（执行状态与输出）。LOC：`Edit` 与 `Write` 经同一 `_LocAccumulator` 回放计算净增代码行与自返工率。
  - **按目录项目级自动聚合**：WorkBuddy 会话在运行时通常以时间戳沙箱目录为 `cwd`（如 `WorkBuddy\2026-10-16-...`），直接按 `cwd` 聚合会导致每个会话散落成独立时间戳单项。`extract_project_dir_from_session` 深度分析会话中工具操作与命令行中触及的业务文件路径，排除 WorkBuddy 沙箱和全局记忆目录后，提取各路径的最深共同业务工程根（如 `E:\slg`）；在项目发现（`list_project_refs`）时将同一真实工程目录下的所有会话自动合并归集进同一个 `ProjectRef`（`key="E--slg"`, `display_name="slg"`, `cwd="E:\slg"`），实现跨沙箱会话在项目级别的自动聚合与指标统算。

- **omp 模型归属补充**（旧 AGENTS #26）：模型仅在产生真实 Token 的助手回合计入 `u.models`，避免初始切换或 400 报错的 0-token 模型变成看板「隐身模型」；全空会话回退当前模型。

## 历史与教训（数据源与 LOC）

- **重复计数是头号风险**：Claude 同一 API 响应被拆成多行重复携带 usage（实测 55.9%），Codex `token_count` 重复投递 `last_token_usage`（虚增 1.5–2.5%），rewind/重连会重发整段历史。凡新增数据源，先查「同一事件会不会被写多次」，再决定按 id 去重还是对累计量做差分。
- **只去重、不对累积快照求和**：Codex resume 的多个 rollout 是累积超集，取最完整一份，求和必然重复计 token。`session_index.jsonl` 等索引文件实测会 stale，不能当去重依据。
- **LOC 一律按「结果」裁决**：失败/被拒绝的 Edit 不得进入行增量（原 #32）；`originalFile` 的 F1 修正必须延迟一行处理，否则顺序错位导致修正丢失。replace_all 少计是已知边界，用「LOC 可信度」指标暴露，而不是偷偷补偿。
- **辅助用量别硬并**：omp `model_usage`、Pi 的 `compaction.usage` 等主对话外用量，并入会破坏 audit 的 `turn_stats_match_total`/`all_models_priced`，需先加辅助用量字段再谈。
- **别为了「数字好看」换口径**：Claude `turn_duration` 是整轮墙钟，不能用来算输出吞吐（`output_tps` 对 Claude 显示「不适用」）；`totalCostUSD` 取会话内最大值而非末条/求和（实测有非单调样本）。「不适用」比一个误导性的数字诚实。
- **脆弱的反解不要用**：Claude 项目目录编码把 `.`/空格折叠成 `-` 有损，不能从 key 反解 cwd，必须读会话 meta；读不到就不合并（宁可不合并，不错合并）。
- **格式漂移要有基线**：各源版本基线记在 `#version-baseline`，升级时先对照再改；无官方 schema 的源（WorkBuddy）必须在文档里声明「字段假设未经上游核实」。
- **大小写/路径折叠**：Windows 盘符大小写会让同一项目出现两个目录，`project_hash_key` 在根内 casefold 折叠，跨根不合并。

