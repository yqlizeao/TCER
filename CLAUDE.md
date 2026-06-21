# TCER — Token-to-Code Efficiency Ratio

## 项目目标

基于真实 Claude Code 会话数据，构建多维 AI 编程效率计量体系（TCER/CTEI）。

- **GUI-only**：`python -m tcer` 启动桌面界面
- **纯离线**：不依赖 git、不做联网操作，数据来自本地 `~/.claude/` JSONL 文件
- **零依赖**：纯 Python ≥3.11 标准库

## 快速开始

```bash
python -m tcer            # 启动 GUI
python -m pytest tests/   # 运行测试（97 项）
```

## 仓库结构

```
TCER/
├── tcer/                  Python 包
│   ├── core/              核心库（reader / loc / metrics / pricing / models / paths / analyze / export / format）
│   ├── gui/               GUI（app / theme / metric_defs / widgets / views / popups）
│   └── config/            配置（model_pricing.json / composite_baselines.json）
├── tests/                 测试（97 项）
└── doc/                   详细文档
    ├── metrics.md         指标公式与计算步骤
    ├── data-format.md     JSONL 数据格式与 LOC 原理
    └── architecture.md    MVC 架构与工程规范
```

## 指标分类（6 组 · 49 项）

GUI 指标按关注维度分为 6 组（扁平，无层级关系）：

| 组 | 名称 | 数量 | 内容 |
|---|------|------|------|
| G1 | 会话概况 | 11 | 元数据（时长、模型、回合、工具调用、用户消息等） |
| G2 | Token 用量 | 5 | 原始消耗（输入/输出/缓存） |
| G3 | 缓存效率 | 6 | 缓存利用率比率 |
| G4 | 代码产出与质量 | 13 | LOC、返工率、工具行为比率、搜索后编辑比等 |
| G5 | 成本分析 | 3 | 金钱代价 |
| G6 | 综合评分 | 11 | 效率指标 + CTEI 评分 + 基准参数 |

**字体颜色**：白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考。

> 指标公式、计算步骤、算例：[doc/metrics.md](doc/metrics.md)

## 工程规范

1. **禁止中间产物**：用完即删，不提交。经验写入本文件。
2. **GUI 全中文**：界面完整中文，仅 TCER 保留英文缩写。代码用缩写。
3. **纯离线**：GUI 不暴露任何需要 git 或网络的功能。
4. **库层不动**：`tcer/core/` 有完整测试覆盖，改动需谨慎。
5. **运行方式**：`python -m tcer`（绿色免安装）。

## 关键注意事项

1. **按 message.id 去重**：一次 API 响应被拆成多行写入 JSONL，每行重复携带 usage。必须按 id 只计一次（实测 55.9% 重复计数）。**边界**：空字符串 `""` 视为无 id，逐条计数。**ccswitch 兼容**：mimo 消息第一行是 thinking 桩（usage=0），第二行才有真实 usage；零 usage 行会释放 id 锁（`seen.discard`），允许后续行贡献真实 token。详见 [doc/data-format.md](doc/data-format.md)。
2. **LOC 不依赖 git**：净增代码来自会话内工具调用回放。Write 覆写已有文件会高估（F1 风险），`unseen_writes` 计数暴露上界。
3. **逐模型计价**：TokenUsage.per_model 按 message.model 分桶，混用多模型会话也精确。价表 `tcer/config/model_pricing.json`（≈160 模型）。**双向前缀匹配**：JSONL 中的短名称（如 `claude-opus-4-6`）通过反向前缀匹配解析到定价表的带日期 key（`claude-opus-4-6-20260206`）。`pricing.normalize()` 将 per_model key 归一化为定价表规范 key。
4. **过滤 `<synthetic>`**：ccswitch 在 429 限流或系统占位时注入伪 assistant 消息，`model` 字段为 `<synthetic>`，usage 全为零。reader 层直接过滤，不计入 `models` 和 `per_model`。
5. **子代理并入父会话**：Token 与 LOC 保留真实成本，不单独计为 session。
6. **时序分析**：`ToolOp(turn, tool, path)` 记录每个工具调用的回合序号和文件路径，支持搜索后编辑比（3 回合窗口）和先读后写率等时序指标。merge 时 rebase turn 编号保证聚合后时序连续。
7. **任务类型体系**：3 大类（代码创作/代码维护/非编码），每类有 TTAF 系数。`ntcer = tcer / ttaf` 归一化后可跨任务类型公平比较。`ta_tcer` 保留为向后兼容别名。

> 完整架构说明：[doc/architecture.md](doc/architecture.md)
> 数据格式细节：[doc/data-format.md](doc/data-format.md)
