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
