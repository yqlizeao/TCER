# TCER — Token-to-Code Efficiency Ratio 项目

## 项目目标

基于真实 Claude Code session 数据，构建多维 AI 编程效率计量体系（TCER/CTEI），其理论框架为原创的五层指标体系（详见下文公式与各模块实现）。

核心指标：TCER（LOC/Mt）、NCPI、CHR、CPE、I/O Ratio，以及综合指数 CTEI。

---

## Claude Code Session 数据获取方式

### 本地文件路径

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

### Session 元数据（`sessions/<pid>.json`）

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

### 对话数据格式（`projects/<hash>/<sessionId>.jsonl`）

JSONL 格式，每行一个 JSON 对象。主要 `type` 字段：

| type | 说明 | 关键字段 |
|------|------|----------|
| `user` | 用户消息 | `message.content[].text`, `timestamp`, `sessionId` |
| `assistant` | 助手回复（★含 token 用量） | `message.model`, `message.usage`, `message.content[]` |
| `tool_use` | 工具调用 | `message.content[].name`, `message.content[].input` |
| `tool_result` | 工具结果 | `message.content[].content` |
| `thinking` | 思考/推理内容 | `message.content[].thinking` |
| `system` | 系统消息 | — |
| `queue-operation` | 入队/出队事件 | `operation` (enqueue/dequeue) |
| `ai-title` | 自动生成的 session 标题 | — |
| `attachment` | 附件 | — |
| `file-history-snapshot` | 文件变更快照 | — |

### Token 用量字段（★核心数据）

每条 `assistant` 类型消息的 `message.usage` 字段：

```json
{
  "input_tokens": 2,
  "cache_creation_input_tokens": 43447,
  "cache_read_input_tokens": 0,
  "output_tokens": 1021,
  "server_tool_use": {
    "web_search_requests": 0,
    "web_fetch_requests": 0
  },
  "service_tier": "standard",
  "cache_creation": {
    "ephemeral_1h_input_tokens": 0,
    "ephemeral_5m_input_tokens": 43447
  }
}
```

**字段说明：**
- `input_tokens` — 非缓存输入 token 数
- `cache_creation_input_tokens` — 本次写入缓存的 token 数（缓存写入，单价 $3.75/MTok）
- `cache_read_input_tokens` — 从缓存读取的 token 数（缓存读取，单价 $0.30/MTok）
- `output_tokens` — 输出 token 数（单价 $15.00/MTok）
- `cache_creation.ephemeral_5m_input_tokens` — 5分钟 TTL 缓存写入量
- `cache_creation.ephemeral_1h_input_tokens` — 1小时 TTL 缓存写入量

---

## 指标计算公式

### 基础指标

| 指标 | 公式 | 说明 |
|------|------|------|
| **TCER** | `净增LOC / 总token消耗(Mt)` | Token转码效率比，单位 LOC/Mt |
| **CHR** | `cache_read / (input + cache_write + cache_read)` | 缓存命中率 |
| **TotalInput** | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` | 总输入 token |
| **TotalTokens** | `TotalInput + output_tokens` | 总 token 消耗 |
| **$/Mt** | `cost / TotalTokens(Mt)` | 每百万 token 实付成本 |
| **CPE** | `cost / 净增LOC × 1000` | 有效千行代码成本 |
| **NCPI** | `当日净增LOC / 累计总LOC` | 净代码产出指数 |
| **I/O Ratio** | `TotalInput / output_tokens` | 输入输出比 |

### 成本估算（按模型计价，源自 cc-switch）

成本不再一律按 Claude 计价。每模型的 `$/MTok` 价表来自 cc-switch 的 `seed_model_pricing()`（厂商官方 list 价，~160 个模型），落盘为可手改配置：

- **配置文件**：`tcer/src/tcer/data/model_pricing.json`（`_meta` 记录来源/抓取日期；`default` 为未知模型回退价；`models` 按 model_id 索引，字段 `input/output/cache_read/cache_write`）
- **加载与解析**：`tcer/src/tcer/pricing.py` —— `resolve(model)` 按「精确 id → 最长前缀 id（兼容 Claude Code 追加的 `[1m]`/日期后缀）→ default」解析；`label(model)` 返回友好名；`default_pricing()` 返回回退价
- **逐模型计价（含混用会话）**：`reader` 在累加时按 `message.model` 分桶（`TokenUsage.per_model`，无 model 记为 `""`），`merge` 自动合并分桶（subagent 折叠、session 聚合都不丢）。`metrics.cost_usd(u)` 对每个分桶用各自价表算成本再相加，**混用多模型的会话也精确**；`metrics.cost_by_model(u)` 给出逐模型成本明细（JSON 导出含 `cost_by_model` 字段）。`cost_usd(u, model=...)` 可强制全部按某模型计价；无分桶的合成 usage 回退到「单一模型→default」
- **扩展**：后续新增模型只需编辑该 JSON 的 `models`，无需改代码

**版本溯源（2026-06-19 抓取）**：源数据是 cc-switch `schema.rs` 的 `seed_model_pricing()` 数组（元组顺序 `(model_id, display_name, input, output, cache_read, cache_creation)`，价格存为 Decimal 字符串）。我们抓的 `main` 分支是最新 release **v3.16.3 的严格超集**：v3.16.3 含 159 个模型，main 与之逐字一致、零价差，另多带一个未发布的 `glm-5.2`，共 **160** 个。cc-switch 的 `repair_current_model_pricing()` 修复表只为老库迁移，seed 数组已是修正后当前价，无需叠加。

**刷新方式**：重新抓 `https://raw.githubusercontent.com/farion1231/cc-switch/main/src-tauri/src/database/schema.rs`，正则提取 `let pricing_data = [ ... ];` 区间的 6 元组，转 JSON 覆盖 `models`（字段映射：`cache_creation` → `cache_write`）。

```python
# default（Anthropic 通用 list 价）回退，等价于历史公式：
cost = (
    input_tokens * 3.00 / 1_000_000 +
    cache_creation_input_tokens * 3.75 / 1_000_000 +
    cache_read_input_tokens * 0.30 / 1_000_000 +
    output_tokens * 15.00 / 1_000_000
)
```

### 高级指标

- **CAF（缓存调整因子）**：`CAF = TotalInput / (input_tokens + cache_creation_input_tokens)`，消除缓存对效率比较的影响
- **TTAF（任务类型调整系数）**：新功能=1.00, 功能扩展=0.85, 调试=0.40, 重构=0.50, 代码审查=0.20, 测试编写=0.90（以原始框架 §6.4 为准）
- **PSAC（项目阶段调整系数）**：基于代码库规模的线性回归，每增 1000 LOC 预期 TCER 下降 0.866
- **TA-TCER**：`TCER / TTAF`，任务调整后的 TCER
- **CTEI**：多维综合指数，归一化后的可横向对比分数

---

## 两种数据获取架构（关键决策）

通过对 Langfuse 与 cc-switch 源码的实地分析，确认存在**两条截然不同的技术路线**。我们的 TCER 项目应优先采用路线 A（离线文件解析），可选叠加路线 B（实时代理）以获取更精确的成本。

### 路线 A：本地 JSONL 离线解析（推荐起步）

直接读取 Claude Code 写入磁盘的 session 文件，**零侵入、无需埋点、可回溯历史**。

**数据源**：`~/.claude/projects/<project-hash>/*.jsonl` + `~/.claude/projects/<project-hash>/<sessionId>/subagents/*.jsonl`

**优点**：实现简单、无 API 调用、能处理所有历史 session、不改变 Claude Code 行为
**局限**：只能事后统计，无法实时；token 用量依赖 `message.usage` 字段是否被记录（部分轻量回复可能 usage=0）

### 路线 B：API 代理实时拦截

在 Claude Code 与上游模型 API 之间架设本地代理（localhost），所有请求/响应经过代理时实时抽取 usage 并入库。

**数据源**：代理截获的 HTTP 请求/响应流（含完整 `usage` 对象、流式 token 计数）

**优点**：实时、可跨供应商（Claude/Codex/Gemini）统一计量、能捕获文件中可能缺失的字段
**局限**：需要让 Claude Code 走代理（配置 `ANTHROPIC_BASE_URL` 指向 localhost），改变运行环境；代理本身的 header 保真/流式解析工程量较大

---

## 现有工具源码分析

### cc-switch（farion1231/cc-switch）— 同时实现两条路线

Tauri(Rust) + React 桌面应用，是参考价值最高的样本。关键代码位于 `src-tauri/src/session_manager/`。

**① 会话浏览（路线 A）— `providers/claude.rs`**

```rust
// 入口：定位 Claude 配置目录下的 projects
let root = get_claude_config_dir().join("projects");
collect_jsonl_files(&root, &mut files);   // 递归收集所有 .jsonl
for path in files { parse_session(&path); }
```

核心解析技巧（可直接借鉴）：

| 函数 | 作用 | 实现要点 |
|------|------|----------|
| `read_head_tail_lines(path, 10, 30)` | 只读首 10 行 + 末 30 行提取元数据 | 大文件（>16KB）用 `Seek` 跳到末尾 16KB 读 tail，避免全文扫描；小文件一次读完 |
| `parse_session` | 提取 sessionId / cwd / 首条用户消息(作为标题) | 跳过 `is_agent_session`（subagent 不算主会话）；过滤 `<local-command-caveat>` 和 `/clear` 等命令噪声 |
| `load_messages` | 逐行解析对话 | 跳过 `isMeta: true` 的行；把「content 全是 tool_result 的 user 消息」重分类为 `tool` 角色 |
| `extract_text` | 兼容多种 content 格式 | 处理 `String` / `Array` / `Object`；`tool_use` 显示为 `[Tool: name]`；递归取 `text` / `input_text` / `output_text` |
| `parse_timestamp_to_ms` | 归一化时间戳 | 自动判别 ms(>1e12) / s / RFC3339 字符串三种格式 |

**结论（对我们最重要）**：cc-switch 的会话浏览**完全不解析 `message.usage` token 字段**——它只关心对话内容的展示。token/成本计量走的是另一条路（见下）。

**② 用量成本追踪（路线 B）— `proxy/` 模块 + 用量仪表盘**

"用量仪表盘"功能跨供应商追踪支出、请求数、Token 用量，依赖内置本地代理（`proxy/` 目录）拦截 API 流量。该模块工程极重（用 hyper 原始写 TCP/TLS 以保真 header 大小写、CONNECT 隧道穿透上游代理、手动 gzip/br 解压流式响应），**对 TCER 起步阶段不建议复刻**，但验证了"代理截获 usage"路线的可行性。

### token-stats（clawlabz/token-stats）— 纯路线 A，最适合起步

- **方式**：直接读取 `~/.claude/projects/*/` 下的 JSONL，提取 `assistant` 消息的 `usage`
- **实现**：纯 Python stdlib，无外部依赖，作为 Claude Code slash 命令安装到 `~/.claude/scripts/token-stats.py`
- **计算**：`CacheHit% = cache_read / (input + cache_write + cache_read)`
- **定价**：input $3.00 / output $15.00 / cache_write $3.75 / cache_read $0.30 per MTok（API 牌价，非订阅实际计费）
- **特点**：零 API 调用，纯本地文件解析——**与 TCER 第一步实现路径完全一致**

### Langfuse — 纯路线 B（应用层埋点）

- **方式**：通过 Anthropic Python SDK 的 `@observe()` 装饰器拦截 API 调用（应用层拦截，**不读本地文件**）
- **配置**：需设置 `LANGFUSE_SECRET_KEY`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_HOST`
- **MCP Server**：`langfuse/mcp-server-langfuse` **仅提供 prompt 管理功能，完全不读取 session 数据**（已源码确认）
- **限制**：需要在应用代码中埋点，无法直接用于 Claude Code CLI/IDE 的已有 session；对历史数据无能为力

### Claude Code Hooks（轻量实时采集，路线 A+B 折中）

在 `settings.json` 中配置，可在特定事件点运行自定义脚本，stdin 收 JSON、stdout 回 JSON：

| Hook | 触发时机 | 用途 |
|------|----------|------|
| `PreToolUse` | 工具调用前 | 记录/审批工具调用 |
| `PostToolUse` | 工具调用后 | 日志记录、后处理 |
| `Stop` | 助手即将停止时 | 记录 session 结束、注入反馈 |
| `SubagentStop` | subagent 即将停止时 | subagent 日志 |
| `Notification` | 发送通知时 | 通知记录 |

Hook 适合做增量补充（如每次 Stop 时把当轮 usage 追加写入自己的数据库），但无法拿到完整 usage 对象（Hook 入参里通常不含 token 计数），因此**不能替代**路线 A 的文件解析，只能配合。

---

## 实现路径

### 第一步：数据读取层（参照 token-stats + cc-switch `claude.rs`）
1. 遍历 `~/.claude/projects/<project-hash>/*.jsonl`（递归含 `subagents/`）
2. 逐行解析 JSON，过滤 `isMeta: true`、跳过 `queue-operation` 等非对话行
3. 提取 `assistant` 消息的 `message.usage`（input/output/cache_creation/cache_read tokens）与 `message.model`
4. 效率优化：列表页只需元数据时，仿 cc-switch `read_head_tail_lines` 只读首尾若干行

### 第二步：指标计算层
汇总每个 session 的 token 用量，计算 TCER、CHR、$/Mt、I/O Ratio 等指标。

### 第三步：代码变更采集（git-free，已实现）
**净增 LOC 来自会话自身的文件改写工具调用**，不依赖 git：逐条回放 `Write` / `Edit` / `MultiEdit` / `NotebookEdit` 的 `input`（Write 的 `content`、Edit 的 `old_string`/`new_string`），累加 added/deleted。优点：零外部依赖、**per-session 精确归因**（不再有"提交落在时间窗间隙"问题）、忠实反映模型真实生成量（含多次重写）。累计代码库总量（NCPI/PSAC 用）改为直接遍历工作区目录数行（`loc.tree_loc`，跳过 `.git`/`node_modules`/`__pycache__` 等）。

> 历史方案曾用 `git log --numstat`，已废弃：git 净增只反映最终提交、受提交习惯影响、且时间窗归因不可靠。`file-history-snapshot.trackedFileBackups` 实测常为空，亦不可用。

### 第四步：综合指数
实现 CTEI 公式，支持任务类型标记和项目阶段调整。

---

## 关键注意事项

1. **数据隐私**：session JSONL 包含完整对话内容和代码片段，注意脱敏处理
2. **缓存价格差异**：cache read ($0.30/MTok) 仅为 input ($3.00/MTok) 的 1/10，CHR 对成本影响极大
3. **session 持久性**：JSONL 文件在 session 结束后保留，可通过 `claude --resume` 恢复
4. **subagent 数据**：主 session 的 subagent 对话存储在 `<sessionId>/subagents/` 子目录下，文件名形如 `agent-<id>.jsonl`。**本工具的做法**：用 `reader.parent_session_id()` 把每个 subagent 按 `<sessionId>` **并入其父会话**作为同一个 session——token 与 LOC 计入父会话（保留真实成本），但不单独计为一个 session，因此会话数与 cc-switch（用 `is_agent_session()` 排除 subagent）一致。`--no-subagents` 可完全排除 subagent 数据。
5. **跨平台路径**：Windows 下路径为 `%USERPROFILE%\.claude\`，macOS/Linux 为 `~/.claude/`
6. **元数据行过滤**（cc-switch 经验）：解析 JSONL 时要跳过 `isMeta: true` 的行；用户消息里还要过滤 `<local-command-caveat>` 和 `/clear` 等命令噪声，否则会污染"首条用户消息"标题与 prompt 统计
7. **usage=0 的空回复**：部分轻量 assistant 消息（如纯 thinking）`usage` 全为 0，汇总 token 时应跳过或单独标记，避免拉低均值
8. **timestamp 格式不统一**：JSONL 中时间戳可能是毫秒整数（>1e12）、秒整数、或 RFC3339 字符串三种形式，解析需归一化（参照 cc-switch `parse_timestamp_to_ms`）
9. **★ 按 `message.id` 去重（关键）**：一次 assistant API 响应常被拆成多行写入 JSONL——每个 content 块（thinking / text / 每个 tool_use）各占一行，且**每行都重复携带同一份 `message.usage`**（在走 AWS Bedrock 的会话里观测到同一 `message.id` 重复多达 6 次）。若按行累加 usage，token 与回合数会被成倍虚高（实测本地数据集**全局 55.9% 的 token 是重复计数**）。因此 `reader.aggregate_usage` 必须**按 `message.id` 只计一次**（ccusage / token-stats 同此做法）。注意：`tool_use` 块各自唯一、不重复，故 `loc.session_loc` 的代码量统计**不受影响**，无需去重。
