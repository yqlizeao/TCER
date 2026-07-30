# TCER Web 深度化调研与落地路线

> 目标：把 web 从「把上传数据画成曲线」升级为**能给出可执行结论的决策层**——
> 帮团队/个人回答模型选型、Agent 工具选型、harness 配置、Skill / 插件（MCP）取舍。
>
> 调研时间：2026-07。结论与选型见文末「路线取舍」与「自我 review」。

---

## 一、竞品与现有方案调研

### A 类：LLM 可观测性平台（trace 中心）

| 方案 | 定位 | 与 TCER 的关系 |
|---|---|---|
| [Langfuse](https://github.com/langfuse/langfuse)（MIT，21k+★，2026-01 被 ClickHouse 收购） | trace / observation 两层数据模型、prompt 管理、dataset、LLM-as-a-Judge 评测、自定义看板；trace 存 ClickHouse 宽表 | **值得抄数据模型和评测环，不值得抄定位** |
| [LangSmith](https://langfuse.com/resources/engineering/langsmith-alternative) / Helicone / Arize Phoenix / OpenLLMetry | 同类，闭源或半开源 | 同上 |
| [SigNoz + Claude Code OTel](https://signoz.io/docs/claude-code-monitoring/) | Claude Code 原生 OTLP 导出 → 任意 OTel 后端 | 只有 token/延迟/成本，没有代码产出 |

**关键判断**：这类工具服务的是「**自己写 LLM 应用的人**」——需要在调用点埋点，度量对象是一次 API 调用。
它们**不知道代码结果**（净增行、返工、测试覆盖），也**无法跨厂商 CLI 对齐**。
TCER 是事后读本地会话日志、零埋点、能回放工具调用推出 LOC 的，属于**另一个物种**。

可借鉴的三件事：①「宽表 + 一次写入」的存储模型；②会话详情页的 trace 阅读体验（TCER 已有）；
③**评测环（eval loop）** 的思想——但 TCER 离线，不能跑 LLM-as-a-Judge，要用**规则化的证据引擎**替代。

### B 类：工程效能 / AI 度量框架（组织中心）

| 来源 | 核心内容 | 对我们的启发 |
|---|---|---|
| [DX AI Measurement Framework](https://getdx.com/whitepaper/ai-measurement-framework/) | 三维度：**利用率(utilization) / 影响(impact) / 成本(cost)**；四视角：速度、有效性、质量、影响。Q1 2026 数据：高采用率可以和**质量下降、变更失败率上升**并存（部分公司缺陷 +50%） | **只报速度不报质量 = 误导**。指标必须成对出现 |
| [DORA 2025 State of AI-assisted Software Development](https://dora.dev/dora-report-2025/) + [AI Capabilities Model](https://dora.dev/research/2025/ai-capabilities-model/)（近 5000 人样本） | 核心结论：**AI 是放大器**——放大高效组织的优势，也放大失能组织的功能障碍。7 项能力：清晰的 AI 立场、健康的数据生态、AI 可访问的内部数据、强版本控制、小批量工作、以用户为中心、优质内部平台 | 建议要落在**可改的配置/习惯**上（小批量 = 会话规模；强版本控制 = 返工率），而不是「换个模型」 |
| [GitClear 2026 Maintainability Gap](https://www.gitclear.com/the_ai_code_quality_maintainability_gap)（6.23 亿行变更） | 块级重复 +81%（2023→2026 历史最高）、commit 内复制粘贴 15.7%、错误掩盖构造 +47%、两周内 churn +15%、跨文件函数调用 −35%、重构行移动 −70% | **返工率 / 高 churn 文件数 / 重复度**是必须的护栏指标。TCER 已有 churn，缺重复度 |
| [METR RCT](https://valueaddvc.com/blog/ai-coding-productivity-data-what-metr-mckinsey-and-github-actually-found-in-2026) | 开发者自评 +20%，实测 −19%（2025）；2026 复测 +18% | **感知与实测有巨大鸿沟**，且**同一方法一年内翻转符号**——任何单点结论都必须带样本量与置信区间，否则不如不给 |

### C 类：厂商自带团队分析（单厂商孤岛）

- [GitHub Copilot Metrics API](https://docs.github.com/en/rest/copilot/copilot-metrics)（2026-02 GA，2026-05 支持 team 级）+ [copilot-metrics-viewer](https://github.com/github-copilot-resources/copilot-metrics-viewer)
- [Claude Code Analytics](https://code.claude.com/docs/en/analytics) / OTel 指标
- Cursor Admin API

**共同短板**：每家只报自己，口径互不可比（Copilot 报「接受率」，Claude 报 token，Cursor 报请求数）。
**跨工具选型问题它们结构性无法回答。**

### D 类：本地单机看板（个人中心）

[ccusage](https://github.com/ryoppippi/ccusage)（读本地 JSONL 算花费）、
[sniffly](https://github.com/chiphuyen/sniffly)（用量 + 错误分析）、
[claude-code-otel](https://github.com/ColeMurray/claude-code-otel)（Grafana 栈）、
Claude-Code-Agent-Monitor。

都是**单人 + 单工具 + 只有成本/用量**，不接代码产出，不做对比。

### E 类：Skill / MCP / harness 效果度量

这块**基本是空白**。现状只有：
[awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering) 之类的清单、
mcp-agent 的评测框架（面向开发者自测）、
以及个别「MCP usage analytics」skill（只统计调用次数与采纳率）。

> **没有任何开源方案回答「装了这个 Skill / MCP 之后，我的产出效率是否真的变好了」。**

---

## 二、TCER Web 现状缺口（逐条对照代码）

读 `web/backend/db.py` / `server.py` / `frontend/app.js` 后，现状是一个**求和 + 画线**层。缺口：

1. **没有对比语义。** 只有按人/项目/模型的曲线，没有「A 比 B 好多少、是否可信」。
2. **没有混杂控制（最大的科学漏洞）。** 模型 A 的 TCER 高于 B，很可能只是因为 A 恰好用在了
   代码创作任务、B 用在了调试任务。不分层直接比 = 结论随机。
3. **维度不够，问不出用户要的问题。** 上传行里其实有 `source`（5 个 CLI）、`reasoning_effort`、
   `approval_policy`、`permission_profile`、`collaboration_mode`、`cli_version`、`task_type`，
   但 DB 一个都没提升为列；**`tool_calls` 压根没导出**——所以 Skill / MCP / 插件维度**当前完全不可分析**。
4. **没有去噪。** `_agg_metrics` 对比率类指标取**朴素算术平均**，会话 token 量差两个数量级时
   直接触发 Simpson 悖论；重尾分布下一次 2M token 的会话能带偏整组。
5. **聚合 CTEI 取了算术平均。** `_agg_metrics` 里写着 `"ctei": avg("ctei")`。
   CTEI 三因子化后确实可以聚合，但正确做法是**从聚合后的 TCER/CPE/CHR 按公式重算**
   （桌面端 audit 的 `aggregate_ctei_recompute` 就是这么校验的），对各会话 CTEI 取平均
   犯的是和「对比率取平均」同一类错误。
6. **没有可执行输出。** 看完图，用户仍然不知道该改什么。

---

## 三、路线取舍

| 路线 | 内容 | 判断 |
|---|---|---|
| **R1 做成 Langfuse** | 引 OTel 摄取、trace 宽表、eval runner、LLM-as-Judge | ❌ 重复造 MIT 成熟轮子；需要联网跑判官，与「纯离线」冲突；且不解决代码产出归因 |
| **R2 做成 DX / Jellyfish** | 接 PR、事故、问卷，出组织级 ROI | ❌ 依赖 TCER 看不到的数据（git/工单/问卷）。框架可借，产品形态不可抄 |
| **R3 决策实验室（Decision Lab）** | 在现有 SQLite 上加**分层队列对比引擎**：任意配置维度（模型 / Agent 源 / reasoning_effort / 权限档 / Skill / MCP）分组 → 分层控混杂 → 稳健统计去噪 → bootstrap 置信区间 → 证据分级 → 规则化建议 | ✅ 选它 |
| **R4 继续加图表** | 更多曲线、更多表 | ❌ 边际价值低，缺口 1/2/6 一个都没补 |

### 为什么是 R3

- **只有 TCER 能做。** 五个 CLI 的会话被归一到同一套指标，这是 C 类厂商方案结构性做不到的。
- **数据已经在手。** `source` / `reasoning_effort` / `permission_profile` / `tool_calls` 都在会话里，
  只差导出与建模，不需要新的采集能力。
- **不破坏纯离线。** 全是本地 SQLite 上的统计计算，零联网、零依赖。
- **直接命中用户的四个问题**：模型选型 / Agent 工具选型 / harness 配置 / Skill 与插件取舍。

### R3 的设计要点（deep / aggregate / de-noise 三要求逐条落地）

**深度（deep）——从"哪个数大"到"为什么、值不值"**
- 队列（cohort）= 某个**配置维度取值**下的会话集合，例如 `model=claude-opus-4-8`、
  `source=codex`、`reasoning_effort=high`、`skill=dataviz`、`mcp=zread`。
- 对每个队列同时报**产出**（净增行/百万 token、净增行/会话）、**成本**（$/千行）、
  **质量护栏**（返工率、高 churn 文件、工具错误率）——照 DX 的「速度必须配质量」。

**聚合（aggregate）——正确的聚合，而非求和**
- 比率类指标一律**按分母加权**重算，不取算术平均（消 Simpson 悖论）。
- 遵守 SSOT：聚合 CTEI **按 `metrics.ctei` 公式重算**，不对各会话取平均。
- Skill / MCP 从 `tool_calls` 的键自动派生（`Skill` 调用、`mcp__<server>__<tool>` 前缀）。

**去噪（de-noise）——默认不信小样本**
- **最小样本门槛**：队列 < N（默认 5 会话）直接标「证据不足」，不参与排名。
- **稳健中心**：主报**中位数**，辅报 **winsorized 均值**（截尾 10%），不用裸均值。
- **分层控混杂**：按 `task_type` × 会话规模档分层，组内比较后再合并（类 Mantel–Haenszel 思路），
  避免"模型 A 恰好干的都是写新代码"这种伪相关。
- **bootstrap 置信区间**：对中位数差做 1000 次重采样，输出 95% CI；**CI 跨 0 = 不给结论**。
- **证据分级** strong / moderate / weak / insufficient，UI 上明确标注。

**建议（advice）——落到可改的东西上**
规则引擎产出的建议必须指向一个**具体可执行动作**，且附证据：
- 「在『代码创作』任务上默认切到 X」（模型/工具选型）
- 「reasoning_effort=high 未带来可观测收益，成本高 42% → 降档」（harness）
- 「Skill `foo` 参与的会话返工率显著更低 → 推广」/「MCP `bar` 只增加探索调用、不改善产出 → 考虑下线」（Skill/插件）
- 「你的会话规模 P90 超过 X，大会话返工率显著更高 → 拆小批量」（呼应 DORA「小批量」能力）

---

## 四、自我 review（选型复核）

我对 R3 自问了四个问题：

**Q1：统计上会不会又造一个"看起来科学"的假象？**
风险真实存在。观测数据没有随机分配，分层只能控住**已观测**的混杂（任务类型、规模），
控不住"难题倾向于派给强模型"这类选择偏差。
**处置**：不宣称因果。所有措辞用「关联/在同类任务上表现为」，UI 顶部常驻免责说明，
证据等级最高只到 `strong`（不叫 `proven`）。CI 跨 0 一律输出「暂无显著差异」而不是硬排名。

**Q2：样本量够吗？**
个人用户一个月可能只有几十个会话，多数队列会落到「证据不足」。
**这不是缺陷，是诚实。** 空结论比假结论好。同时这正好构成产品动机：多传数据才有结论。

**Q3：会不会 over-engineering？**
bootstrap + 分层用纯标准库实现不到 200 行，无第三方依赖，符合项目零依赖约束。可控。

**Q4：最该先做的是什么？**
是**数据管道**——`tool_calls` 和配置字段不导出，Skill/MCP/harness 三个维度就是纸上谈兵。
所以实现顺序必须是：客户端导出 → 服务端建模 → 分析引擎 → UI。

**复核结论：维持 R3**，但把「不下因果结论、CI 跨 0 不排名、小样本显式标注」写进实现约束。

---

## 五、落地实现（本分支 `feat-web-insights`）

1. `tcer/core/export.py`：`report_row_dict` 增补 `tool_calls`（原始工具名 → 次数）。
2. `web/backend/db.py`：
   - 新增列 `source` / `task_type` / `reasoning_effort` / `approval_policy` /
     `permission_profile` / `collaboration_mode` / `cli_version` / `assistant_turns` /
     `session_duration_minutes` / `high_churn_file_count` / `tool_calls_json`（增量迁移，向后兼容）；
   - `_agg_metrics` 改为**分母加权**重算比率，聚合 CTEI 改为按 `metrics.ctei` 公式重算。
3. `web/backend/analysis.py`（新）：队列构建、分层、稳健统计、bootstrap CI、证据分级、建议规则。
4. `web/backend/server.py`：新增 `GET /api/compare`、`GET /api/insights`、`GET /api/dimensions`。
5. `web/frontend/`：新增「决策实验室」视图（维度选择 + 队列对比表 + 效应量条 + 建议卡片）。
6. `tests/test_web_analysis.py`：统计与建议引擎的单元测试。
