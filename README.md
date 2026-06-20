# TCER — AI 编码 Token→Code 科学计量

基于真实 Claude Code 会话数据，度量「每消耗多少 Token、产出多少有效代码」的多维效率体系。指标框架为原创的五层体系，`tcer/` 是其完整可运行的 Python 实现。**纯离线**：不依赖任何版本管理工具、不做任何联网操作，所有数据来自本地 `~/.claude/` 目录。

---

## 仓库结构

```
.
├── CLAUDE.md                                    # 项目规格文档
└── tcer/                                        # 实现：纯 Python 标准库，零第三方依赖
    ├── src/tcer/                                # 源码（核心库 + gui 包）
    ├── tests/                                   # 测试（7 文件，76 项全过）
    ├── conftest.py                              # 让 python -m pytest 找到 src/
    └── README.md                                # 英文说明
```

## 这个工具做什么

`tcer` 直接解析 Claude Code 写在 `~/.claude/projects/<项目哈希>/*.jsonl` 的会话文件（**离线、零 API 调用、零埋点**），汇总 Token 用量、统计代码增删，算出一套效率指标，并提供图形界面查看方式。

### 源码模块

| 模块 | 职责 |
|------|------|
| `reader.py` | JSONL 发现/解析、`isMeta` 过滤、head/tail 取样、时间戳归一化、`message.usage` 聚合 |
| `paths.py` | 定位 `~/.claude`、项目哈希编解码 |
| `loc.py` | **git-free** 代码量统计：`session_loc`（从工具调用统计增删）+ `tree_loc`（扫描工作目录） |
| `metrics.py` | 全部公式：TCER/CHR/CPE/CAF/TTAF/TA-TCER/PSAC/NCPI/churn/**CTEI** + 评级 + 逐模型成本 |
| `pricing.py` | 逐模型计价：从 `data/model_pricing.json`（≈160 模型）解析 `$/MTok`，未知模型回退 default |
| `models.py` | 数据类（`TokenUsage` / `ModelUsage` / `SessionMeta` / `SessionReport`） |
| `analyze.py` | 编排层：项目→会话→指标，GUI 调用 |
| `calibrate.py` | LOC 精度校准：对照 git 历史量化工具调用 LOC 偏差 |
| `format.py` | 纯值格式化器（千分位/百分比/时间戳/模型名），GUI 与导出层共用 |
| `export.py` | JSON/CSV/Markdown 序列化 + CTEI 排名数据 + 文本条形图 |
| `gui/` | Tkinter 图形界面（MVC 架构：app/theme/metric_defs/widgets/views/popups） |

## 核心指标

| 指标 | 含义 | 公式要点 |
|------|------|----------|
| **TCER** | Token 转码效率比（行/百万Token） | 净增代码行 ÷ 百万Token |
| **CHR** | 缓存命中率 | 缓存读取 ÷ 总输入 |
| **CPE** | 有效千行代码成本 | 成本 ÷ 净增行 × 1000 |
| **churn** | 代码返工率（L3 质量层） | 删除行 ÷ 写入行 |
| **NCPI** | 净代码产出指数 | 净增行 ÷ 代码库累计行 |
| **CAF** | 缓存调整因子 | 总输入 ÷（普通输入 + 缓存写入） |
| **TTAF / TA-TCER** | 任务类型调整 | TA-TCER = TCER ÷ TTAF |
| **PSAC** | 项目阶段调整系数 | 抵消大代码库的结构性 TCER 下降 |
| **CTEI** | 复合 Token 效率指数（综合评分） | (TCER/基准)×(NCPI/基准)×(基准CPE/CPE)×(1+CHR×0.5) |

CTEI 评级：**优秀 >2 · 良好 1~2 · 中等 0.5~1 · 低效 0.1~0.5 · 极端低效 <0.1**。
实现已验证能**复现原始框架发布的逐会话评分（误差 <0.1%）**。成本按**各模型 list 价**分别估算并求和（价表见 `tcer/src/tcer/data/model_pricing.json`，≈160 模型；未知模型回退 Anthropic 标价 input \$3 / output \$15 / cache-write \$3.75 / cache-read \$0.30 每百万 Token）。

**软指标（TTAF / 基准值 / CHR 权重）可配置**：`tcer/src/tcer/data/composite_baselines.json` 存储 CTEI 基准、TTAF 系数、PSAC 回归参数、CHR 权重。默认值来自原始框架的 16 会话参考数据集，你可以：
- 手改 JSON 覆盖任意系数
- 用 GUI 的「计算个人基准」按钮从自己积累的数据计算中位数/均值，建立个人基准（框架 §8.3 建议）

### 指标健康参考范围（经验值）

| 指标 | 优秀 | 良好 | 需改进 | 说明 |
|------|------|------|--------|------|
| **TCER** | >80 | 40–80 | <40 | 基准 76.59（框架中位数）。新功能开发通常 >50，调试/重构偏低 |
| **CHR** | >85% | 70–85% | <70% | 缓存命中率。>80% 说明提示词稳定、上下文复用好 |
| **CPE** | <\$10 | \$10–\$30 | >\$30 | 每千行成本。受任务类型影响大（调试贵、新功能便宜） |
| **Churn** | <5% | 5–15% | >15% | 代码返工率。低表示"一次到位"，高表示反复修改 |
| **I/O Ratio** | 150–250 | 80–150 | <80 或 >300 | 输入输出比。太低=输出冗长（啰嗦），太高=输入过多（效率低） |

这些范围来自 TCER 项目自身数据与原始框架参考集的观察，**不是理论阈值**——实际"健康值"因任务类型、代码库规模、个人风格而异。用 GUI 的「计算个人基准」按钮建立你自己的参考线。

## 关键事实：LOC 不依赖 git

净增代码**不来自 git**，而是逐条回放会话里的文件改写工具调用（`Write` / `Edit` / `MultiEdit` / `NotebookEdit`）统计增删：

- **零外部依赖**——不需要 git，任何文件夹都能算；
- **按会话精确归因**——不再有「提交落在时间窗间隙」的误差；
- **忠实反映生成量**——计入多次重写/实验，是更公平的 Token→Code 分母（不受提交习惯影响，因此通常大于最终进 git 的净增）。

代码库累计行数由扫描工作目录得到（`tree_loc`，跳过 `.git`/`node_modules`/`__pycache__` 等）。

## 科学计算步骤（可复现）

从原始会话文件到 CTEI 综合评分，全过程分七步。每步对应源码中的具体函数，可逐项复算。

### 第 1 步 · 采集与清洗（`reader.iter_messages`）

1. 递归收集 `~/.claude/projects/<项目哈希>/**/*.jsonl`（含 `subagents/` 子目录）。
2. 逐行解析 JSON，**跳过** `isMeta: true` 的元数据行、`queue-operation` 等非对话行。
3. 主会话与子代理会话按目录区分（`subagents/` 下的为子代理）。

### 第 2 步 · Token 用量聚合（`reader.aggregate_usage`）

遍历每条 `type: assistant` 消息的 `message.usage`，对四个计费字段求和；**整条全为 0 的回复（如纯 thinking）跳过**并计入 `empty_usage_skipped`，避免拉低均值。

> **★ 按 `message.id` 去重（关键）**：一次 API 响应常被拆成多行写入 JSONL（thinking / text / 每个 tool_use 各一行），且**每行重复携带同一份 `usage`**。必须按 `message.id` 只计一次，否则 token 与回合数成倍虚高——实测本地数据集若不去重，**全局 55.9% 的 token 是重复计数**。`tool_use` 块各自唯一，故 LOC 统计不受影响。

```
total_input = input + cache_write + cache_read
total       = total_input + output
```

会话级求和后，项目级再把各会话用量相加（`TokenUsage.merge`）。

### 第 3 步 · 成本估算（`metrics.cost_usd` + `pricing.resolve`，逐模型 list 价）

成本**按模型分别计价再求和**：每个模型的 token 用各自的 `$/MTok` 价表算钱，因此混用多模型的会话也精确。价表来自 cc-switch 的 `seed_model_pricing()`（≈160 模型，含官方 list 价），落盘为可手改的 `data/model_pricing.json`；`pricing.resolve(model)` 按「精确 id → 最长前缀 id（兼容 `[1m]`/日期后缀）→ default」解析，未知模型回退 `default`（Anthropic 通用标价）。

```
# 单个模型分桶的成本（reader 按 message.model 分桶，cost_by_model 给出逐模型明细）：
cost = ( input        × r["input"]
       + cache_write  × r["cache_write"]
       + cache_read   × r["cache_read"]
       + output       × r["output"] ) / 1_000_000      # r = 该模型的 $/MTok 价表

# default 回退价（等价于历史 Anthropic 公式）：
#   input 3.00 / cache_write 3.75 / cache_read 0.30 / output 15.00 ($/MTok)
```

> 缓存读取单价仅为普通输入的 1/10，所以 CHR（缓存命中率）对成本影响极大。

### 第 4 步 · 代码量统计（git-free，`loc.session_loc`）

按时间顺序回放会话里的文件改写工具调用，只统计代码后缀文件（`.py/.js/.ts/.md/...`）。`nlines(s)` = 字符串行数（universal newlines，与 `tree_loc` 口径一致）。维护「本会话内各文件当前行数」`file_lines`：

| 工具 | 增 (added) | 删 (deleted) |
|------|-----------|--------------|
| `Write`（新建/覆写） | `max(0, 新行数 − 旧行数)` | `max(0, 旧行数 − 新行数)`；旧行数取本会话内该文件当前值 |
| `Edit` | `max(0, 新串行数 − 旧串行数)` | `max(0, 旧串行数 − 新串行数)` |
| `MultiEdit` | 各子编辑按 `Edit` 规则求和 | 同左 |
| `NotebookEdit` | 非删除模式：新单元行数 | 删除模式：被删行数 |

```
net_loc = added − deleted            # 净增代码行（按会话精确，项目级再求和）
```

**F1 风险（Write 覆写已有文件）**：`file_lines` 在每个会话开始时为空，所以 `Write` 首次遇到某文件时「旧行数」被假设为 0——对新文件正确，对覆写已有文件错误（整个新内容算 added，删除被漏掉）。`Edit` 只看 delta，不受影响。报告的 `unseen_writes` 计数（Quality L3 层）= 本会话首次 Write 的文件数，是 F1 暴露面上界。实测 TCER 的真实工作流（新文件用 Write、改老文件用 Edit）**偏差 0%**，但换了工作流就会咬人。用 GUI 的「校准 LOC」按钮（对照 git 历史）可精确量化偏差。核心保持 git-free，风险通过计数器可见。

代码库累计行数 `loc_accumulated` = `tree_loc(工作目录)`：扫描目录、跳过 `EXCLUDE_DIRS`、累加代码后缀文件的行数（text mode universal newlines，与 `session_loc` 口径一致）。

### 第 5 步 · 基础效率指标（L2 / L4，`metrics.compute`）

```
TCER        = net_loc / (total / 1_000_000)          # 行/百万Token
CHR         = cache_read / total_input               # 缓存命中率
I/O Ratio   = total_input / output
$/Mt        = cost / (total / 1_000_000)
CPE         = cost / net_loc × 1000                  # 美元/千行
```

### 第 6 步 · 修正与质量指标（L3 / L5 中间量）

```
NCPI        = net_loc / loc_accumulated              # 净产出指数（贡献密度）
CAF         = total_input / (input + cache_write)    # 缓存调整因子，≥1
churn       = deleted / added                        # 返工率（L3 质量）
TA-TCER     = TCER / TTAF[task_type]                 # 任务调整后 TCER
PSAC        = 83.64 / (83.64 − 0.000866 × loc_accumulated)
phase-adj   = TCER × PSAC
```

TTAF 取值（原始框架 §6.4）：新功能 1.00 · 功能扩展 0.85 · 调试 0.40 · 重构 0.50 · 审查 0.20 · 测试 0.90。

### 第 7 步 · CTEI 综合指数与评级（`metrics.ctei` / `metrics.grade`）

```
CHR_factor  = 1 + CHR × 0.5
CTEI = (TCER / TCER_baseline)
     × (NCPI / NCPI_baseline)
     × (CPE_baseline / CPE)
     × CHR_factor
```

基准（默认取原始框架 16-会话参考数据集的中位数/均值，可在 GUI 中覆盖）：
`TCER_baseline = 76.59`、`NCPI_baseline = 0.101`、`CPE_baseline = 8.22`。

评级：**优秀 >2 · 良好 1~2 · 中等 0.5~1 · 低效 0.1~0.5 · 极端低效 <0.1**。该实现已用测试断言能**复现原始框架发布的逐会话 CTEI（误差 <0.1%）**。

### 数值算例（一条会话走完全程）

假设某会话：`input=20,000`、`cache_write=180,000`、`cache_read=4,700,000`、`output=100,000`；工具调用净增 `added=420 / deleted=20`；代码库累计 `loc_accumulated=10,000`；任务类型 `feature`（TTAF=1.0）。

| 步骤 | 计算 | 结果 |
|------|------|------|
| total_input | 20,000+180,000+4,700,000 | 4,900,000 |
| total | 4,900,000+100,000 | 5,000,000 |
| cost | (20k·3 + 180k·3.75 + 4.7M·0.30 + 100k·15)/1e6 | **\$3.645** |
| net_loc | 420 − 20 | 400 |
| TCER | 400 / 5.0 | **80.0** 行/百万Token |
| CHR | 4,700,000 / 4,900,000 | 95.9% |
| CPE | 3.645 / 400 × 1000 | \$9.11/千行 |
| NCPI | 400 / 10,000 | 0.040 |
| CAF | 4,900,000 / 200,000 | 24.5 |
| churn | 20 / 420 | 4.8% |
| PSAC | 83.64 / (83.64 − 8.66) | 1.115 |
| CHR_factor | 1 + 0.959×0.5 | 1.480 |
| **CTEI** | (80/76.59)×(0.040/0.101)×(8.22/9.11)×1.480 | **≈ 0.55 → 中等** |

## 运行（绿色免安装）

纯 Python ≥3.11 标准库，**无需安装**：不用 `pip`、不改 PATH、完全便携。直接从 `src/` 目录用 `python -m` 运行：

```bash
cd tcer/src
python -m tcer                                    # 启动图形界面（主入口，全中文）
python -m tcer.gui                                # 同上（兼容入口）
```

> 从 `src/` 目录运行会自动把 `tcer` 包加入导入路径，零配置——不需要 editable 安装，也不会往 PATH 里塞 console 命令。

**子代理（subagent）会并入其父会话**作为同一个 session：它们的 Token 与代码计入父会话（保留真实成本），不单独计为一个 session。

### 图形界面

`python -m tcer`（从 `tcer/src` 目录运行）打开桌面窗口。三栏布局：左侧项目列表、中间会话列表、右侧面板。右侧 Notebook 含三个标签页：

- **五层指标**：45 个指标，按层级分组（L0 数据 / L1 原始 / L2 效率 / L3 质量 / L4 经济 / L5 综合），鼠标悬停可看每个指标的中文解释。颜色分级：红=终极指标 TCER，橘=复合指标，蓝=高级指标，白=基础指标。L5 含当前 CTEI 基准参考值。
- **综合效率指数排名**：条形图，按 CTEI 评级着色。
- **趋势**：折线图，TCER/CTEI/CPE/缓存命中/成本的时间趋势（横轴为会话开始时间），TCER 趋势叠加框架基准线。

顶部筛选栏含任务类型、时间范围、视图切换。功能按钮：**导出**（JSON/CSV/Markdown）。纯标准库 `tkinter`，分析在后台线程运行不卡界面。纯离线，不依赖 git。

## 数据来源

Claude Code 把会话写在 `~/.claude/`（Windows 为 `%USERPROFILE%\.claude\`）：

- `projects/<项目哈希>/<sessionId>.jsonl` — 完整对话（含 `message.usage` Token 用量与工具调用 `input`）
- `projects/<项目哈希>/<sessionId>/subagents/*.jsonl` — 子代理会话，按 `<sessionId>` 并入对应父会话

详见 [CLAUDE.md](CLAUDE.md)。

## 当前状态

- 原创的五层指标体系（L1 原始 / L2 效率 / L3 质量 / L4 经济 / L5 综合）已全部实现；L3 目前覆盖 churn 率与 `unseen_writes` 计数（F1 暴露面），圈复杂度、覆盖率需引入 radon/lizard 等外部工具，留作 opt-in。
- 成本逐模型计价：价表 `data/model_pricing.json`（≈160 模型，源自 cc-switch），未知模型回退 default。
- **软指标可配置**：`data/composite_baselines.json` 存储 TTAF / CTEI 基准 / PSAC / CHR 权重，可手改或用 GUI「计算个人基准」按钮从积累数据建立个人基准。
- **测试 76 项全过**（`test_reader` / `test_paths` / `test_metrics` / `test_pricing` / `test_loc` / `test_baselines` / `test_metric_defs` / `test_export` + `test_calibrate`）。
- **硬数据可靠性已审计**：F1（Write 覆写已有文件）用 `unseen_writes` 计数暴露风险、`calibrate.py` 量化偏差（实测 0%）；F4（行数口径）已统一 `session_loc` 与 `tree_loc` 为 universal newlines；时间戳盲区（仅零 usage 回合的会话）已修复。
- GUI-only，`python -m tcer` 启动；纯 Python 标准库，零第三方运行时依赖；版本 0.3.0。

> 注：TTAF 取值以原始框架 §6.4 为准（重构 0.50、审查 0.20），代码、CLAUDE.md 与本文档三者一致。
