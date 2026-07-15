# TCER — Token-to-Code Efficiency Ratio

## 项目目标

基于真实 AI 编程助手会话数据，构建多维 AI 编程效率计量体系（TCER/CTEI）。支持四个数据来源：Claude Code（`~/.claude`）、Codex（`~/.codex`）、OpenCode（`~/.local/share/opencode`）、Grok / grok build CLI（`~/.grok`）。

- **GUI-only**：`python -m tcer` 启动桌面界面
- **纯离线**：不依赖 git、不做联网操作，数据来自本地 JSONL / SQLite 文件
- **零依赖**：纯 Python ≥3.11 标准库

## 快速开始

```bash
python -m tcer            # 启动 GUI
python -m pytest tests/   # 运行测试（156 项）
```

## 仓库结构

```
TCER/
├── tcer/                  Python 包
│   ├── core/              核心库（reader / loc / metrics / pricing / models / paths / analyze / export / format）
│   ├── gui/               GUI（app / theme / metric_defs / widgets / views / popups）
│   └── config/            配置（model_pricing.json / composite_baselines.json）
├── tests/                 测试（156 项）
└── doc/                   详细文档
    ├── metrics.md         指标公式与计算步骤
    ├── data-format.md     JSONL 数据格式与 LOC 原理
    └── architecture.md    MVC 架构与工程规范
```

## 指标分类（6 组 · 52 项）

GUI 指标按关注维度分为 6 组（扁平，无层级关系）：

| 组 | 名称 | 数量 | 内容 |
|---|------|------|------|
| G1 | 会话概况 | 10 | 元数据（时长、模型、回合、工具调用、用户消息等） |
| G2 | Token 用量 | 5 | 原始消耗（输入/输出/缓存） |
| G3 | 缓存效率 | 6 | 缓存利用率比率 |
| G4 | 代码产出与质量 | 16 | LOC、返工率、工具行为比率、搜索后编辑比等 |
| G5 | 成本分析 | 3 | 金钱代价 |
| G6 | 综合评分 | 12 | 效率指标 + CTEI 评分 + 基准参数 |

**字体颜色**：白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考。

> 指标公式、计算步骤、算例：[doc/metrics.md](doc/metrics.md)

## 指标唯一真理源（SSOT）

`tcer/gui/metric_defs.py` 是**所有指标展示的唯一真理源**——名称 / 提示 / 单位 / 格式 / 好坏方向 / 取值，4 个页签（指标分类 / 排名 / 趋势 / 模型对比）与全部 popups（雷达等）都从这里取，禁止各处再自管：

- **会话/项目级**：`raw_value(report, key)`（图表用数值）、`format_value(key, native)`（显示串）、`display(report, key)`、`report_values(report)`。每个 `Metric` 带 `fmt` 规格（`int`/`pct`/`float:N`/`money`/`text`）。
- **逐模型**：`MODEL_GROUPS` + `model_raw` / `model_display` / `model_tip`，同义指标转调 `format_value` 与会话级逐字节一致；**例外**：模型对比是 N 列并排，Token 数等大数量级用 K/M 紧凑显示（布局需要），比率/百分比仍与网格一致。
- **CTEI 因子分解**：`CTEI_FACTORS`（名称/公式）。
- **评级体系**：`core/metrics.GRADE_BANDS`（名称+阈值，best→worst）是 grade 的唯一源；`grade()`、排名分布条、趋势 CTEI 带都从它派生。


## 工程规范

1. **禁止中间产物**：用完即删，不提交。经验写入本文件。
2. **GUI 全中文**：界面完整中文，仅 TCER 保留英文缩写。代码用缩写。
3. **纯离线**：GUI 不暴露任何需要 git 或网络的功能。
4. **库层不动**：`tcer/core/` 有完整测试覆盖，改动需谨慎。
5. **运行方式**：`python -m tcer`（绿色免安装）。

## 关键注意事项

1. **按 message.id 去重**：一次 API 响应被拆成多行写入 JSONL，每行重复携带 usage。必须按 id 只计一次（实测 55.9% 重复计数）。**边界**：空字符串 `""` 视为无 id，逐条计数。**ccswitch 兼容**：mimo 消息第一行是 thinking 桩（usage=0），第二行才有真实 usage；零 usage 行会释放 id 锁（`seen.discard`），允许后续行贡献真实 token。**Grok 差异**：grok build 每 turn 恰好一条 `turn_completed` 携带权威 usage，无多行重复问题，直接累加；错误回合的空 usage（字段为 null）计入 `empty_usage_skipped`，不虚增回合数。详见 [doc/data-format.md](doc/data-format.md)。
2. **LOC 不依赖 git**：净增代码来自会话内工具调用回放。Write 覆写已有文件会高估（F1 风险），`unseen_writes` 计数暴露上界。**Grok 同理**：`search_replace`（`file_path`/`old_string`/`new_string`）与 Claude Edit 同构，行差即净增；`write` 整文件写入计 `unseen_writes`。
3. **逐模型计价**：TokenUsage.per_model 按 message.model 分桶，混用多模型会话也精确。价表 `tcer/config/model_pricing.json`（≈175 模型）。**四级匹配**（`pricing._match_id`，按优先级）：①精确 ②归一化精确（小写、去 `-`/`_`、`5p2`→`5.2` 且 `5-6`→`5.6`，先于前缀以防 `glm-5p2` 误中 `glm-5`、`gpt-5-6-sol` 误中 `gpt-5`）③前缀（`claude-opus-4-8[1m]`→`claude-opus-4-8`）④反向前缀（短名 `claude-opus-4-6`→带日期 key）。每条先试原 id 再试末段 path（剥 `z-ai/`、`accounts/fireworks/models/` 等供应商前缀）。`pricing.normalize()` 把 per_model key 归一化到价表规范 key；`pricing.table_key()` 返回 None 即走 default 回退（GUI 价表浮窗据此标"默认配置价"）。**Grok**：`turn_completed.usage.modelUsage` 同样按模型分桶（如 `grok-4.5`）。
4. **过滤 `<synthetic>`**：ccswitch 在 429 限流或系统占位时注入伪 assistant 消息，`model` 字段为 `<synthetic>`，usage 全为零。reader 层直接过滤，不计入 `models` 和 `per_model`。
5. **子代理并入父会话**：Token 与 LOC 保留真实成本，不单独计为 session。
6. **时序分析**：`ToolOp(turn, tool, path)` 记录每个工具调用的回合序号和文件路径。**搜索后编辑比**按回合就近匹配（搜索后 3 回合内出现 Write/Edit 即算跟进，不绑定具体文件——真实 Grep/Glob 的 `path` 多为目录）；**先读后写率**等仍用 file_path。merge 时 rebase turn 编号保证聚合后时序连续。
7. **任务类型体系**：3 大类（代码创作/代码维护/非编码），每类有 TTAF 系数。`ntcer = tcer / ttaf` 归一化后可跨任务类型公平比较。`ta_tcer` 保留为向后兼容别名。
8. **返工率 = 自返工率**：churn 只计「本会话先写入、随后又被自己删除/替换」的行（`loc.SessionLoc.rework_deleted`，封顶于本会话已写入该文件的行数）；删除会话之外的既有代码属正常编辑，不计入。
9. **聚合层禁用 NCPI/CTEI/评级**：这三项是单会话概念（NCPI = 净增 ÷ 当前代码库行数）。聚合时净增是全生命周期累计、分母是当前快照，比值常 >1 致 CTEI 虚高，故 `analyze` 在聚合报告里置空它们；TCER/PSAC/NTCER 作为聚合仍有效。
10. **自定义 Claude 配置目录自动识别**：用户常以 `CLAUDE_CONFIG_DIR=%USERPROFILE%\.zclaude`（或其他自定义名）启动 Claude Code 以隔离 `.claude`。该环境变量只在 Claude 进程内、TCER 读不到；故 `paths.claude_config_dirs()` 以规范目录（`CLAUDE_CONFIG_DIR` 或 `~/.claude`）为锚，扫描其**父目录**里所有结构匹配 Claude 的兄弟目录（`projects/<hash>/*.jsonl` 指纹），全部视为 Claude 根。`list_projects()`/`discover_jsonl(hash)` 跨所有根查找，**同 hash 跨根的会话合并**（不同项目各自出现）；结果按 `(home, CLAUDE_CONFIG_DIR)` 进程级缓存——会话期间新建的自定义配置目录需重启 TCER 才会出现。

> 完整架构说明：[doc/architecture.md](doc/architecture.md)
> 数据格式细节：[doc/data-format.md](doc/data-format.md)
