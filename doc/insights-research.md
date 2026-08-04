# 调研：Claude Code `/insights` · `/doctor` · `/optimize` 类指令

> 目的：为 TCER「洞察与意见」面板（`core/insights.py` + 排名页 `ScoreRankingView._build_insights_section`）
> 提供设计依据。调研这些「把使用数据翻译成可执行改进」的指令：它们看什么、怎么算、
> 输出什么结构、如何落到行动。文末给出对 TCER 的映射与取舍。
>
> 来源为公开网络资料（博客/文档/GitHub），非官方源码；实现细节以「社区逆向 + 官方文档」
> 交叉印证为准，可能随版本变化。检索工具：tavily / fathomsearch / firecrawl（多引擎多关键词）。

---

## 1. 一句话对照

| 指令 | 定位 | 输入 | 输出 | 是否用 LLM |
|---|---|---|---|---|
| `/insights` | 「照镜子」——分析你怎么用 Claude Code | 近 30 天本地会话日志 | 交互式 HTML 报告（7 段） | 是（Haiku/Opus） |
| `/doctor`（`/checkup`） | 「体检」——诊断安装/配置健康并可修复 | 本地安装与配置文件 | 发现项清单 + 修复建议（先报告后确认） | 部分（trim 用模型） |
| `/optimize` 类（社区/自定义） | 「优化」——针对代码或提示词给改进 | 目标代码/提示词/上下文 | 结构化改进项 | 视实现而定 |

（下面各节展开。）

---

## 2. `/insights` 深度拆解

### 2.1 定位与数据来源

- 官方描述：「Generate a report analyzing your Claude Code sessions, including project areas, interaction patterns, and friction points.」
- 读取**本地**会话日志 `~/.claude/projects/<project>/<session>.jsonl`，默认覆盖近 30 天。
- 只看会话「元数据 + 转录」，**不上传源代码**；分析经 Anthropic API（Haiku/Opus，社区资料两说，见 2.7），报告本地生成。
- 产物：自包含交互式 HTML，落盘 `~/.claude/usage-data/report.html`（部分版本 `~/.claude/data/report.html`）。
- 两种运行：会话内 `/insights`；或 shell `claude -p "/insights"`（非交互，适合每周定时）。

### 2.2 7 阶段流水线（社区逆向）

1. **Collect**：扫 `~/.claude/projects/` 下所有 `.jsonl` 会话。
2. **Filter**：剔除子代理会话（`agent-` 开头）、内部 facet 抽取会话、user 消息 < 2 条、时长 < 1 分钟的会话。
3. **Extract metadata**：每会话抽结构化元数据（见 2.3）。
4. **Facet extraction**：对每个会话调 LLM 抽「facets」（定性判断，见 2.4），**按会话缓存**。
5. **Aggregate**：跨会话聚合统计 + 文本摘要。
6. **Generate insights**：多个专用 prompt 并行生成各报告段（见 2.5）。
7. **Render**：渲染 HTML。

伪代码（社区整理）：

```
sessions = load("~/.claude/projects/").filter(   not agent, not internal, user_msgs>=2, duration>=1min)
metadata = map(extractMetadata, sessions)
for s in sessions:                      # 阶段 2&3，带缓存
    facets[s.id] = cache.get(s.id) or callLLM(FACET_PROMPT + maybe_summarize(s.transcript))
aggregated = aggregate(metadata, facets)
insights = { project_areas, interaction_style, what_works,
             friction, suggestions, on_the_horizon, fun_ending }  # 各一次 LLM
insights.at_a_glance = callLLM(AT_A_GLANCE, aggregated + insights)  # 执行摘要
render("~/.claude/usage-data/report.html")
```

### 2.3 每会话元数据（LLM 之前，纯统计）

`session_id` · `start_time` · `duration_minutes` · `user_message_count` ·
`input_tokens`/`output_tokens` · `tool_counts` · `languages`（按文件后缀）·
`git_commits`/`git_pushes` · `user_interruptions` · `tool_errors`（含分类）·
`lines_added`/`lines_removed`/`files_modified` ·
`uses_task_agent`/`uses_mcp`/`uses_web_search`/`uses_web_fetch` · `first_prompt` · `summary`。

> TCER 已有其中绝大多数同段（token 分桶、tool_calls、tool_errors、lines、git、
> first_prompt_chars、web_searches、image/task agent 等），是本地可复现的部分。

### 2.4 facet 抽取（定性核心）

对每会话让模型输出一个 JSON，schema（社区抓取的 prompt）关键字段：

- `underlying_goal`：用户根本想达成什么。
- `goal_categories`：13 类之一计数——**只算用户显式要求的**（"can you…/please…/I need…"），
  不算 Claude 自主探索。类别：debug_investigate / implement_feature / fix_bug /
  write_script_tool / refactor_code / configure_system / create_pr_commit /
  analyze_data / understand_codebase / write_tests / write_docs / deploy_infra /
  warmup_minimal。
- `outcome`：not_achieved → partially → mostly → fully_achieved → unclear。
- `user_satisfaction_counts`：按显式信号——frustrated / dissatisfied /
  likely_satisfied / satisfied / happy（"perfect!"→happy，"try again"→dissatisfied…）。
- `claude_helpfulness`：unhelpful → slightly → moderately → very_helpful → essential。
- `session_type`：single_task / multi_task / iterative_refinement / exploration / quick_question。
- `friction_counts`（12 类）：misunderstood_request / wrong_approach / buggy_code /
  user_rejected_action / claude_got_blocked / user_stopped_early / wrong_file_or_location /
  excessive_changes / slow_or_verbose / tool_failed / user_unclear / external_issue。
- `primary_success`：none / fast_accurate_search / correct_code_edits / good_explanations /
  proactive_help / multi_file_changes / good_debugging。
- `friction_detail` / `brief_summary`：各一句话。

长转录（> 30000 字符）先分块（25000 字符/块）摘要再抽 facet。

### 2.5 聚合分析的专用 prompt（并行，各一段）

传入聚合统计（sessions/analyzed/date_range/messages/hours/commits/top_tools[8]/
top_goals[8]/outcomes/satisfaction/friction/success/languages）+ 文本摘要
（≤50 条会话摘要、≤20 条 friction detail、≤15 条「用户反复给 Claude 的指令」）。

七段各自的 prompt 意图：
1. **Project Areas**：4–5 个项目领域，各带会话数与 2–3 句描述。
2. **Interaction Style**：2–3 段第二人称叙述「你怎么用」（先写规格 vs 边做边改、
   爱不爱打断），末尾一句最鲜明特征。
3. **What Works**：3 个你做得漂亮的工作流（标题 + 描述）。
4. **Friction Analysis**：3 个摩擦类别，各带 1–2 句解释 + 2 个真实例子（含后果）。
5. **Suggestions**：三维度——
   - `claude_md_additions`：**优先「多次重复出现」的指令**（2+ 会话说过同样的话就该写进 CLAUDE.md），每条给 addition/why/放哪。
   - `features_to_try`：从 MCP / Custom Skills / Hooks / Headless / Task Agents 里挑
     2–3 个最贴合你工作流的，各带可复制命令。
   - `usage_patterns`：每条带一个可直接复制的 prompt。
6. **On the Horizon**：3 个更进阶方向（自主工作流、并行代理、测试驱动迭代），各带 prompt。
7. **Fun Ending**：一个有人情味的难忘瞬间（非统计）。

最后 **At a Glance** 执行摘要，固定 4 段结构（教练口吻）：
**What's working** / **What's hindering you**（拆成 Claude 侧 vs 用户侧）/
**Quick wins to try** / **Ambitious workflows for better models**。

> 这套「亮点 / 拖累（分因） / 快速改进 / 远期」正是 TCER 洞察面板
> good / drag / tip 三段的直接来源。

### 2.6 存储与缓存

| 路径 | 用途 |
|---|---|
| `~/.claude/projects/<p>/<s>.jsonl` | 原始会话日志 |
| `~/.claude/usage-data/session-meta/` | 每会话统计摘要缓存 |
| `~/.claude/usage-data/facets/<id>.json` | 每会话 facet 缓存（AI 抽取结果） |
| `~/.claude/usage-data/report.html`（或 `~/.claude/data/report.html`） | 生成的报告 |

facet 按会话缓存 → 二次运行只分析新会话，快很多。单次上限：社区两说
**50 新会话/次**（Zolkos）或 **200 会话上限、优先最近**（Vincent Qiao），版本差异。

### 2.7 模型与工程细节

- 模型：Zolkos 记为 **Haiku**（快、便宜，facet 4096 tokens、聚合 8192 tokens/prompt）；
  Vincent Qiao 记为 **Opus**（重理解、质量优先）。很可能不同版本/不同阶段用不同模型。
- 实现文件约 113 KB，含 HTML 渲染依赖，采用**惰性加载**（用到才 import，零启动开销）。
- 已知缺陷：facet 抽取会静默失败 → 报告 AI 段全空、`facets/` 目录不创建
  （GitHub issue anthropics/claude-code #70011 / #70228）。

### 2.8 官方生态里的近亲：session-report 插件

Anthropic 官方插件 `session-report`（`claude.com/plugins/session-report`）：
解析本地转录，产出**纯统计**的自包含 HTML——token 消耗、缓存效率、子代理表现、
skills、识别「昂贵 prompt」与子代理异常。与 `/insights` 的差别：偏**量化审计**、
不做 LLM 定性洞察。**这条路线与 TCER 现有能力高度重合**（TCER 已算 CCHR/CPE/子代理折叠/
逐模型成本），可作为「离线也能给价值」的印证。

---

## 3. `/doctor` 与「健康体检」类

### 3.1 官方 `/doctor`（别名 `/checkup`）

定位：**安装 / 配置健康体检，诊断并可修复**。官方文档列出的检查项：

- 安装健康：重复/残留安装、`PATH` 问题、无法解析的 settings 文件。
- 上下文成本：找出「用不到但占上下文」的 skills / MCP servers / plugins；标记慢 hooks。
- 版本：按 release channel 检查是否有新版。
- `CLAUDE.md` 治理（v2.1.206+）：去重（本（本地 vs 已提交）、裁剪（删「模型能从代自行
  推断」的内容，如目录结构、依赖列表、架构概述；**保留**坑点、取舍理由、与工具默认不同的约定）、
  把常驻指南迁移到按需加载的 skills / 嵌套 `CLAUDE.md`。
- 权限：可选把 auto 模式设为默认、预批常被拒的只读命令。

关键工程约定：**先报告发现项、改动前要确认**（先 report 后 confirm）。终端 `claude doctor`
可只读打印诊断、不进会话。已知问题：`claude doctor` 偶发挂起需超时（issue #66122）。

> 对 TCER 的直接启示：**「发现 → 给修复 → 用户确认」三步**，且**只读默认、破坏性改动需确认**。
> TCER 洞察面板目前是纯只读建议（不自动改用户环境），符合这条最保守取向。

### 3.2 社区 `cc-health-check`（github: yurukusa/cc-health-check）

一个把「体检」做成**确定性打分 + 可执行修复**的开源 CLI，与 TCER 综合效率分的
「轴 → 分 → 建议」结构高度同构，值得细看：

- **6 维度 × 20 项检查**：Safety Guards(4) / Code Quality(4) / Monitoring(3) /
  Recovery(3) / Autonomy(3) / Coordination(3)。每项 pass/fail。
- **0–100 分 + 评级带**：80–100 Production Ready / 60–79 Getting There /
  35–59 Needs Work / 0–34 Critical。
- **每维度百分比** + **Top fixes** 一句话可执行项，甚至给「一键修复」命令
  （`npx cc-safe-setup --install-example rm-safety-net`）。
- **纯本地、零依赖、零上传**；`--json` 供 CI/仪表板，`--badge` 生成 shields.io 徽章，
  退出按分数阈值（>=60 → 0）供 CI 门禁。
- 工作方式：读 `~/.claude/settings.json` 的 hooks + 扫 `CLAUDE.md`（全局+项目）+ 查约定文件
  → 逐项打分 → 算维度分 → 出带一键命令的建议。

同作者的 cc-toolkit 还印证了「量化审计」这条产线：`cc-session-stats`（用量）、
`cc-audit-log`（AI 做了什么）、`cc-cost-check`（每次提交成本）、`cc-roast`（毒舌点评 CLAUDE.md）。

### 3.3 `claude-doctor`（行为反模式检测）

社区另有 `claude-doctor` CLI：**读本地会话转录，找行为反模式**（behavioral anti-patterns）
并给修复。与官方 `/doctor`（查安装/配置）不同，它查**用得好不好**——更接近 `/insights` 的定位，
但落成确定性的「反模式清单 + 修复」，而非 LLM 叙述。

---

## 4. `/optimize` 与自定义 slash 命令族

### 4.1 本质：slash 命令 = 提示词剧本

Claude Code 自定义命令就是 `.claude/commands/<name>.md` 里的一段提示词。实例
`to4iki/ai-project-rules` 的 `/optimize` **整个文件只有一行**：

> 「分析这段代码的性能，提出 3 个具体优化。」（原文日文）

启示：**「给我 N 个具体的、可实施的建议」这种把分析约束成固定条数、要求具体可执行的
提示结构，本身就是产品设计**——不追求大而全，只追求「能马上照着做」。TCER 洞察面板每条
drag 必带一句 action，正是同一取向的确定性版本。

### 4.2 成体系的命令套件（github: qdhenry/Claude-Command-Suite）

按命名空间组织的「审计类」命令，展示了「优化/审计」如何模块化：

- `/performance:performance-audit` — 找性能瓶颈。
- `/security:dependency-audit` — 查过期/有风险依赖。
- `/dev:refactor-code <target>` — 改进问题区域。
- `/skills:build-skill` — 把重复工作流固化成 skill。

以及社区流传的 `/refactor-code`（拆解巨型单体文件）等。共同点：**面向具体产物给结构化改进**，
与 `/insights`（面向使用习惯）、`/doctor`（面向环境健康）构成三个互补维度。

### 4.3 官方 `session-report` 插件（见 2.8）

再次点名：官方 `session-report` 是「纯量化审计」路线的官方背书，与 cc-health-check 同属
「不靠 LLM 也能给价值」的证据——对 TCER 尤其相关，因为 TCER 的洞察引擎正是走确定性规则路线。

---

## 5. 综合提炼：三类指令的共同「可执行改进」骨架

把 `/insights`、`/doctor`、`/optimize` 拆开看，它们共享同一套产品骨架：

1. **只读、本地、不改用户代码**（隐私优先；`/doctor` 破坏性改动才需确认）。
2. **观测 → 归类 → 建议**：先量化观测，再归入有限的**类别体系**（goal/friction/维度），
   最后每类给**具体可执行**的下一步。
3. **结构化输出**：JSON/HTML，字段稳定，便于渲染、CI、对比。
4. **可执行性是第一性**：建议必须能「复制就用」——CLAUDE.md 行、命令、prompt、一键修复。
5. **优先高频/高影响**：`/insights` 显式「多次重复的指令优先进 CLAUDE.md」；
   cc-health-check 给「Top fixes」。**不追求穷尽，追求先改最痛的。**
6. **可对比/闭环**：定期跑 → 应用建议 → 再跑看是否改善（self-improving loop）。
7. **LLM 可选而非必需**：`/insights` 用 LLM 做定性归因；cc-health-check / session-report /
   `/optimize`（规则或单 prompt）证明**纯确定性也能给可执行价值**。

分类体系可直接借鉴：
- **摩擦 12 类**（misunderstood / wrong_approach / buggy_code / excessive_changes …）。
- **成功 7 类**（fast_accurate_search / correct_code_edits / good_debugging …）。
- **任务 13 类**（implement_feature / fix_bug / refactor_code / write_tests …，已对应 TCER 任务类型）。
- **健康 6 维度**（Safety/Quality/Monitoring/Recovery/Autonomy/Coordination）。

---

## 6. 对 TCER 的映射与取舍（落到 `core/insights.py`）

TCER 已实现的「洞察与意见」面板与本次调研的对应关系：

| 调研要素 | TCER 现状 | 说明 |
|----|---|---|
| 亮点/拖累/快速改进三段| `Insight.kind` = good/drag/tip | 直接对应 `/insights` 的 What's Working / Hindering / Quick Wins |
| 拖累分「Claude 侧 / 用户侧」 | 暂未细分 | TCER 数据是单机指标回放，暂只给「现象+改法」，不归因到谁 |
| 每条带可执行下一步 | `Insight.action`（drag 必带） | 对齐「可执行第一性」 |
| 观测可核对 | `Insight.evidence`（实际数值串） | 如「返工率 46%」，对齐结构化输出 |
| 有限类别体系 | 阈值表 `_TH` + 轴/质量子信号 | 返工/工具错误/先读后写/盲写/缓存/Edit 占比 |
| 优先高影响 | 三轴最低项优先 + drag 排前 | 对齐「先改最痛的」 |
| 纯确定性、不调 LLM | 规则引擎 `session_insights()` | 对齐纯离线定位，印证于 cc-health-check / session-report |
| 只读、不改环境 | 面板仅展示建议 | 采官方 `/doctor` 最保守取向（不自动改用户配置） |

**刻意不做（及原因）**：
- **跨会话语义聚类 / LLM 定性归因**（`/insights` 的 facet 抽取）：需 LLM + 大量转录理解，
  违背 TCER「纯离线、零依赖、不联网」定位。TCER 用确定性规则覆盖最高价值的一部分。
- **自动修复 / 一键改配置**（cc-health-check 的 `cc-safe-setup`）：TCER 是**度量与洞察**工具，
  不介入用户的 AI 客户端配置；只给「你可以这么做」，不替用户动手。
- **健康 6 维度（环境/安全）**：那是 `/doctor` 的域（hooks/权限/安装），与 TCER「效率度量」正交；
  未来若做「配置体检」可作独立模块，不混入效率分。

**可借鉴的后续增强（未实现，待评估）**：
1. **跨会话重复模式**：某 drag 在 N/M 会话反复出现 → 升为「系统性建议」（对齐
   `/insights`「多次重复优先」）。需项目级聚合 pass。
2. **摩擦类别细化**：把当前笼统的质量信号，按 12 类摩擦体系细分（需更细的工具时序解析）。
3. **闭环对比**：记录上次洞察，下次对比「该 drag 是否消失」，做 self-improving loop 的度量。
4. **建议可复制化**：drag 的 action 目前是一句话；可升级为「可复制的 CLAUDE.md 行 / 命令」。

---

## 7. 参考来源

- Claude Code 官方文档 · Commands（`/insights`、`/doctor`/`/checkup` 定义）
- Claude Code 官方插件 · session-report（量化审计路线）
- Rob Zolkos，《Deep Dive: How Claude Code's /insights Command Works》（7 阶段流水线 + 各 prompt）
- Vincent Qiao，《Claude Code /insights: Your Personalized AI Usage Report》（五阶段/facet schema/存储）
- Skill Gallery、MindStudio 等（报告结构、实操建议）
- github: yurukusa/cc-health-check（6 维度 20 检查 0–100 打分 + 一键修复）
- github: to4iki/ai-project-rules（`/optimize` 命令定义）
- github: qdhenry/Claude-Command-Suite（审计类命令套件）
- github issues: anthropics/claude-code #70011 / #70228 / #66122（facet 失败、doctor 挂起）

> 说明：以上除官方文档外多为社区逆向/二手资料，实现细节可能随 Claude Code 版本变化；
> 本文用于设计参考，不作为官方规范。
