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

启动 Claude Code 时传 `CLAUDE_CONFIG_DIR=%USERPROFILE%\.zclaude` 会让它改用 `.zclaude`（结构同 `.claude`）存数据，常用于不污染 `.claude`。该环境变量只在 Claude 进程内，TCER 读不到，因此 TCER 用**结构指纹**自动发现：以规范目录（`CLAUDE_CONFIG_DIR` 或 `~/.claude`）为锚，扫描其父目录下所有含 `projects/<hash>/*.jsonl` 的兄弟目录，全部当作 Claude 根。`discover_jsonl(hash)` 与 `list_projects()` 跨所有根查找，**同一项目 hash 在多个配置目录里的会话合并**；只存在于自定义目录的项目也会出现。无需任何手动配置。

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

JSONL 格式，每行一个 JSON 对象。主要 `type` 字段：

| type | 说明 | 关键字段 |
|------|------|----------|
| `user` | 用户消息 | `message.content[].text`, `timestamp`, `sessionId` |
| `assistant` | 助手回复（★含 token 用量） | `message.model`, `message.usage`, `message.content[]` |
| `tool_use` | 工具调用 | `message.content[].name`, `message.content[].input` |
| `tool_result` | 工具结果 | `message.content[].content` |
| `thinking` | 思考/推理内容 | `message.content[].thinking` |
| `ai-title` | 自动生成的 session 标题 | — |

## Token 用量字段

每条 `assistant` 消息的 `message.usage` 字段：

```json
{
  "input_tokens": 2,
  "cache_creation_input_tokens": 43447,
  "cache_read_input_tokens": 0,
  "output_tokens": 1021
}
```

- `input_tokens` — 非缓存输入 token（$3.00/MTok）
- `cache_creation_input_tokens` — 写入缓存（$3.75/MTok）
- `cache_read_input_tokens` — 从缓存读取（$0.30/MTok）
- `output_tokens` — 输出（$15.00/MTok）

## LOC 不依赖 git

净增代码来自会话内文件改写工具调用（Write/Edit/MultiEdit/NotebookEdit），逐条回放统计增删。

- **零外部依赖**：不需要 git，任何文件夹都能算
- **按会话精确归因**：不再有「提交落在时间窗间隙」的误差
- **忠实反映生成量**：计入多次重写/实验，通常大于最终进 git 的净增

代码库累计行数由 `tree_loc` 扫描工作目录得到（跳过 .git/node_modules/__pycache__ 等）。

### F1 风险（Write 覆写已有文件）

`session_loc` 对每个会话从空的 `file_lines={}` 开始。Write 首次遇到某文件时假设原大小为 0——对新文件正确，对覆写已有文件错误。Edit 只看 delta，不受影响。`unseen_writes` 计数是 F1 暴露面上界。

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
  events.jsonl     # 轻量事件：turn_ended(outcome) / tool_completed(duration) / permission_resolved
  signals.json     # 聚合信号（turnCount / toolCallCount / modelsUsed / agentLines*）——仅交叉校验
  rewind_points.jsonl / terminal/*.log / subagents/   # 按需
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

`search_replace` 与 Claude 的 `Edit` 同构（`file_path` / `old_string` / `new_string`），净增 = `new_string` 行数 − `old_string` 行数；`write` 整文件写入计入 `unseen_writes`（同 F1 风险）。无 `search_replace`/`write` 的会话 TCER/CPE/CTEI 显示为 `-`。返工率首版置 0（与 Codex 同款简化；`search_replace` 数据上可后续支持自返工计算）。

