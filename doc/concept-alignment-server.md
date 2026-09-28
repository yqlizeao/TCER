# TCER Server · 团队概念对齐（Concept Alignment）落地方案

> **版本** v0.2（P0 + P1 已落地）｜**日期** 2026-09-28
> **前身**：CBAS v1.0《组织概念基线与沟通偏差纠正系统》（下称「原方案」）
> **一句话**：用团队成员上传的 AI 会话作为主要证据，在 tcer-server 上维护一份**可审计的团队概念基线**，测量「人↔人、人↔AI、AI↔AI」三条沟通通道上的概念损耗，并按个人 / 团队 / 管理三种视角输出。

---

## 0. 结论先行

| # | 决策 | 与原方案的差异 |
|---|---|---|
| D1 | **客户端铁律不变**：零三方依赖，只负责本地采集 + opt-in 上传；语义分析全部放在 server | 一致 |
| D2 | **server 核心仍是纯标准库 + SQLite**；模型能力（jev / LLM / embedding）走可插拔 HTTP 端点，未配置时降级为纯统计模式 | 原方案要求 GPU、Qwen3、BGE-M3、jieba、DuckDB、R。按团队规模这是过度设计，而且和现有 server 的形态冲突 |
| D3 | **分析单元从「会话簇」改为「术语用法（usage）」**：以团队术语库为骨架，逐条记录术语出现在谁的哪条消息里、用的是哪个义项 | 原方案的 k-means 数千簇配合 ≥5 人/≥20 会话的隐私门槛，在几十人规模下大部分簇都会被丢掉（见 §2.3） |
| D4 | **NNO 换成「义项分布差异」**：每条用法做义项判定（候选义项直接来自术语库的 `definition`/`renderings`/`misconceptions`），再比较各职能的义项分布 | 小语料下训练词向量算近邻不稳定（见 §2.2） |
| D5 | **新增人↔AI 通道**：术语出现之后紧跟用户纠正，就是 AI 误解团队黑话的行为证据，成本几乎为零，还能直接换算成 Token/$ 损耗 | 原方案没有覆盖这条通道 |
| D6 | **CCT 和 Pathfinder 在 server 上用纯 Python 实现**（经典 CCT 特征值法 + bootstrap 区间，PFNET 用 minimax Floyd），问卷通过 web 下发 | 原方案依赖 R 的 CCTpack 做 MCMC |
| D7 | 概念模块的数据**永不与效率指标（TCER/CTEI/评分）关联**，也不进入现有「诊断」页里的人员排名 | 把原方案的伦理红线 R1 落到工程约束上 |

---

## 1. 目标与输入输出

### 1.1 目标

降低团队中三类沟通的信息损耗：

| 通道 | 典型损耗 | 在 AI 会话数据中的可观测形态 |
|---|---|---|
| **人↔人** | 同一个词，策划和程序理解不同；同一个概念有多种叫法 | 不同成员的 user 消息中，同一术语的义项分布不同 |
| **人↔AI** | AI 按通用含义理解团队黑话，或自作主张换了术语 | 术语出现 → AI 行动 → 用户纠正；AI 回复里换了同义词 |
| **AI↔AI** | 主代理派发给子代理的 prompt 里术语失真；项目上下文文件（CLAUDE.md/AGENTS.md）缺术语表 | `Task`/`Agent` 工具入参里的 prompt 文本，以及子代理的产出 |

这和客户端术语库（CLAUDE.md #40）的初衷一致：客户端解决「一个人查词、测歧义、考古」，server 解决「团队层面谁跟谁不一致、代价多大、基线怎么治理」。

### 1.2 输入

| 优先级 | 来源 | 获取方式 | 现状 |
|---|---|---|---|
| **主** | 成员上传的 AI 会话（七源 `conversation` 块） | 现有 `/api/upload`，`detail=true` 时带 | ✅ 已有，需新增语义分析授权（§8.2） |
| 主 | 客户端本地术语库 `termbase.json` | 新增 `/api/concepts/termbase/import`（客户端 opt-in 推送，或在 web 上传） | 🆕 |
| 预留 | 人员 → 职能 / 小组映射 | web 端 CSV 导入 + 成员自报 + 飞书通讯录（opt-in） | 🆕 |
| 预留 | 权威文档（Wiki / 术语表 / 策划案 / PRD / onboarding） | web 端上传 Markdown / TXT / CSV | 🆕 |
| 预留 | 协作 IM 记录（飞书群导出） | web 端上传导出文件；以后可做飞书开放平台同步 | 🆕 这是唯一真正「人↔人」的原始语料 |
| 预留 | 概念问卷作答 | web 端问卷页 | 🆕 |

### 1.3 输出（三种视角）

| 视角 | 谁能看 | 看什么 |
|---|---|---|
| **个人** | 仅本人（可主动授权给他人） | 我的术语用法与基线的差异、AI 最常误解我的哪些词、只有我在用的私有黑话（可提名入库）、我的问卷结果（含区间）、「我认为基线有问题」入口 |
| **团队 / 组织全貌** | 全体成员 | 概念地图、基线定义库（定义 + 各职能表述 + 边界 + 版本史）、多套并行理解的概念、术语翻译网络、候选词队列、趋势 |
| **管理诊断** | 管理员（聚合 ≥5 人） | 2×2 诊断象限、跨职能同词异义高风险清单、人↔AI 误解热点及 Token/$ 代价、基线治理健康度、数据覆盖率 |

---

## 2. 对原方案四个问题的深入调研

### 2.1 2×2 象限自相矛盾：必须先定义「准确」的参照物

**问题**：原方案铁律 4 和 §11.5 的文字都说危险区在「右上角（高一致 + 低准确）」，但图中横轴是 Accuracy 低→高、纵轴是 Similarity，按图画出来危险区在**左上角**。

**更深的问题**不在方位，在于 Accuracy 的参照物。原方案给了两种参照：专家结构，或者 CCT 按能力加权后的共识。可是 CCT 的共识本身就是从同一批人的作答里估计出来的，拿它当 Accuracy 参照，和 Similarity 高度共线，象限会退化成一条对角线。

**落地定义**（写入代码 SSOT，杜绝图文不一致）：

```
X 轴 = 一致度 Similarity：成员之间对该概念的义项分布有多集中
        （1 − 归一化熵，或问卷中成员两两一致率）
Y 轴 = 对标度 Accuracy：成员用法与「已批准基线」（术语库中 status=active 且经过评审的 definition）的吻合率
        —— 参照物是治理产物，不是统计产物，两轴才独立

              一致度 低 ─────────────→ 高
对标度 高 │ 🟡 传播问题（有人对，未扩散）│ ✅ 理想区                    │
对标度 低 │ ⚪ 无共识（先建定义）        │ 🔴 集体性误解（最高优先级）  │
```

危险区统一放在**右下角**（高一致、低对标），图例和文字共用同一组常量，不再分别手写。
没有已批准基线的概念，Y 轴不可计算，直接归入「⚪ 待定义」，不做假象限。

### 2.2 NNO 在团队规模下不成立，换成「义项分布差异」

**原方案的做法**：按职能分别训练（或微调）词向量，比较目标词在两个空间里的 k 近邻重叠度，要求每个职能里该词至少出现 50 次。

**调研结论**：
- 近邻距离对训练语料的微小扰动非常敏感，**语料越小越明显**；低频词的近邻最不稳定，建议对多个 bootstrap 样本取平均，不要依赖单个模型（[Antoniak & Mimno, TACL 2018](https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00008/43418/Evaluating-the-Stability-of-Embedding-based-Word-Similarities)；[Wendlandt et al. 2020](https://arxiv.org/pdf/2007.16006)）。几十人团队中，单个职能的语料往往只有几万到几十万字，在上面训练独立的词向量空间，本身就是噪声源。
- 按职能训练词向量在 server 上需要 numpy/gensim，破坏 D2。
- 更适合的替代是**用 LLM 为每一次用法生成语境化定义，再比较义项分布**（[Giulianelli et al., ACL 2023](https://aclanthology.org/2023.acl-long.176/)；[Fedorova et al., Findings ACL 2024](https://aclanthology.org/2024.findings-acl.339.pdf)）。这种做法可解释，在低资源、嘈杂的场景下，零样本 prompt 的表现优于传统嵌入（[Large Language Models on LSCD](https://arxiv.org/html/2312.06002)）。

**TCER 的优势**：术语库已经是现成的**义项清单**：
- `definition`：基线义项
- `renderings[role]`：各职能的合法表述
- `misconceptions[{role, wrong, actual}]`：已知的错误义项

所以不用开放式聚类，可以把每条用法做成**选择题**：「在这句话里，『X』最接近下列哪个意思？」，选项为基线义项 + 各 misconception.wrong + `other`（逃生口）。这正是 jev 擅长的 Choice 题型（选而不生成）。

**指标**：`sense_jsd(term, f_a, f_b)` = 两个职能义项分布之间的 Jensen–Shannon 散度，按会话做 bootstrap 给 95% 区间。
**最小样本**：每个职能 ≥8 条用法且来自 ≥3 人，否则显示「数据不足」。选 8 而不是 50，是因为这是分类计数，不是训练向量空间。门槛值需要在 §7 的交叉验证里校准。
**`other` 占比 >30%**：说明术语库的义项清单不全，把该词送进「义项补全」队列（LLM 从 `other` 类样本里提议新义项，由人工确认）。

### 2.3 隐私门槛与规模不匹配

**原方案**：k=500–5000 个簇，每个簇至少 5 人、20 个会话才保留。

**调研结论**：Clio 在每一步都强制执行最小唯一账户数门槛，层级结构约为 10/100/1000 个簇（[ZenML 对 Clio 的整理](https://www.zenml.io/llmops-database/privacy-preserving-llm-usage-analysis-system-for-production-ai-safety)；[Anthropic Clio](https://www.anthropic.com/research/clio)），但它面对的是数百万用户。粗略估算：团队有 N 人，每个簇至少覆盖 5 个不同的人，按均匀分布最多能留下 N×(每人涉及话题数)/5 个簇。对 40 人团队来说是几十到一两百个，比 500 少了一个数量级；而且话题分布是长尾的，被丢弃的恰好是小组特有的黑话，也就是最有价值的部分。

**落地方案**：门槛挂在**展示层**，而不是分析层。分析层照常处理所有数据；每个视图在输出前按统一的「披露规则」过滤：

| 披露对象 | 门槛 | 不满足时 |
|---|---|---|
| 候选新术语进入公共队列 | ≥3 人使用 且 ≥2 个项目 | 只在使用者本人的个人视图里显示「只有你在用」 |
| 公共视图中的原文证据片段 | 片段来自 `visibility=public` 的会话，或作者单独授权了该片段 | 只显示计数，不显示原文 |
| 职能级统计（义项分布等） | 该职能 ≥5 人 | 并入「其他职能」，或不出数 |
| 管理视图中的任何数字 | 聚合 ≥5 人 | 不出数，并标注原因 |
| 个人视图 | 只有本人 | —— |

门槛集中在 `concepts/disclosure.py` 一个模块，所有 API 出口都经过它，并用测试钉住（参照现有 `_agg_metrics` 的 SSOT 做法）。
原方案里的「LLM 核验摘要不含可识别信息」保留为可选步骤（隐私核验走 jev Noul 题，未配置则跳过，改为人工抽检）。

### 2.4 facet 表主键设计错误

`facet` 表的主键是 `(session_id, facet_name)`，但 `concepts` 这个 facet 一个会话会有多条，存不下。
此外 `msg_id` 列与这个会话级主键矛盾。

**修正**：把标量 facet 和多值实体分开建表：`session_facet (session_key, name) → value` 存标量，`term_usage` 逐条存用法（§4.2）。

### 2.5 调研中额外发现的问题

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| E1 | **AI 会话里不存在「跨职能会话」**：每个会话是一个人和 AI 的对话，原方案「跨界协商记录」依赖的 `freq_in_cross_function_sessions` 在这类数据里恒为 0 | never_negotiated 判定全部为真，指标失效 | 改为「同项目跨职能共用度」（两个职能在同一项目、同一时间窗内都使用该词）；真正的人↔人协商证据来自 IM/文档这个预留入口 |
| E2 | **assistant 文本不代表人的理解**：原方案把 user 和 assistant 都计入词频，只用 `author_balanced` 调权重 | 把模型的措辞算到人头上 | 按说话方严格分层：`user` = 人的理解；`assistant` = AI 的转述，只用于人↔AI 通道；`subagent_prompt` = AI↔AI 通道 |
| E3 | **注入块污染**：Claude 的 user 消息里混有 `<system-reminder>`、`<command-*>`、`<local-command-caveat>`、粘贴的日志；Codex 有 `<environment_context>` | 词频头部被系统文本占据 | server 入库时剥离，并复用客户端 `parse_util` 的 SSOT 规则（`is_slash_command` 等） |
| E4 | **jev 中文准确率偏低** | 义项判定质量下降 | 见 §5.3：英文题面 + 保留原文素材 + 按语言分层做 POC |
| E5 | **匿名上传无法关联职能** | 个人视图与职能统计不可用 | 匿名数据只计入组织级统计，不进职能/个人维度 |
| E6 | **仅聚合上传没有文本** | 语义模块没有输入 | 覆盖率面板明确显示「有文本的会话占比 / 人数」，不足时不出结论 |
| E7 | **现有 server 登录后不区分权限** | 管理视图、个人视图的隔离无从谈起 | 本模块的前置条件：最小 RBAC（§8.1） |

---

## 3. 总体架构

```
┌────────────────────────── TCER 客户端（零依赖，不改铁律）──────────────────────────┐
│ 七源 reader → read_conversation（统一 block 形状）                                 │
│ 术语库 termbase.json（F2/F3/F4 本地功能不变）                                       │
│ 上传：现有 /api/upload + 新增 semantic_consent 标记；可选推送/拉取术语库              │
└──────────────────────────────────────┬─────────────────────────────────────────┘
                                       │ HTTPS（opt-in）
┌──────────────────────────── tcer-server（纯标准库 + SQLite）───────────────────────┐
│                                                                                   │
│  [S0] 入口层   uploads（现有） · 文档/IM/问卷/职能映射（web 预留入口）                 │
│        ↓                                                                          │
│  [S1] 语料层   utterance：按说话方分层（user / assistant / subagent_prompt / doc / im）│
│               剥离注入块 · PII 脱敏 · 代码块只保留标识符 · 幂等（上传 id + block 序号） │
│        ↓                                                                          │
│  [S2] 词汇层   已知术语匹配（复用 tcer.core.termbase.find_terms_in_text）              │
│               新词挖掘：CJK 字 n-gram + 拉丁 token，G² 显著性，对照背景语料             │
│               → term_usage（每次出现一行） · term_candidate                          │
│        ↓                                                                          │
│  [S3] 信号层（零模型，立即可用）                                                     │
│               人↔AI：术语出现后 1–3 轮内被纠正（复用 is_correction）→ 误解事件 + 代价   │
│               AI 换词：user 用 A，assistant 随后改用术语库中 A 的同义词 B              │
│               同项目跨职能共用度 · 术语趋势 · 私有黑话                                 │
│        ↓                                                                          │
│  [S4] 语义层（可插拔 Classifier：jev / LLM / none）                                  │
│               义项判定（Choice，候选来自术语库） → sense 分布 → JSD + bootstrap         │
│               纠正确认（复用 crosscheck 题面）· 候选词是否术语 · 义项补全提议            │
│        ↓                                                                          │
│  [S5] 测评层   问卷（题目由 misconceptions 生成边界题）→ 经典 CCT + bootstrap          │
│               配对相似度 → Pathfinder → 节点级偏差                                   │
│        ↓                                                                          │
│  [S6] 治理层   团队术语库（版本化）· 评审 · 质疑工单 · 导出 CLAUDE.md/AGENTS.md 术语段 │
│        ↓                                                                          │
│  [S7] 披露层   disclosure.py：唯一出口，按视角与门槛过滤 · 访问审计                     │
│        ↓                                                                          │
│  web：个人 / 团队全貌 / 管理诊断                                                     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**为什么不引入嵌入模型和聚类**：概念清单的主骨架是团队术语库，数量级在几十到几百，不是 Clio 那种数千个未知话题。新词发现用 G² 统计 + 人工确认就够了。嵌入只在 Phase 4 翻译层召回时才有明确收益，作为可选的 OpenAI 兼容 `/v1/embeddings` 端点接入（可以是自托管模型），server 不装模型。

---

## 4. 数据模型（SQLite，追加到 `db.py` 的 `_SCHEMA`）

### 4.1 身份与权限

```sql
CREATE TABLE IF NOT EXISTS person_profile (
  person        TEXT PRIMARY KEY,          -- 与 uploads.person 同一命名空间（经 person_aliases 归一）
  username      TEXT,                      -- 绑定的登录账号（本人视图鉴权用）
  function      TEXT,                      -- 职能 key，复用 termbase.ROLE_LABELS（art/designer/ux/eng/audio/qa）
  team          TEXT,
  function_source TEXT NOT NULL,           -- imported | self | feishu | inferred
  semantic_consent INTEGER NOT NULL DEFAULT 0,  -- 是否授权语义分析（见 §8.2）
  personal_opt_out INTEGER NOT NULL DEFAULT 0,  -- 退出个人统计（公共聚合仍计入）
  updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_role (      -- 最小 RBAC
  username TEXT NOT NULL,
  role     TEXT NOT NULL,                   -- member | concept_owner | reviewer | manager | steward | admin
  PRIMARY KEY (username, role)
);

CREATE TABLE IF NOT EXISTS view_access_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  viewer TEXT NOT NULL, subject_person TEXT, view TEXT NOT NULL, at INTEGER NOT NULL
);
```

`function_source=inferred` 的职能**不得**用于个人视图中的结论性表述，只能参与聚合层的探索性统计（沿用原方案 R5）。

### 4.2 语料与用法

```sql
CREATE TABLE IF NOT EXISTS utterance (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  origin       TEXT NOT NULL,     -- upload | doc | im
  origin_ref   TEXT NOT NULL,     -- uploads.id / source_document.id
  block_idx    INTEGER NOT NULL,  -- 在 conversation 中的序号，与 origin_ref 组成幂等键
  session_key  TEXT,              -- source:session_id
  person       TEXT,
  project      TEXT,
  speaker      TEXT NOT NULL,     -- user | assistant | subagent_prompt | doc | im
  turn         INTEGER,
  ts           INTEGER,
  lang         TEXT,              -- zh | en | mixed
  text         TEXT NOT NULL,     -- 已剥离注入块 + 已脱敏
  UNIQUE (origin, origin_ref, block_idx)
);
CREATE VIRTUAL TABLE IF NOT EXISTS utterance_fts USING fts5(text, content='utterance', content_rowid='id', tokenize='trigram');

CREATE TABLE IF NOT EXISTS term_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  utterance_id INTEGER NOT NULL,
  term_slug    TEXT,              -- 已知术语；新词候选为 NULL
  surface      TEXT NOT NULL,     -- 原文命中形式（pref_label / alt_label / term_en）
  char_start   INTEGER, char_end INTEGER,
  sense        TEXT,              -- 义项判定结果：baseline | misc:<i> | other | NULL(未判)
  sense_prob   REAL,
  sense_by     TEXT,              -- jev | llm | human
  UNIQUE (utterance_id, char_start)
);

CREATE TABLE IF NOT EXISTS session_facet (   -- 修正原方案主键问题：标量 facet
  session_key TEXT NOT NULL, name TEXT NOT NULL, value TEXT, extractor TEXT,
  PRIMARY KEY (session_key, name)
);

CREATE TABLE IF NOT EXISTS term_candidate (
  surface TEXT PRIMARY KEY, g2 REAL, freq INTEGER, n_persons INTEGER, n_projects INTEGER,
  is_term_prob REAL,                 -- jev Noul「是否团队术语」
  nearest_slug TEXT,                 -- 疑似已有术语的别名
  status TEXT NOT NULL DEFAULT 'new' -- new | nominated | merged | rejected
);
```

FTS5 的 `trigram` 分词器从 SQLite 3.34 起内置，能对中文做子串检索，不需要分词库。两个字的中文词无法走 trigram 索引，需要回退到 `LIKE`（[SQLite FTS5 CJK 实践](https://zenn.dev/kanseilink/articles/kanseilink-fts5-trigram-cjk-20260507?locale=en)）。它只服务于「证据检索」，术语命中本身走 `find_terms_in_text`。

### 4.3 信号与测评

```sql
CREATE TABLE IF NOT EXISTS misread_event (    -- 人↔AI 误解事件
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  term_usage_id INTEGER NOT NULL,             -- 触发的术语用法（user 侧）
  correction_utterance_id INTEGER NOT NULL,   -- 随后的纠正消息
  turns_between INTEGER, tokens_between INTEGER, cost_usd_between REAL,
  confirmed_by TEXT,                          -- regex | jev | human
  confirm_prob REAL
);

CREATE TABLE IF NOT EXISTS assessment (...);        -- 结构沿用原方案 §12，模型类型固定为 classic_cct
CREATE TABLE IF NOT EXISTS assessment_item (...);   -- 题目：concept_slug, kind(boundary|definition|likert), 来源 misconception 索引
CREATE TABLE IF NOT EXISTS assessment_response (...);
CREATE TABLE IF NOT EXISTS cct_result (...);        -- 共识答案 / 能力值 D_i 及 bootstrap 区间 / λ1/λ2
```

### 4.4 治理与审计

```sql
CREATE TABLE IF NOT EXISTS concept (            -- 团队术语库（server 为权威源）
  slug TEXT PRIMARY KEY,
  entry_json TEXT NOT NULL,                     -- 与客户端 TermEntry.to_dict() 完全同构
  status TEXT NOT NULL,                         -- draft | reviewing | active | deprecated
  version INTEGER NOT NULL,
  owner TEXT, approved_by TEXT, approved_at INTEGER
);
CREATE TABLE IF NOT EXISTS concept_history (slug TEXT, version INTEGER, entry_json TEXT, changed_by TEXT, changed_at INTEGER, reason TEXT,
  PRIMARY KEY (slug, version));
CREATE TABLE IF NOT EXISTS concept_dispute (    -- 「我认为基线有问题」
  id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, raised_by TEXT, body TEXT,
  status TEXT,                                  -- open | accepted | rejected
  created_at INTEGER, resolved_at INTEGER
);
CREATE TABLE IF NOT EXISTS source_document (    -- web 预留入口：Wiki / 术语表 / PRD / IM 导出
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, title TEXT, uploaded_by TEXT,
  visibility TEXT, text TEXT, uploaded_at INTEGER
);
CREATE TABLE IF NOT EXISTS model_call_log (     -- 铁律 7：全链路可审计
  id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, provider TEXT, model TEXT,
  prompt_version TEXT, request_hash TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL, cached INTEGER, at INTEGER
);
```

**术语库的 SSOT**：server 的 `concept.entry_json` 与客户端 `TermEntry` 字段完全一致（slug / pref_label / alt_labels / renderings / misconceptions …），导入导出无损。server 端直接 `from tcer.core import termbase` 做校验和匹配，现有 server 已经这样复用 `tcer.core.pricing`/`metrics`，这里遵循同一做法，两端的匹配规则就不会漂移。

---

## 5. 分析模块详细设计

### 5.1 S1 语料层（`concepts/ingest.py`）

- **触发**：上传入库后异步排队（标准库 `threading` + 任务表），也可以由管理员在 web 上触发全量重建。
- **说话方分层**：`conversation` block 中 `role=user,type=text` → `user`；`role=assistant,type=text` → `assistant`；`type=tool_use` 且 `name ∈ {Task, Agent}` 的 `input.prompt` → `subagent_prompt`；`thinking` 与 `tool_result` **不入库**（体积大，也不代表沟通）。
- **剥离**：`<system-reminder>…</system-reminder>`、`<command-*>`、`<local-command-*>`、`<environment_context>`；以 `/` 开头的斜杠命令（`parse_util.is_slash_command`）。
- **脱敏**：邮箱、手机号、身份证号、银行卡号、`sk-…`/`ghp_…` 等 key 形态、内网 IP。先脱敏再落库，原始 `raw_json` 仍按现有 visibility 规则存放，不另外复制。
- **代码**：fenced code block 只保留 camelCase/snake_case 标识符并拆词（例如 `useEffect` → `use effect`）；长粘贴日志（>2000 字符且非自然语言比例高）截断。

### 5.2 S2 词汇层（`concepts/lexicon.py`）

**已知术语命中**：`termbase.find_terms_in_text(text, active_terms)`，拉丁词边界、CJK 子串、短词保护三条规则与客户端完全一致。

**新词挖掘**（纯标准库）：
1. 候选：中文连续片段的 2–6 字 n-gram；拉丁 token 及 2–3 词短语；标识符。
2. 过滤：频次 ≥5、使用人数 ≥2；去掉被更长候选完全包含且频次相同的子串（例如「上下文窗」被「上下文窗口」吸收）。
3. 显著性：用 **G²（对数似然比）** 比较团队语料与背景语料的频率。背景语料 = 同一 server 上所有项目的 assistant 文本（AI 的通用措辞），这样可以把「AI 也常这么说」的通用词压下去。统计上不用裸 PMI，理由同原方案（[Dunning 1993](https://aclanthology.org/J93-1003/)）。
4. 与术语库对齐：2-gram 重叠率配合标签双向包含 → `nearest_slug`，提示「可能是某个已有术语的别名」。这一步复用客户端 F2 的 `align_candidate_with_termbase` 逻辑。
5. 可选：用 jev Noul 判定「是否团队术语」，复用 F3 题面 `is_term`，不另写新题面，保证缓存键一致。

### 5.3 S4 语义层：可插拔判定器（`concepts/classify.py`）

```python
class Classifier(Protocol):
    name: str                                    # jev | llm | none
    def choose(self, task: str, state: dict, options: list[str]) -> Decision: ...
    def yesno(self, task: str, state: dict) -> Decision: ...
```

| 任务 | 题型 | 实现 | 未配置时 |
|---|---|---|---|
| 义项判定 `sense` | Choice（基线 / 各 misconception / other） | jev 优先，LLM 次之 | 不出义项分布，只出频次与人↔AI 信号 |
| 纠正确认 `correction` | Noul ×3（复用 crosscheck 的 corrects/dissatisfied/newreq） | jev | 只用正则 `is_correction` |
| 候选是否术语 `is_term` | Noul（复用 F3 题面） | jev | 只用 G² 排序 |
| 义项补全 `sense_propose` | 生成式 | LLM | 跳过 |
| 翻译说明 `translation_note` | 生成式 | LLM | 跳过 |

**jev 使用纪律**（基于 `doc/jev-research.md` 的实测）：
1. **题面用英文，素材保留原文**：官方说明 jev 以英语为主要训练语言，CJK 准确率会降低；TCER 自己的中英 A/B 实测中，英文题面置信度更高，输入 token 少 20–35%。
2. **每道 Choice 题必须带逃生口**（`other`/`none`），因为 Choice 的概率分布之和恒为 1，没有逃生口时乱码输入也会被迫选一项。
3. **0.40–0.60 为灰色地带**，单列展示，不计入任何结论（与 crosscheck v2 同口径）。
4. **校准是群体性质**，单次判定不保证正确，所以只用于分布统计，不对单条消息下「此人理解错了」的结论（铁律 6）。
5. **内容哈希缓存 + 计费记账**：复用 `typesafe_client` 的缓存与 usage 机制，server 侧另起文件，写入 `model_call_log`。

**POC 门禁**（进入产品前必须通过）：从真实数据中抽 200 条术语用法，由两名成员独立标注义项。报告以下几项：人-人一致率 κ（作为上界基线）；jev 与人工的一致率（**按中文 / 英文 / 中英混合分层**报告）；置信度校准曲线（ECE）；p95 延迟。jev 一致率 ≥ 人-人一致率 × 0.85 才允许进入义项分布统计；否则回退到 LLM，或者只保留人工标注的小样本。

### 5.4 S3 人↔AI 与 AI↔AI 信号（`concepts/signals.py`，零模型）

这是本方案相对原方案最大的增量：**不需要问卷、不需要模型，只要有上传数据就能产出结论，而且直接对应成本**。

**① 术语触发误解事件**
```
对每条 user 侧 term_usage u（会话 s，回合 t）：
  找 s 中 t 之后第一条 user 消息 v，要求在 1–3 个 user 轮次内；
  若 is_correction(v)，且 v 中重新提到该术语或其别名，或者 AI 在 t..v 之间编辑过文件
    → misread_event(u, v)
  代价 = t..v 之间的 Token 与 $。当前上传行不含逐回合 turn_stats，先按
         「会话成本 ÷ assistant_turns × 间隔 AI 回合数」估算并标注「估算」；
         客户端补传 turn_stats 后（§9 可选项）改为精确值
```
- `misread_rate(term)` = 误解事件数 ÷ 该术语在 user 侧的用法数，按人做 bootstrap 给区间。
- 正则只召回有纠正措辞的消息，漏报率高，所以用 jev 纠正题对**所有** 1–3 轮内的后续消息复核（配置了判定器时），并在报告中注明「正则召回 / 模型召回」各自贡献多少。
- **不归责到人**：这个指标描述的是「这个词让 AI 困惑的概率」。对应的行动是把该术语写进项目上下文文件，而不是去纠正人。

**② AI 换词率**：user 用了术语 A（slug 为 X），AI 在后续回复中没有沿用 A 及其 alt_labels，改用了术语库中**另一个**概念 Y 的标签。这说明 AI 把 X 当成了 Y。这条指标只统计术语库内的替换，避免把正常改写也算进去。

**③ AI↔AI 术语保真度**：`subagent_prompt` 中，被派发的任务描述是否保留了同会话 user 消息中出现过的术语，还是换成了通用词。保真度低意味着子代理拿到的是被稀释的需求。

**④ 上下文文件覆盖**：统计 `misread_rate` 高的术语，是否出现在该项目上传的 `CLAUDE.md`/`AGENTS.md` 读取记录中（`tool_use` 为 Read，路径命中）。未覆盖就给出行动建议：「把以下 N 个术语加入项目上下文」，并在 web 上一键导出一段 Markdown 术语表（§6.4）。

**⑤ 同项目跨职能共用度**（替代原方案「跨界协商」，见 E1）：同一项目内，职能 A 和 B 在 30 天窗口内都使用了该术语，记为一次「共用」。若义项分布 JSD 高且共用度高，说明两个职能在同一个项目里对同一个词各说各话，干预优先级最高。

### 5.5 S5 测评层（`concepts/assess.py`、`concepts/cct.py`、`concepts/pathfinder.py`）

**题目生成**（半自动，需人工审核）：
- 每条 `misconceptions[i]` 生成一道**边界判断题**：「以下说法是否符合团队对『X』的定义：{wrong}」。原方案指出边界题比核心题更能暴露偏差，而术语库里的 misconceptions 本身就是经过整理的边界。
- 每个概念再出一道定义匹配单选题，选项为 definition + 各 renderings + 干扰项。
- 概念的选取按以下三项排序：`sense_jsd` 高、`misread_rate` 高、跨职能共用度高，这就是原方案「A → B」的方向。

**作答**：web 问卷页，每人单独作答、限时，不显示他人答案（CCT 要求作答相互独立）。

**CCT（经典形式，纯 Python）**：
1. 计算成员两两一致率矩阵，按猜测率做校正（真假题为 1/2，L 选 1 题为 1/L）。
2. 对角线未知时，用 minimum residual 迭代估计，再做幂迭代取前两个特征值 λ1、λ2，并得到第一因子载荷。载荷即能力值 D_i。
3. 检验：λ1/λ2 ≥ 3，且所有 D_i > 0。不满足时**不输出单一基线**，改为输出「该组概念存在多套并行理解」，并按载荷符号或第二因子把成员分成亚共识组。
4. 共识答案：用能力值加权投票（Romney et al. 1986 的贝叶斯判定）。
5. **区间**：对题目做 bootstrap 重采样（1000 次），得到 D_i 与 λ 比的 95% 区间。这是用经典方法替代层次贝叶斯 MCMC 的做法，精度略低，但不需要 R 或 Stan。

**样本量**：CCT 在熟悉的领域里 4 个信息人就能得到较好结果，所需人数随平均能力下降而增加，原文给出了查表关系（[Romney, Weller & Batchelder 1986](https://www.bebr.ufl.edu/sites/default/files/Culture%20as%20Consensus.pdf)；[Weller 2007](https://journals.sagepub.com/doi/10.1177/1525822X07303502)）。落地规则：全团队 ≥10 人才出组织基线；做职能分组比较时每组 ≥5 人；题目 ≥20 道（个体能力区间需要足够的题量）。

**Pathfinder**：配对相似度评分（≤15 个概念，即 105 对）→ PFNET(r=∞, q=n−1) = 用 minimax 版 Floyd–Warshall 保留满足三角不等式的边 → 个体网络与参照网络（团队均值或专家均值）逐节点求邻居 Jaccard，得到节点级偏差热力图。

### 5.6 S6 治理层（`concepts/governance.py`）

```
新概念 / 候选提名 / 质疑累积 ≥3 次 / 季度复审
  → 概念负责人起草（draft）
  → 评审（每个涉及的职能至少 1 名 reviewer 批准）→ reviewing
  → 批准 active，version+1，写 concept_history
  → 通知：变更公示；受影响成员的个人视图显示「基线已更新」
```

- 质疑工单（`concept_dispute`）必须有回复才能关闭；「驳回」必须附理由。
- 术语库双向同步：web 端可以导入客户端 `termbase.json`（按 slug 合并，冲突进评审）；server 导出与客户端同构的 JSON，客户端可在「上传设置」中 opt-in 拉取（§8.3）。

---

## 6. 视图规格

### 6.1 个人视图（`/concepts/me`，仅本人）

| 卡片 | 内容 | 数据来源 | 最低门槛 |
|---|---|---|---|
| 我与基线的差异 | 按 `sense` 分布与基线的差距排序的前 10 个术语；每项展开：基线定义、我的用法分布、3 条我的原文证据、各职能的表述 | term_usage（本人 user 侧） | 该术语本人用法 ≥5 |
| AI 最常误解我的词 | misread_rate 排序 + 代价（Token/$）+ 「加入项目上下文」按钮 | misread_event | 事件 ≥2 |
| 只有我在用的词 | 候选词中仅本人使用的（私有黑话）；可「提名入库」或「标记为无需收录」 | term_candidate | —— |
| 我的问卷结果 | 能力值 D_i 及 95% 区间；逐概念与共识答案的对照 | cct_result | 已作答 |
| 概念丰富度 | 本人使用过的不同义项数（原方案 B11），**明确标注丰富度高是优点** | term_usage | —— |
| 我认为基线有问题 | 每个概念旁都有入口 | concept_dispute | —— |

**措辞纪律**：只说「与基线的差异」，不说「错误」或「理解错了」；不显示任何横向排名；每个结论附原文证据，本人可以质疑。

### 6.2 团队 / 组织全貌（`/concepts`，全员）

| 页面 | 内容 |
|---|---|
| 概念地图 | 按 MDA 层 / 项目 / 职能分组的树表 + 共现网络（共现按 NPMI 排序，最少共现 3 次；社区划分用标签传播，纯标准库）。量值用条形和位置编码，不做词云 |
| 基线定义库 | 每个概念一页：标准定义、各职能表述、边界（misconceptions）、版本史、负责人、频次与使用人数、公开证据片段（≤20 条，经披露层过滤）、问卷共识强度（若已测评） |
| 多套理解 | λ1/λ2 < 3 或义项分布双峰的概念，以及各亚共识组的代表性表述（不显示组员名单） |
| 翻译网络 | 概念 ↔ 各职能表述的二部图；边宽为置信度，虚线为待确认，点边可以确认或否认（需 reviewer 权限） |
| 候选队列 | 满足 ≥3 人、≥2 项目的新词，G² 排序；任何成员都可以认领起草 |
| 趋势 | 术语月度使用量、新义项出现、基线变更时间线 |

### 6.3 管理诊断（`/concepts/diagnosis`，manager，聚合 ≥5 人）

| 模块 | 内容 | 行动建议 |
|---|---|---|
| 2×2 象限 | §2.1 的定义；点 = 概念，大小 = 使用量 | 🔴 右下：组织拉齐会 + 修订基线；🟡 左上：推广基线；⚪：指派负责人起草 |
| 跨职能同词异义 | `sense_jsd` 高 × 同项目共用度高的术语，附两侧的代表性表述 | 进入评审队列 |
| 人↔AI 误解代价 | 按术语、按项目汇总 misread 事件的 Token/$，以及纠正循环的轮数中位数 | 导出上下文术语段；对比写入前后的误解率（前后对照，按「决策实验室」的证据分级展示） |
| AI↔AI 保真度 | 子代理 prompt 中术语丢失率排名前列的项目 | 在项目上下文中要求派发时保留术语 |
| 治理健康度 | 草稿积压、质疑未回复时长、无负责人概念数、基线覆盖率（高频候选中已入库的比例） | —— |
| 数据覆盖 | 有文本的会话占比、授权人数、各职能人数（<5 人的职能标灰） | 覆盖不足时整页显示「数据不足」，不出结论 |

**硬约束**：管理视图的任何 API 都不接受 `person` 参数；按职能或项目聚合时，若人数 <5 则整行不出数。

### 6.4 「降损耗」的直接出口：上下文术语段导出

在基线定义库和管理诊断中都提供导出：选定项目 → 生成可直接粘贴进 `CLAUDE.md`/`AGENTS.md` 的术语段，包括术语、团队含义、常见误解、禁用的替代说法。这是把「对齐基线」直接交给 AI 的最短路径，也是验证本系统价值最直接的实验：导出前后对比同一项目的 `misread_rate`。

---

## 7. 效度验证（上线门槛）

| 检验 | 方法 | 门槛 | 不达标时 |
|---|---|---|---|
| 合成数据重建 | 仿照现有 `seed_mock.py` 的「植入已知效应」：造一批会话，植入「职能 A 把 X 用作义项 2」「术语 Y 触发纠正」等效应 | 植入效应全部被检出，且无植入的术语不误报 | 修管线；作为 CI 用例 |
| 义项判定 POC | §5.3 | 与人工一致率 ≥ 人-人一致率 × 0.85（按语言分层） | 回退到 LLM 或人工 |
| 自动 vs 问卷 | 已测评概念上，`sense_jsd` 与 CCT 分歧度（1 − λ1/λ2 归一化，或亚共识数）做 Spearman 相关 | ρ ≥ 0.5 | `sense_jsd` 在管理视图中降级为「探索性」标签 |
| 误解信号 | 随机抽 100 个 misread_event 人工复核 | 精确率 ≥ 0.7 | 收紧匹配窗口 / 要求模型复核 |
| 外部效度 | 与导入的 Wiki / 术语表定义比较 | 高频候选词中已被文档收录者 ≥50% | 检查新词挖掘参数 |

门槛数字属于初值，第一轮真实数据跑完后需要回填调整，调整记录写入本文件。

---

## 8. 隐私、授权与权限

### 8.1 RBAC（前置条件）

| 角色 | 权限 |
|---|---|
| member | 个人视图（仅本人）、团队全貌、提名候选、质疑基线、作答问卷 |
| concept_owner | 编辑所负责概念的草稿、回复质疑 |
| reviewer | 批准 / 驳回概念变更、确认翻译边 |
| manager | 管理诊断（仅聚合） |
| steward | 隐私审计、访问日志、披露门槛配置、模型端点配置 |
| admin | 账号与角色管理 |

manager **没有**查看他人个人视图的权限。本人可以主动授权给指定账号（如导师），授权可撤销，并写入 `view_access_log`；本人可以查看谁访问过自己的数据。

### 8.2 授权分层

| 层级 | 客户端上传内容 | 语义模块可用范围 |
|---|---|---|
| 仅聚合 | aggregate 行 | 不参与 |
| 会话指标 | session 行（无文本） | 不参与 |
| 含明细 | + conversation | 只有**另外勾选「允许团队概念分析」**（`semantic_consent`）才进入 S1 |
| 含明细 + 公开 | + visibility=public | 原文片段可在公共视图中作为证据展示 |

`semantic_consent` 与现有 `detail` 分开设置：同意把对话传到 server 供自己回看，不等于同意纳入团队分析。撤回授权后，删除该人的所有 utterance / term_usage / misread_event，聚合结果在下次重建时刷新。

### 8.3 五条红线（沿用原方案并落到工程约束）

| # | 红线 | 工程实现 |
|---|---|---|
| R1 | 不用于绩效评估 | 概念模块的表与 `uploads` 的效率列不做 join；现有「诊断」页的人员维度不读取概念模块数据；代码评审清单中写入此条；有测试钉住 |
| R2 | 个人视图仅本人或受托人可见 | API 层以登录身份决定 `person`，不接受该参数 |
| R3 | 聚合 ≥5 人 | `disclosure.py` 唯一出口 |
| R4 | 访问审计 | `view_access_log` |
| R5 | 推断职能不下个人结论 | `function_source=inferred` 在个人视图中不参与职能对比 |

### 8.4 联网姿态

现有 server 的承诺是「运行时零联网」。语义层调用 jev/LLM 是新的外联行为，处理方式与客户端 #3/#38 保持一致：**未配置端点 = 零外联**（S0–S3 和 S5 全部可用）；配置由 steward 在 web 上完成，页面上明确列出会发送的数据范围（术语用法所在的句子窗口，≤400 字符，已脱敏）。推荐使用私有部署的 OpenAI 兼容端点；若使用 jev 等外部服务，需要在授权说明中写明。

---

## 9. 客户端配套改动（最小化，保持零依赖）

| 改动 | 位置 | 说明 |
|---|---|---|
| 上传信封增加 `semantic_consent` | `upload_client.build_payload` + 上传对话框复选框 | 默认不勾选；只有 `detail=true` 时可勾 |
| 推送本地术语库 | 术语库页签工具栏「推送到团队」 | 走现有上传鉴权；server 作为提案合并，进入评审 |
| 拉取团队术语库 | 同上「从团队同步」 | 覆盖前显示差异；仍然是 opt-in 联网 |
| 可选：上传逐回合 `turn_stats` | `build_payload` 在 `detail=true` 时附带 | 让误解代价从估算变成精确值；字段已存在于 `TokenUsage`，不涉及 reader 改动 |
| 无 | reader / 指标 / audit | **不改**；`conversation` 块已经包含 subagent 的 `tool_use.input` |

---

## 10. 路线图

| 阶段 | 周期 | 交付 | 门禁 |
|---|---|---|---|
| **P0 地基** | 1–2 周 | RBAC、person_profile 与职能导入页、semantic_consent、concept 表与术语库导入导出、disclosure.py、访问日志 | 红线测试全部通过；职能覆盖 ≥80% 授权成员 |
| **P1 零模型价值** | 2–3 周 | S1 语料层、S2 已知术语命中与新词挖掘、S3 人↔AI 误解与换词信号、上下文术语段导出；个人视图的「AI 误解我的词」「只有我在用的词」；团队全貌的基线库与候选队列 | 合成重建用例通过；misread 抽检精确率 ≥0.7 |
| **P2 问卷测评** | 2–4 周（与 P1 并行） | 题目生成与审核、问卷页、经典 CCT + bootstrap、Pathfinder、节点级热力图、多套理解页 | λ 检验逻辑有单测；第一轮作答 ≥10 人 |
| **P3 语义层** | 3–4 周 | Classifier 抽象、jev/LLM 适配、义项判定与 JSD、POC 报告、2×2 象限、跨职能同词异义卡、model_call_log | POC 门禁；ρ ≥ 0.5 才进入管理视图 |
| **P4 深化** | 持续 | IM/文档入口的真正人↔人分析、可选 embedding 召回的翻译网络、上下文导出前后的效果对照、契合度时间序列 | 按需评估 |

与原方案「先做通道 B」的差异：TCER 的数据里已经有**行为级的 ground truth**（纠正事件），P1 不需要问卷就能产出可执行结论，所以 P1 和 P2 并行，不让问卷阻塞整体进度。

---

## 11. 模块与代码索引

```
server/backend/concepts/
├── __init__.py
├── schema.py        建表与迁移（挂到 db.init）
├── ingest.py        S1：conversation → utterance（分层、剥离、脱敏、幂等）
├── lexicon.py       S2：已知术语命中（复用 tcer.core.termbase）+ n-gram/G² 新词挖掘
├── signals.py       S3：misread_event、AI 换词、AI↔AI 保真度、同项目共用度
├── classify.py      S4：Classifier 协议 + jev/llm/none 实现 + 缓存与记账
├── sense.py         S4：义项判定调度、分布、JSD、bootstrap
├── assess.py        S5：题目生成、问卷作答
├── cct.py           S5：经典 CCT（一致率矩阵、MINRES、幂迭代、bootstrap）
├── pathfinder.py    S5：PFNET(r=∞,q=n-1) + 节点级 Jaccard
├── governance.py    S6：版本、评审、质疑、导入导出、上下文术语段
├── disclosure.py    S7：唯一出口 + 门槛 + 访问日志
└── api.py           路由注册（由 server.py 分发）
server/frontend/js/
├── view-concepts-me.js
├── view-concepts-org.js
└── view-concepts-diag.js
tests/server/
├── test_concepts_disclosure.py   红线与门槛
├── test_concepts_synthetic.py    植入效应重建
├── test_cct.py / test_pathfinder.py
└── test_concepts_signals.py
```

---

## 12. 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 授权率低，文本覆盖不足 | 中高 | 高 | 先上线只对本人有用的个人视图（AI 误解我的词）作为授权的回报；覆盖不足时如实显示 |
| 员工认为被监视 | 中 | 极高 | 红线公示；管理视图只看聚合；本人可查访问记录；可退出 |
| 被挪用于绩效 | 中 | 极高 | R1 工程隔离 + 使用协议 |
| jev 中文义项判定不达标 | 中 | 中 | POC 门禁；回退 LLM；最坏情况只保留零模型信号 |
| 术语库义项清单不全 | 高 | 中 | `other` 占比告警 + 义项补全队列 |
| 小职能人数 <5 | 高 | 中 | 合并为「其他」，或只在组织级呈现 |
| 误解信号混入非术语原因的纠正 | 中 | 中 | 要求纠正消息与术语有关联（重提术语 / 编辑同一文件）；模型复核；抽检 |

---

## 附录 A · 与客户端现有术语能力的对应

| 客户端（#40） | server 对应 | 关系 |
|---|---|---|
| `termbase.TermEntry` | `concept.entry_json` | 同构，server 为团队权威源 |
| `find_terms_in_text` | S2 已知术语命中 | 直接复用 |
| F2 术语考古（单会话） | S2 新词挖掘（全团队）+ 候选队列 | 同一口径的团队版 |
| F3 反向查询（`is_term`/`which_term`） | 候选是否术语、别名对齐 | 复用题面，缓存键一致 |
| F4 歧义检测（D3 误读探针） | 义项判定（S4）+ 问卷边界题（S5） | misconceptions 同时充当义项清单和题库 |
| crosscheck 纠正交叉验证 | misread_event 复核 | 复用三问题面与灰色地带口径 |
| typesafe 缓存 / 计费记账 | model_call_log | 同一机制的 server 版 |

## 附录 B · 参考

- Romney, Weller & Batchelder (1986). [Culture as Consensus](https://www.bebr.ufl.edu/sites/default/files/Culture%20as%20Consensus.pdf)
- Weller (2007). [Cultural Consensus Theory: Applications and FAQ](https://journals.sagepub.com/doi/10.1177/1525822X07303502)
- Antoniak & Mimno (2018). [Evaluating the Stability of Embedding-based Word Similarities](https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00008/43418/Evaluating-the-Stability-of-Embedding-based-Word-Similarities)
- Wendlandt et al. (2020). [Word Embeddings: Stability and Semantic Change](https://arxiv.org/pdf/2007.16006)
- Giulianelli et al. (2023). [Interpretable Word Sense Representations via Definition Generation](https://aclanthology.org/2023.acl-long.176/)
- Fedorova et al. (2024). [Definition generation for lexical semantic change detection](https://aclanthology.org/2024.findings-acl.339.pdf)
- [Large Language Models on Lexical Semantic Change Detection: An Evaluation](https://arxiv.org/html/2312.06002)
- Anthropic. [Clio](https://www.anthropic.com/research/clio)；[ZenML 对 Clio 管线的整理](https://www.zenml.io/llmops-database/privacy-preserving-llm-usage-analysis-system-for-production-ai-safety)
- Dunning (1993). [Accurate Methods for the Statistics of Surprise and Coincidence](https://aclanthology.org/J93-1003/)
- [SQLite FTS5 trigram 与 CJK 检索](https://zenn.dev/kanseilink/articles/kanseilink-fts5-trigram-cjk-20260507?locale=en)
- TCER 内部：`doc/jev-research.md`（jev 语言、校准、逃生口）、CLAUDE.md #38/#40


---

## 附录 C · 实施状态（2026-09-28）

| 阶段 | 状态 | 落点 |
|---|---|---|
| P0 地基 | ✅ | `concepts/schema.py`（表）· `rbac.py`（角色 + 本人授权，admin 也看不了他人个人视图）· `profile.py`（职能 CSV 导入 / 本人填写 / 授权开关）· `disclosure.py`（唯一出口）· `governance.py`（提案 → 评审 → 版本；质疑必须回复；与客户端 `termbase.json` 同构导入导出）|
| P1 零模型信号 | ✅ | `ingest.py`（说话方分层 / 注入块剥离 / 脱敏 / 代码只留标识符）· `lexicon.py`（复用客户端术语匹配；CJK n-gram + 邻接字多样性 + G² 新词挖掘）· `signals.py`（误解 / 换词 / 子代理保真）· `pipeline.py`（授权门 + 内容哈希增量 + 撤回即清除）· `views.py` · 前端四页 |
| 客户端 | ✅ | `upload_config.semantic_consent`（默认关）· 上传对话框复选框 · payload 仅「带明细 + 实名」时置位 |
| P2 问卷测评 | ⏳ | CCT / Pathfinder 未开始 |
| P3 语义层 | ⏳ | 视图已留 `semantic: {enabled:false}` 占位，不输出假数字 |

**实现中对设计的修正**：
1. 误解事件的判定比 §5.4 更保守：纠正必须**重提该术语**（强证据），或紧接下一轮且中间 AI 改过文件（弱证据，单独标注「强证据占比」）；纠正不重提、也没改文件的一律不归因。
2. AI 换词只在「用户本轮只提了一个术语、AI 只引入了一个新术语」时成对，多对多不判。
3. 新词挖掘加了**邻接字多样性**过滤（两侧各 ≥2 种邻接，且至少一侧有 ≥2 种真实邻接字），否则 n-gram 会大量产出「战令系统第」「秒改成」这类跨词碎片。
4. 时间窗（趋势 / 跨职能共用）以**数据最新时间**为锚点，而非服务器当前时间。
5. 概念页隐藏顶部时间 / 成员 / 项目筛选（概念数据不按这些维度过滤，保留会误导）。

**验证**：`tests/test_server_concepts.py`（31 例，含植入效应重建、授权撤回清除、红线、治理流程）；`seed_concepts.py` 造数 + 无头浏览器走查四个页面与起草提案流程。
