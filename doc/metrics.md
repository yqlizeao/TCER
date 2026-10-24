# 指标体系

> **指标全集以 `TCER/gui/metric_defs.py` 的 `GROUPS` 为准**（名称/提示/单位/格式/好坏方向的唯一真理源）；本文只讲公式、算例与关键口径，不追求覆盖每一个指标。
> 评分体系自 v1.1.0 起为**综合效率分 v2**（三正交轴），旧 CTEI（三因子乘积）与 `ctei()` 已整体移除；参数以 `TCER/config/composite_baselines.json` 为唯一真理源。

## 基础指标

| 指标 | 公式 | 说明 |
|------|------|------|
| **TCER（TCER）** | `净增LOC / 总token消耗(Mt)` | Token 转码效率比，单位 LOC/Mt |
| **CHR** | `cache_read / (input + cache_write + cache_read)` | 缓存命中率 |
| **TotalInput** | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` | 总输入 token |
| **TotalTokens** | `TotalInput + output_tokens` | 总 token 消耗 |
| **$/Mt** | `cost / TotalTokens(Mt)` | 每百万 token 实付成本 |
| **CPE** | `cost / 净增LOC × 1000` | 有效千行代码成本 |
| **I/O Ratio** | `TotalInput / output_tokens` | 输入输出比 |

## 成本估算（按模型计价）

成本按模型分别计价再求和：每个模型的 token 用各自 `$/MTok` 价表算钱。价表 `TCER/config/model_pricing.json`（当前 347 个模型条目；数量以文件为准），
`pricing.resolve(model)` 按「精确 → 归一化精确 → 前缀 → 反向前缀 → default」四级匹配（详见 [#pricing](#pricing)）。未知模型回退 default（Anthropic 标价 input $3 / output $15 / cache-write $3.75 / cache-read $0.30 每百万 Token）。

```python
cost = (input × r["input"] + cache_write × r["cache_write"] + cache_read × r["cache_read"] + output × r["output"]) / 1_000_000
```

Claude 缓存写按 TTL 分档：1h 档单价 = 5m 档 × 1.6（`CACHE_1H_PREMIUM = 0.6` 即溢价 60%），子集来自 `usage.cache_creation.ephemeral_1h_input_tokens`（详见 [#cache-ttl](#cache-ttl)）。

## 高级指标

- **CAF**：`TotalInput / (input + cache_write)`，消除缓存对效率比较的影响
- **TTAF / NTCER**：任务类型调整系数。`ntcer = TCER / TTAF`。取值来自配置 `TASK_CATEGORIES`（当前：代码创作 1.00 / 代码维护 0.45 / 非编码 0.20；`ta_tcer` 为向后兼容别名）
- **Churn（返工率，自返工率）**：`自返工删除行 / 写入行`。自返工删除行 = 本会话先写入、随后又被自己删除/替换的行数（按文件累计，封顶于「本会话已写入该文件的行数」）；删除会话之外的既有代码属正常编辑，不计入。
- **search_edit_ratio（搜索后编辑比）**：Grep/Glob 调用中，3 回合内发生 Write/Edit 的占比。**按回合就近匹配，不绑定具体文件**——真实 Grep/Glob 的 `path` 多为目录或缺省。

> 比率类指标的 Bash 盲区：`read_write_ratio` / `exploration_ratio` 只统计 Read/Grep/Glob 等专用工具；Claude Code 中大量阅读与搜索经 `Bash`（cat/rg/find）完成，不计入，故这两项实测普遍低于直觉，仅作粗略趋势参考。

## 综合效率分 v2（取代 CTEI）

代码入口：`core/metrics.py` 的 `score_axes` / `efficiency_score` / `_half_sat` / `output_axis` / `cost_axis` / `quality_axis` / `_shrink` / `tier`；GUI 构成面板标签取自 `metric_defs.SCORE_AXES`。

**1. 三条正交轴**，各经半饱和变换 `Φ(x)=x/(x+b) ∈ [0,1)`（基准 b 处恰 0.5 = 与基准持平；单调、有界，自动饱和重尾，无需截尾）：

| 轴 | 输入 | 变换 | 基准 b |
|----|------|------|--------|
| 产出 output | `ntcer`（任务归一后每百万 token 净产出） | `x/(x+b)` | `baselines.quillmere`（`SCORE_OUTPUT_BASELINE`） |
| 成本 cost | `cpe`（cost 已含缓存读低价，缓存收益只在此出现一次） | 反向 `b/(x+b)` | `baselines.cpe`（`SCORE_COST_BASELINE`） |
| 质量 quality | 低返工 `1−churn`、低错误 `1−tool_error_rate`、先读后写率 | 子信号加权（`quality_weights`：0.40/0.35/0.25；缺项重分权，全缺→None） | 无（与体量无关） |

**2. 证据收缩**：每轴按证据量 `m = net_loc` 向 0.5 收缩 `w = m/(m+k)`，`ẽ = 0.5 + w·(e−0.5)`，`k = shrink_k = 200`。小会话被拉回中性，无法「写 5 行登顶」。

**3. 加权合成**：`score = 100 × Σ wᵢ·ẽᵢ / Σ wᵢ`，权重 `weights = {output 0.5, cost 0.3, quality 0.2}`；成本/质量轴缺失时把权重重分配给可用轴；**产出轴缺失 → score=None**（排名页回退按 TCER 排名并提示）。

**4. 评分门控**：`net_loc ≤ 0` 的会话三轴一律 None、不评分（实测约 65% 会话零净产，收缩后会被钉在恰好 50 分的常数，伪装成信息）。详见 [#score-gating](#score-gating)。

**5. 评级带**（配置 `score_tiers.bands`，best→worst，顶档严格大于、其余 ≥）：优秀 > 68 · 良好 ≥ 57 · 中等 ≥ 47 · 待改进 ≥ 42 · 低效 < 42。按 133 个真实已评分会话分位划定（≈P90/P70/P20）；`metric_defs._tier_band_hint` 动态生成提示，勿手写阈值。

**6. 聚合有效**：`efficiency_score`/`score_axes` 皆可聚合——聚合分从聚合 TCER/CPE/质量子信号**重算**，不对各会话分数取平均（audit 校验 `aggregate_score_recompute`；server 同口径）。

**7. 基准**：全局 `baselines`（TCER/CPE）+ 逐源 `baselines_per_source` + 逐项目个人基准，解析链 逐项目 > 逐源 > 全局（`resolve_baselines`）。个人基准默认需至少 `MIN_BASELINE_SESSIONS = 5` 条完整会话（且每条净增 ≥ `MIN_BASELINE_NET_LOC = 20` 行）。`efficiency_score()`/`score_axes()`/`compute()` 的基准参数默认 `None` → 调用时读实时全局（不可写成 `=TCER_BASELINE`，def 时冻结会令保存基准后分数直到重启才更新）。
切勿在代码/文档里硬编码具体基准数字（历史上散落旧值导致雷达图刻度失真）；当前配置值请直接读 `composite_baselines.json`。

> 旧 CTEI（`(TCER/基准)×(CPE基准/CPE)×(1+CHR×0.5)`）被移除的原因：两个共线比率相乘约等于 loc/tok 平方，方差放大、重尾需 P90 截尾；缓存收益在 CPE 与 CHR 因子中被双重计入；且完全忽略质量。`chr_weight` 配置键现仅作历史字段保留。

## 扩展指标（数据源深挖信号，节选）

以 `metric_defs.GROUPS` 为准；「不适用」= 该数据源不产生此字段（见 `_SOURCE_SUPPORT`）。

| 指标 | 公式 / 来源 | 源 |
|------|------------|-----|
| **Bash 占比** | Bash+PowerShell ÷ 总工具调用（比率盲区暴露面） | 全部 |
| **首次编辑回合** | min(Write/Edit 的回合号)+1；None=未动手 | 全部 |
| **改后验证率** | Write/Edit 后 3 回合内出现 Bash 的占比 | 全部 |
| **一次写对率** | 只编辑 1 次的文件 ÷ 被编辑文件总数 | 全部 |
| **人工修正** | `toolUseResult.userModified=true` 计数 | Claude |
| **回退事件** | OpenCode `session.revert` | OpenCode |
| **首字延迟** | 逐回合 TTFT 样本的**中位数**（无样本回退会话最小值；字段 `time_to_first_token_ms` 语义仍是 min） | Codex / omp |
| **首字延迟 P95** | 逐回合 TTFT 样本的 P95 | Codex / omp |
| **估算输出速度** | Σ合格响应输出 ÷ Σ合格响应估算耗时（含首字等待；见 [#est-speed](#est-speed)） | Claude/Codex/OpenCode/omp/pi |
| **输出吞吐** | Σ输出 ÷ Σ单次补全自报 duration（不设门槛，不改口径） | omp / pi |
| **配额峰值** | max(primary/secondary `used_percent`)/100 | Codex |
| **钩子开销** | Σ `stop_hook_summary.hookInfos[].durationMs`（次数/失败数） | Claude |
| **排队输入** | `queue-operation` 行计数 | Claude |
| **限流命中** | Claude 429 `api_error` 行 / Codex 限流快照 / omp 429 | Claude/Codex/omp |
| **网页搜索** | Claude `server_tool_use` / Codex 事件 | Claude/Codex/omp/pi |

导出独有（不进指标网格）：`patch_diff_added/deleted`（structuredPatch 独立差分，交叉校验回放 LOC）、`source_reported_cost_usd`（自报成本，交叉校验计价）、`mcp_calls_by_attr`（MCP 精确归因）、`abort_reasons`。

**模型对比分摊**：`compare_models` 的产出/行为/质量指标按「该模型在会话内的 token 占比」加权分摊；单模型会话权重 = 1.0（详见 [#compare-models](#compare-models)）。

## 科学计算步骤

1. **采集与清洗**（`reader.iter_messages`）：递归收集会话 JSONL（含子代理），逐行解析，跳过 `isMeta: true`、`queue-operation`；主会话与子代理按目录区分。
2. **Token 用量聚合**（`reader.aggregate_usage` / `scan_session`）：遍历 assistant 消息 `message.usage` 四个计费字段求和，**按 `message.id` 去重**（一次响应常被拆成多行重复携带 usage；详见 data-format.md#dedup），整条全 0 的回复跳过。
3. **成本估算**（`metrics.cost_usd`）：逐模型计价再求和，缓存写按 TTL 分档。
4. **代码量统计**（`loc.session_loc` / `scan_session`）：回放文件改写工具调用（Write/Edit/MultiEdit/NotebookEdit），按结果裁决，统计增删。
5. **基础效率指标**（`metrics.compute`）：TCER, CHR, I/O Ratio, $/Mt, CPE。
6. **修正指标**：CAF, churn, NTCER。
7. **综合效率分**（`metrics.efficiency_score`）：三轴 → 收缩 → 加权合成 → `tier()` 评级（见上节）。

## 指标健康参考范围（经验值）

| 指标 | 优秀 | 良好 | 需改进 | 说明 |
|------|------|------|--------|------|
| **TCER** | >80 | 40–80 | <40 | 经验带，与配置基准独立（基准仅用于综合效率分） |
| **CHR** | >85% | 70–85% | <70% | 缓存命中率 |
| **CPE** | <$10 | $10–$30 | >$30 | 每千行成本 |
| **Churn** | <5% | 5–15% | >15% | 返工率 |
| **I/O Ratio** | 150–250 | 80–150 | <80 或 >300 | 输入输出比 |

## 数值算例

假设某会话：input=20,000 / cache_write=180,000 / cache_read=4,700,000 / output=100,000；净增 added=420 / deleted=20；任务类型 code_creation（TTAF=1.0）。演示基准取 b_out=26.22、b_cost=13.62（仅为演示，实际以配置为准）。

| 步骤 | 计算 | 结果 |
|------|------|------|
| total_input | 20,000+180,000+4,700,000 | 4,900,000 |
| total | 4,900,000+100,000 | 5,000,000 |
| cost | (20k·3 + 180k·3.75 + 4.7M·0.30 + 100k·15)/1e6 | $3.645 |
| net_loc | 420 − 20 | 400 |
| TCER | 400 / 5.0 | 80.0 LOC/Mt |
| CHR | 4,700,000 / 4,900,000 | 95.9% |
| CPE | 3.645 / 400 × 1000 | $9.11/千行 |
| churn | 20 / 420（假设删除全为自返工） | 4.8% |
| 产出轴 | Φ(80) = 80/(80+26.22) | 0.753 |
| 成本轴 | 13.62/(9.11+13.62) | 0.599 |
| 质量轴 | 假设工具错误率 5%、先读后写 0.8：0.4·0.952+0.35·0.95+0.25·0.8 | ≈0.913 |
| 收缩 | w = 400/(400+200) = 0.667；ẽ=0.5+w·(e−0.5) | 产出 0.669 / 成本 0.566 / 质量 0.775 |
| **综合效率分** | 100 × (0.5·0.669 + 0.3·0.566 + 0.2·0.775) | **≈ 65.9 → 良好** |

> 本算例已用 `metrics.score_axes` / `metrics.efficiency_score` 以同样输入实算核对（2026-10）：三轴 0.6688 / 0.5661 / 0.7756，综合 65.93，评级「良好」。

## 实现不变量详述（自 CLAUDE.md 迁移）

> 本节自 CLAUDE.md 迁移而来（2026-10 瘦身）：CLAUDE.md 只保留一行硬规则，完整细节、实测数据与历史缘由在此。条目前缀「原 #N」= 瘦身前 CLAUDE.md 的「关键注意事项」编号，可据此追溯。

<a id="pricing"></a>
### 原 #3 逐模型计价

- TokenUsage.per_model 按 message.model 分桶，混用多模型会话也精确。价表 `tcer/config/model_pricing.json`（≈350 模型）。
  - **定价口径**：
  - ①录厂商**标准价**（非限时促销/介绍价，无时间相关性）；
  - ②峰谷分时录**闲时档**；
  - ③促销价/峰时价/Batch/Priority 等多轨与同模型分段计费记入条目 `_note`（`pricing.note_for()` 读取，`views._model_price_tip` 悬浮展示，让四个单价始终说明自己取的是哪一轨）。同步数据源：cc-switch `schema.rs` 为 SSOT + models.dev `api.json`（https://models.dev/api.json，更新最快、cc-switch 自身也用它审计，但双方都各有过错误记录，**价差必须厂商官方页裁决**）。
  - **四级匹配**（`pricing._match_id`，按优先级）：
  - ①精确 ②归一化精确（小写、去 `-`/`_`、`5p2`→`5.2` 且 `5-6`→`5.6`，先于前缀以防 `glm-5p2` 误中 `glm-5`、`gpt-5-6-sol` 误中 `gpt-5`）③前缀（`claude-opus-4-8[1m]`→`claude-opus-4-8`）④反向前缀（短名 `claude-opus-4-5`→带日期 key）。每条先试原 id 再试末段 path（剥 `z-ai/`、`accounts/fireworks/models/` 等供应商前缀）。`pricing.normalize()` 把 per_model key 归一化到价表规范 key；`pricing.table_key()` 返回 None 即走 default 回退（GUI 价表浮窗据此标"默认配置价"）。匹配候选含 path 尾段与 **mode 后缀剥离**（`-thinking`/`-reasoner`，如 `claude-opus-4-5-thinking`→`claude-opus-4-5-20251101`；不剥 `-high`/`-reasoning` 以免误绑）。`pricing.unmatched_models` / `metrics.unmatched_pricing_models` 列出回退模型；状态栏与模型/成本弹窗提示。

<a id="cache-ttl"></a>
### 原 #16 缓存写 TTL 分档计价

- Claude `usage.cache_creation.ephemeral_1h_input_tokens` 是 1h 缓存写子集（单价 2×input，5m 为 1.25×input）。`TokenUsage.cache_write_1h_tokens` 记录该子集，`metrics._cost_from` 按 `CACHE_1H_PREMIUM=0.6` 加溢价（价表 cache_write 视为 5m 率）。实测本机 60% 缓存写走 1h 档——漏掉会系统性低估成本。

<a id="temporal"></a>
### 原 #6 时序分析

- `ToolOp(turn, tool, path)` 记录每个工具调用的回合序号和文件路径。
  - **搜索后编辑比**按回合就近匹配（搜索后 3 回合内出现 Write/Edit 即算跟进，不绑定具体文件——真实 Grep/Glob 的 `path` 多为目录）；
  - **先读后写率**等仍用 file_path。merge 时 rebase turn 编号保证聚合后时序连续。

<a id="task-type"></a>
### 原 #7 任务类型体系

- 3 大类（代码创作/代码维护/非编码），每类有 TTAF 系数。`ntcer = tcer / ttaf` 归一化后可跨任务类型公平比较。`ta_tcer` 保留为向后兼容别名。

<a id="task-ssot"></a>
### 原 #11 任务类型 SSOT

- `TASK_CATEGORIES` / TTAF 只来自 `config/composite_baselines.json`（`metrics._refresh_composite_globals`）。分析入口默认 `code_creation`；`resolve_task_type` 把空值/未知/`feature` 等合法化，`coerce_task_type` 给公式层（未知→None，不静默套创作系数）。`task_type=auto`（GUI「自动」）按会话 `infer_task_type`（net_loc/探索比/Edit 比/读写比）推断，聚合取众数。个人基准默认至少 `MIN_BASELINE_SESSIONS=5` 条完整会话（且每条净增 ≥ `MIN_BASELINE_NET_LOC=20` 行）。
  - **`efficiency_score()`/`score_axes()`/`compute()` 的 `tcer_baseline`/`cpe_baseline` 参数默认 `None`→调用时读实时全局**（不可用 `=TCER_BASELINE` 默认值：默认参数在 def 时冻结，`save_baselines` 重绑全局后会用陈旧基准，分数直到重启才更新）。

<a id="rework"></a>
### 原 #8 返工率 = 自返工率

- churn 只计「本会话先写入、随后又被自己删除/替换」的行（`loc.SessionLoc.rework_deleted`，封顶于本会话已写入该文件的行数）；删除会话之外的既有代码属正常编辑，不计入。

<a id="high-churn"></a>
### 原 #13 high_churn 合并

- 子代理折叠/项目聚合用 `loc.merge_session_locs`，按合并后的 `file_edit_counts` 重算 `high_churn_files`（同路径不重复计）。

<a id="score-v2"></a>
### 原 #9 综合效率分 v2（三正交轴，聚合有效）

- 替代旧 CTEI（旧式 `(TCER/基准)×(CPE基准/CPE)×(1+CHR×0.5)` 把两个共线比率相乘≈对 loc/tok 平方，方差放大、重尾需 P90 截尾、缓存双重计数、忽略质量——已整体移除）。新式 `score = 100 × Σ权重·收缩(半饱和(轴))`，三条**正交**轴各经半饱和变换 `Φ(x)=x/(x+b)∈[0,1)`（基准 b 处=0.5 中性，天然有界、自动饱和重尾，**无需截尾**）：
  - ①**产出**用 ntcer(任务归一)、以 TCER 基准为 b；
  - ②**成本**用 cpe(cost 已含缓存读低价，缓存收益只在此出现一次)、反向半饱和 `b/(x+b)`、以 CPE 基准为 b；
  - ③**质量**由返工率↓/工具错误率↓/先读后写↑ 加权（与体量无关，子信号缺失则重分权到可用项，全缺→None）。各轴再按证据量 net_loc 向 0.5 **收缩** `w=m/(m+k)`（小会话拉回中位，防「写 5 行登顶」），加权算术平均（`SCORE_WEIGHTS`）合成 0–100。产出轴缺失→score=None（回退 TCER 排名）。`efficiency_score`/`score_axes` 皆可聚合，audit 校验 `aggregate_score_recompute`。config 块 `score_model`（权重/收缩常数/质量子权重）+ `score_tiers`（评级带）+ 共享 `baselines`（TCER/CPE，个人基准一改两轴即生效）`SessionReport.score`/`tier`/`score_output_axis`/`score_cost_axis`/`score_quality_axis`。

<a id="score-gating"></a>
### 原 #36 评分门控与评级带校准（v1.7.1）

- ①零/负净产（net_loc≤0）会话**不评分**——`metrics.score_axes` 直接返回三轴 None（实测 65% 会话零净产，证据收缩把它们全钉在恰好 50 分的「中等」，常数分伪装信息；排名页/insights 对 None 已有兜底）；
  - ②`score_tiers.bands` 按 133 个真实已评分会话分位重划（优秀>68≈P90 · 良好≥57≈P70 · 中等≥47≈P20 · 待改进≥42 · 低效<42，旧 75/60/45/25 把 87% 压进中等），`metric_defs._tier_band_hint` 从 `SCORE_TIER_BANDS` 动态生成提示防漂移。（曾实现过「综合分百分位」逐会话指标，按用户要求移除——无意义。）**harness 影响诊断结论（跨源比分的四通道）**：恒等式 `CPE×TCER=1000×p̄`（p̄=混合单价）⇒ 产出轴×成本轴实测相关 0.63、80% 权重压在 TCER 一个潜变量上；跨源差距来自 口径/token 构成（p̄ 0.37~1.14）、行为（工具/百行 16.9~46.5）、任务组合（实测某源 75% non_coding 且净产中位 2961 行）三层；同模型同源名下还叠「部署路由」层（代理会话不上报 cache 字段）。
  - **③逐源基准（v1.7.1）**：`baselines_per_source` 配置块 + `resolve_baselines(uid, source=)` 解析链（逐项目 > 逐源 > 全局）+ `save_baselines(source=)` + `compute_baselines_per_source`（每源独立过 MIN_BASELINE_SESSIONS 门槛）；analyze 两条路径的基准回退与 GUI reanalyze 走同一链；GUI「计算个人基准」弹窗加「按数据源」模式。不写入则维持全局基准=系统分口径（保留 harness 真实差异）。
  - **④同模型跨源对照（v1.7.1）**：`analyze.cross_source_models(reports)`（fixed model vary harness：`_dominant_model_key` 按 token 量取主模型经 `pricing.normalize` 归一 × source 分桶，≥2 源且每源 ≥`CROSS_SOURCE_MIN_CELL`=3 个**有产出（net_loc>0，与评分门控同口径——零净产 tcer=0.0 非 None 不过滤会把中位数拖到 0）**的会话才成组，数值为会话级中位数）→ GUI「工具 → 同模型跨源对照」`CrossSourceModelsPopup`（两级树 + 窗底归因口诀：TCER/工具每百行差=行为、p̄/CPE 差=价格组合、净产差=任务组合）。
  - **待做**：成本轴改 p̄ 比率与 TCER 解耦（正交化，需指标版本化 + audit 重验）。

<a id="compare-models"></a>
### 原 #23 模型对比归因

- `compare_models` 中 Token 用量、成本、缓存命中率、Token 效率按 `TokenUsage.per_model` 精确分桶统计（含单模型与多模型混用会话）；
- 代码效率（`m_code_per_dollar`）与「代码质量与行为」8 项指标均为会话级回放结果，**仅统计单模型独立完成的会话**（`solo_session_count` / `_solo_cost`）。多模型混用会话不按 Token 占比 $w$ 分摊（按 $w$ 加权再除以 $\sum w$ 在单会话或共现会话下恒有 $w/w=1$ 约分，导致两个模型显示完全相同的净增行/读写比/返工率，且浮点尾数微差触发虚假的金色最优高亮；同时 `net_loc * w / cost` 会把代码效率变成 Token 效率的常数倍）。若某模型仅出现在混合会话中（`mixed_only=True`），上述 9 项指标显示「不适用」。

<a id="new-metrics"></a>
### 原 #34 新指标族（v1.6.0 审查批次）

- ⓪**压缩代价** `TokenUsage.compaction_discarded_tokens/pre_tokens_max/duration_ms_total`（Claude compactMetadata；触发方式记 abort_reasons `compact:auto/manual`）；
  - **自报成本偏差** `cost_reported_ratio`（价表计价÷源自报，opencode/omp/pi）；
  - **LOC 可信度** `loc_patch_agreement`（回放 vs structuredPatch，仅 Claude）；
  - **限流洞察**（429 ≥3 次触发 drag）。Claude `usage.output_tokens_details.thinking_tokens` → reasoning（已含在 output，只作占比）；`server_tool_use` 块与 user 内联 image 块计入 tool_calls/image_count；`plan_mode_reentry` 计入计划模式；omp `mode_change(plan)`→计划模式、`stopReason=error`+Too many requests→限流命中、零 usage 响应 duration 不落上一 TurnStat。
  - ①**重试循环** `metrics.retry_loop_metrics`（同工具+同路径连续 ≥3 次；`retry_loops`/`retry_loop_max` 指标 + insights 卡死规则；无路径工具不参与）；
  - ②**单回合成本** `turn_cost_analysis`（最贵回合占比 `turn_cost_share` ≥30% 触发 insights「大户回合」；`cache_invalidations` 缓存失效事件 = cw 翻倍且 cr 回落——**已按用户要求从指标面板移除展示**，计算与 CSV 字段保留）；
  - ③**人机时间** `activity_metrics`（`ai_active_ratio` = Σ回合耗时÷墙钟，Claude 为上界估计；`user_gap_median` 相邻回合间隔中位数，只统计 1–30 分钟）；
  - ④**分段效率** `segment_metrics`（回合三等分算分段 TCER，`efficiency_decay` 末/首比 <0.5 触发「上下文拖累」insights；`TokenUsage.compaction_turns` 记压缩位置，merge 随 turn rebase）。
  - ⑤**项目级分析** `analyze.file_hotspots`（跨会话热点文件）/ `model_switch_stats`（委派 vs 升级救援，按价表 output 价排方向）/ `tool_profile`（技能/子代理/MCP 复用）——GUI「项目画像」弹窗消费。

<a id="est-speed"></a>
### 估算输出速度 `est_output_tps` 与首字延迟中位数

借鉴 cc-switch「估算速度」（纯离线、从会话日志推算；代理实测的「精确速度」TCER 拿不到）。

- **定义**：`est_output_tps = Σ est_gen_output_tokens ÷ (Σ est_gen_duration_ms / 1000)`，单位 token/秒，G2 组，越高越快。耗时**含首字等待**，所以低于代理实测的纯生成速度，两者不可直接比；仅作同口径横向参考。
- **逐响应样本门槛**（`TokenUsage.add_est_gen_sample`，常量见 `models.EST_*`）：输出 ≥ 200 token 且耗时 ≥ 1s，且耗时落在 100ms–1h（再长多半夹了休眠/重试）。聚合一律 Σ/Σ（`est_gen_*` 两个计数字段可加，`merge` 直接求和），**不做逐条平均**。无合格样本 → None（显示 "-"）；数据源根本不产生（Antigravity/WorkBuddy）→「不适用」。
- **各源耗时口径**：
  - **Claude**（`reader.scan_session` 单次扫描内采集，独立路径 `aggregate_usage` 不涉及）：起点 = 该响应首块沿 `parentUuid` 向上第一个**非 attachment** 行的时间戳（最多 32 跳；`isMeta` 行也进链，否则起点解析不出）；终点 = 同 `message.id` 各行最晚时间戳；要求某一行带 `stop_reason` 且最先见到的块 `apiBlockIndex==0`（旧日志无此字段按首块处理）；输出取 `booked[mid].o`（流式快照补差后的最终值）。按 `message.id` 一个样本，rewind 重放同 id 不重复计，终点取所有重复行最晚值（与 cc-switch 同；重放行若时间戳更晚会拉长耗时，靠 1h 上限兜底）。子代理文件各自在文件内解析链，merge 求和。
  - **Codex**（`codex_reader._RequestTimer`，移植 cc-switch `RequestTimer` 的耗时部分）：起点 = 本次请求第一个输出项前最近的「起点行」（上次 token_count / 工具结果 / user·developer 消息 / turn_context）；终点 = 同请求的 `token_usage_record`（新版），否则 token_count 时刻，若 token_count 紧跟工具结果（等工具跑完才写，差 ≤100ms）则取最后一个输出项时刻；旧布局补写在上一 token_count 之后（≤100ms）的输出项忽略；`task_started`/`turn_aborted` 作废未结算请求。输出 = 该 token_count 的 total 差分输出增量；重复投递的 token_count（差分为 0）不结算。
  - **OpenCode**：`message.data.time` 的 `streamed`（不晚于 created 时退回 `completed`）− `created`，要求有 `completed`；输出 = `output + reasoning`（与 TCER 把 reasoning 折入 output 一致）。
  - **omp / Pi**：每响应自报 `duration` / `durationMs` + 该响应 usage.output。
- **与 cc-switch 差异**：不推首字（TCER 有 Codex/omp 自报 TTFT）；OpenCode 输出含 reasoning（cc-switch 单列）；Codex 不读 `item_completed.started_at_ms`；其余门槛逐项一致。
- **`output_tps` 不变**：仍仅 omp/Pi（自报 duration，不设门槛），Codex 的 `task_complete.duration_ms` 是任务级墙钟、Claude 只有整轮墙钟（不变量 #17），均不适用；单位文案由「字/秒」更正为「token/秒」。跨源可比请用 `est_output_tps`。
- **首字延迟 `ttft`** 显示改为逐回合样本中位数（`metrics.ttft_primary_sec`），无样本回退会话最小值；`TokenUsage.time_to_first_token_ms`（min）与 merge 规则不变，`ttft_p95` 不变。Codex 在每个 `task_complete.time_to_first_token_ms` 都追加 `ttft_ms_samples`（已有）。
- **实测量级（本机 2026-10）**：Claude 中位 ≈52 tok/s（有值会话 188/499）、Codex ≈37、omp ≈71、OpenCode ≈22；个别经代理的快模型（qwen 系）可到 800–1100 tok/s，逐响应一致、非缓冲假象，按原值显示。

## 补充：洞察引擎（旧 AGENTS.md 独有）

> 以下条目来自旧 AGENTS.md（CLAUDE.md 的较早分叉，2026-10 已改为短索引文件）。其中 CLAUDE.md 原本没有的内容迁入此处；与 CLAUDE.md 重复的条目以后者为准，不再重复收录。

<a id="insights"></a>
### 原 AGENTS #32 洞察与意见（可执行诊断，`core/insights.py`）

- 排名/分数告诉「好不好」，洞察引擎告诉「具体改什么」。参考 Claude Code 的 `/insights`（What's Working / What's Hindering / Quick Wins 三类，每条给可复制的下一步）与 `/doctor`（发现问题→给修复）。
  - **纯离线、确定性规则引擎**（不调 LLM）：`session_insights(report)` 读一个 `SessionReport`，按阈值表 `_TH`（一处可调）产出 `Insight(kind, title, evidence, action, metric)`——kind∈{good 亮点, drag 拖累项, tip 快速改进}，evidence 用实际数值串（如「返工率 46%」，可核对），action 是一句话下一步（drag 必带），metric 指向 metric_defs 指标供 GUI 上色/跳转。规则覆盖：三轴强弱（>0.65 亮点 / <0.35 拖累）、返工率、工具错误率（需 ≥10 次调用样本，避噪声）、先读后写、盲写覆盖(unseen_writes)、缓存命中率、Edit 占比。UI 文案集中在 `_COPY` 表（一处改词）。
  - **严格聚合不臆测**：仅指标确有数据才判断，缺失不产生洞察；未评分会话只给「不可评分」一条指向排查方向。GUI 在排名页「与项目均值对比」下方新增 `ScoreRankingView._build_insights_section`，按 亮点/拖累项/快速改进 分组渲染（✓/!/→ 标记 + kind 上色 + 证据灰行 + → 建议行），默认展开。护栏测试 `tests/test_insights.py` + `test_gui_smoke.py::test_ranking_insights_section_renders`。设计依据与同类指令（/insights、7阶段流水线、facet schema）调研见 [doc/insights-research.md](insights-research.md)。跨会话（项目级）：`project_insights(reports)` 复用 session 规则、按 (kind,metric) 统计普遍度（每会话每类只计一次），drag 在 ≥40%会话、good ≥ 60%会话才升为「系统性/稳定优势」（evidence 换为「N/M 会话出现」，< 2 会话返回 []）；只统计普遍度、不做 LLM 归因。排名页空态（未选会话）先展「项目洞察（跨会话）」再提示钻取单会话（仿 /insights At-a-Glance）；GUI 渲染走共享 `_render_insight_items`。

## 历史与教训（指标与计价）

- **共线比率不要相乘**：旧 CTEI 把 TCER 与 1/CPE 相乘，近似对 loc/tok 取平方，重尾需截尾且缓存被双重计数；v2 改三正交轴 + 半饱和，有界、可分解、聚合可重算。恒等式 `CPE×TCER=1000×p̄`（p̄=混合单价）意味着产出轴×成本轴实测相关约 0.63，80% 权重压在 TCER 一个潜变量上，待做：成本轴改 p̄ 比率正交化（需指标版本化 + audit 重验）。
- **常数分是伪装的信息**：零净产会话在证据收缩下全部变成恰好 50 分，所以直接不评分；评级带必须按真实已评分分布的分位重划（旧 75/60/45/25 把 87% 压进「中等」）。
- **默认参数在 def 时冻结**：基准参数一律 `None` → 调用时读实时全局，否则 `save_baselines` 重绑全局后分数直到重启才更新。
- **比率聚合用「分子和÷分母和」**：不取算术平均（5k 与 2M token 会话等权会触发 Simpson 悖论）；无分母的比率退化为中位数并标注。
- **漏掉缓存 1h 档会系统性低估成本**（实测本机 60% 缓存写走 1h 档）；价表同步必须以厂商官方页裁决价差（cc-switch 与 models.dev 都出过错）；口径固定为标准价/闲时档，多轨价记入 `_note`。
- **「综合分百分位」类逐会话指标曾实现又移除**（无意义）：新增指标先问它是否携带新信息。
- **指标面板移除 ≠ 删计算**：`cache_invalidations` 按用户要求不再展示，但计算与 CSV 字段保留。
- **文档里别硬编码基准数字**，曾因散落旧值导致雷达图刻度失真。

