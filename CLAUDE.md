# TCER — Token-to-Code Efficiency Ratio

## 项目目标

基于真实 Claude Code 会话数据，构建多维 AI 编程效率计量体系（TCER/CTEI）。

- **GUI-only**：`python -m tcer` 启动桌面界面
- **纯离线**：不依赖 git、不做联网操作，数据来自本地 `~/.claude/` JSONL 文件
- **零依赖**：纯 Python ≥3.11 标准库

## 快速开始

```bash
python -m tcer            # 启动 GUI
python -m pytest tests/   # 运行测试（76 项）
```

## 仓库结构

```
TCER/
├── tcer/                  Python 包
│   ├── core/              核心库（reader / loc / metrics / pricing / models / paths / analyze / export / format）
│   ├── gui/               GUI（app / theme / metric_defs / widgets / views / popups）
│   └── config/            配置（model_pricing.json / composite_baselines.json）
├── tests/                 测试（76 项）
└── doc/                   详细文档
    ├── metrics.md         指标公式与计算步骤
    ├── data-format.md     JSONL 数据格式与 LOC 原理
    └── architecture.md    MVC 架构与工程规范
```

## 核心指标

| 指标 | 含义 | 公式要点 |
|------|------|----------|
| **TCER** | Token 转码效率比 | 净增行 ÷ 百万Token |
| **CHR** | 缓存命中率 | 缓存读取 ÷ 总输入 |
| **CPE** | 千行代码成本 | 成本 ÷ 净增行 × 1000 |
| **CTEI** | 综合效率指数 | (TCER/基准)×(NCPI/基准)×(CPE基准/CPE)×(1+CHR×0.5) |

> 指标公式、计算步骤、算例：[doc/metrics.md](doc/metrics.md)

## 工程规范

1. **禁止中间产物**：用完即删，不提交。经验写入本文件。
2. **GUI 全中文**：界面完整中文，仅 TCER 保留英文缩写。代码用缩写。
3. **纯离线**：GUI 不暴露任何需要 git 或网络的功能。
4. **库层不动**：`tcer/core/` 有完整测试覆盖，改动需谨慎。
5. **运行方式**：`python -m tcer`（绿色免安装）。

## 关键注意事项

1. **按 message.id 去重**：一次 API 响应被拆成多行写入 JSONL，每行重复携带 usage。必须按 id 只计一次（实测 55.9% 重复计数）。详见 [doc/data-format.md](doc/data-format.md)。
2. **LOC 不依赖 git**：净增代码来自会话内工具调用回放。Write 覆写已有文件会高估（F1 风险），`unseen_writes` 计数暴露上界。
3. **逐模型计价**：TokenUsage.per_model 按 message.model 分桶，混用多模型会话也精确。价表 `tcer/config/model_pricing.json`（≈160 模型）。
4. **子代理并入父会话**：Token 与 LOC 保留真实成本，不单独计为 session。

> 完整架构说明：[doc/architecture.md](doc/architecture.md)
> 数据格式细节：[doc/data-format.md](doc/data-format.md)
