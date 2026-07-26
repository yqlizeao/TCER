# 指标体系

## 基础指标

| 指标 | 公式 | 说明 |
|------|------|------|
| **TCER** | `净增LOC / 总token消耗(Mt)` | Token转码效率比，单位 LOC/Mt |
| **CHR** | `cache_read / (input + cache_write + cache_read)` | 缓存命中率 |
| **TotalInput** | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` | 总输入 token |
| **TotalTokens** | `TotalInput + output_tokens` | 总 token 消耗 |
| **$/Mt** | `cost / TotalTokens(Mt)` | 每百万 token 实付成本 |
| **CPE** | `cost / 净增LOC × 1000` | 有效千行代码成本 |
| **I/O Ratio** | `TotalInput / output_tokens` | 输入输出比 |

## 成本估算（按模型计价）

成本按模型分别计价再求和。每个模型的 token 用各自 `$/MTok` 价表算钱。

价表来源：cc-switch 的 `seed_model_pricing()`（≈177 模型），落盘为 `tcer/config/model_pricing.json`。
解析：`pricing.resolve(model)` 按「精确 → 归一化精确 → 前缀 → 反向前缀 → default」四级匹配（详见 CLAUDE.md 注意事项 3）。
未知模型回退 default（Anthropic 标价 input $3 / output $15 / cache-write $3.75 / cache-read $0.30 每百万 Token）。

```python
cost = (input × r["input"] + cache_write × r["cache_write"] + cache_read × r["cache_read"] + output × r["output"]) / 1_000_000
```

## 高级指标

- **CAF**：`TotalInput / (input + cache_write)`，消除缓存对效率比较的影响
- **TTAF / NTCER**：任务类型调整系数。NTCER = TCER / TTAF。取值：代码创作 1.00 / 代码维护 0.45 / 非编码 0.20
- **CTEI**（三因子）：`(TCER/TCER_baseline) × (CPE_baseline/CPE) × (1 + CHR × 0.5)`
  历史第四因子「产出密度 NCPI/基准」因分母需扫描真实仓库、违背「纯离线仅析会话数据」定位而移除；三因子均可聚合。
- **评级**：优秀 >2 · 良好 1~2 · 中等 0.5~1 · 低效 0.1~0.5 · 极端低效 <0.1
- **Churn（返工率）**：`自返工删除行 / 写入行`。自返工删除行 = 本会话先写入、随后又被自己删除/替换的行数（按文件累计，封顶于「本会话已写入该文件的行数」）；删除会话之外的既有代码属正常编辑，不计入。这样维护/重构任务不会因编辑既有代码而虚高返工率。
- **search_edit_ratio（搜索后编辑比）**：Grep/Glob 调用中，3 回合内发生 Write/Edit 的占比。**按回合就近匹配，不绑定具体文件**——真实 Grep/Glob 的 `path` 多为目录或缺省（仓库级搜索），按文件匹配不可靠。

> CTEI 三因子化后**单会话与项目聚合均有效**（旧版因 NCPI 分母语义在聚合层失效而禁用，已随 NCPI 移除一并解除）。

> ⚠️ **比率类指标的 Bash 盲区**：`read_write_ratio` / `exploration_ratio` 只统计 Read/Grep/Glob 等专用工具；Claude Code 中大量阅读与搜索经 `Bash`（cat/rg/find）完成，不计入，故这两项实测普遍低于直觉，仅作粗略趋势参考。


基准默认值：TCER=76.59, CPE=8.22（来自原始框架 16 会话参考数据集）。可通过 `tcer/config/composite_baselines.json` 覆盖。

## 扩展指标（数据源深挖信号）

以 `metric_defs.GROUPS` 为准；「不适用」= 该数据源不产生此字段（见 `_SOURCE_SUPPORT`）。

| 指标 | 公式 / 来源 | 源 |
|------|------------|-----|
| **Bash 占比** | Bash+PowerShell ÷ 总工具调用（比率盲区暴露面） | 全部 |
| **首次编辑回合** | min(Write/Edit 的回合号)+1；None=未动手 | 全部 |
| **改后验证率** | Write/Edit 后 3 回合内出现 Bash 的占比 | 全部 |
| **一次写对率** | 只编辑 1 次的文件 ÷ 被编辑文件总数 | 全部 |
| **人工修正** | `toolUseResult.userModified=true` 计数 | Claude |
| **回退事件 / 回退行** | OpenCode `revert` / Grok `hasReverted`、`agentLines*Reverted` | OpenCode/Grok |
| **落地提交** | Grok `signals.gitCommitCount`（PR 数在导出里） | Grok |
| **用户取消 / 重新生成** | Grok `cancellationCount` / `regenerationCount` | Grok |
| **用户评价** | Grok `positiveRatings` / `negativeRatings` | Grok |
| **审批等待** | Σ `permission_resolved.wait_ms`（括号内为请求次数） | Grok |
| **输出间隔 P50/P99** | Grok `itlP50Ms` / `itlP99Ms`（inter-token latency） | Grok |
| **首字延迟 P95** | 逐回合 TTFT 样本的 P95 | Codex |
| **配额峰值** | max(primary/secondary `used_percent`)/100 | Codex |
| **钩子开销** | Σ `stop_hook_summary.hookInfos[].durationMs`（次数/失败数） | Claude |
| **排队输入** | `queue-operation` 行计数 | Claude |
| **限流命中** | Claude 429 `api_error` 行 / Codex 限流快照 | Claude/Codex |
| **网页搜索** | Claude `server_tool_use` / Codex·Grok 事件 | Claude/Codex/Grok |

导出独有（不进指标网格）：`patch_diff_added/deleted`（structuredPatch 独立差分，交叉校验回放 LOC）、`source_reported_cost_usd`（OpenCode 自报成本，交叉校验计价）、`mcp_calls_by_attr`（MCP 精确归因）、`abort_reasons`。

**成本计价补充**：Claude 缓存写按 TTL 分档——1h 档单价 = 5m 档 × 1.6（`CACHE_1H_PREMIUM`），子集来自 `usage.cache_creation.ephemeral_1h_input_tokens`。

**模型对比分摊**：`compare_models` 的产出/行为/质量指标按「该模型在会话内的 token 占比」加权分摊；单模型会话权重 = 1.0，混合模型会话按占比拆分（不再整段丢弃）。

## 科学计算步骤

### 第 1 步 · 采集与清洗（`reader.iter_messages`）
1. 递归收集 `~/.claude/projects/<项目哈希>/**/*.jsonl`（含 subagents/）
2. 逐行解析 JSON，跳过 `isMeta: true`、`queue-operation`
3. 主会话与子代理按目录区分

### 第 2 步 · Token 用量聚合（`reader.aggregate_usage`）
遍历 `type: assistant` 消息的 `message.usage`，对四个计费字段求和。整条全为 0 的回复跳过。

> ★ 按 `message.id` 去重：一次 API 响应常被拆成多行写入 JSONL，每行重复携带 usage。必须按 message.id 只计一次，否则 token 成倍虚高。

### 第 3 步 · 成本估算（`metrics.cost_usd`）
按模型分别计价再求和。

### 第 4 步 · 代码量统计（`loc.session_loc`）
逐条回放文件改写工具调用（Write/Edit/MultiEdit/NotebookEdit），统计增删。

### 第 5 步 · 基础效率指标（`metrics.compute`）
TCER, CHR, I/O Ratio, $/Mt, CPE。

### 第 6 步 · 修正指标
CAF, churn, TA-TCER。

### 第 7 步 · CTEI 综合指数（`metrics.ctei`）

## 指标健康参考范围（经验值）

| 指标 | 优秀 | 良好 | 需改进 | 说明 |
|------|------|------|--------|------|
| **TCER** | >80 | 40–80 | <40 | 基准中位数 76.59 |
| **CHR** | >85% | 70–85% | <70% | 缓存命中率 |
| **CPE** | <$10 | $10–$30 | >$30 | 每千行成本 |
| **Churn** | <5% | 5–15% | >15% | 返工率 |
| **I/O Ratio** | 150–250 | 80–150 | <80 或 >300 | 输入输出比 |

## 数值算例

假设某会话：input=20,000 / cache_write=180,000 / cache_read=4,700,000 / output=100,000；工具调用净增 added=420 / deleted=20；任务类型 code_creation。

| 步骤 | 计算 | 结果 |
|------|------|------|
| total_input | 20,000+180,000+4,700,000 | 4,900,000 |
| total | 4,900,000+100,000 | 5,000,000 |
| cost | (20k·3 + 180k·3.75 + 4.7M·0.30 + 100k·15)/1e6 | $3.645 |
| net_loc | 420 − 20 | 400 |
| TCER | 400 / 5.0 | 80.0 LOC/Mt |
| CHR | 4,700,000 / 4,900,000 | 95.9% |
| CPE | 3.645 / 400 × 1000 | $9.11/千行 |
| CAF | 4,900,000 / 200,000 | 24.5 |
| churn | 20 / 420（此例假设 20 行删除全为自返工；实际以 rework_deleted 为分子，缺失时才回退用 deleted） | 4.8% |
| CHR_factor | 1 + 0.959×0.5 | 1.480 |
| **CTEI** | (80/76.59)×(8.22/9.11)×1.480 | **≈ 1.39 → 良好** |
