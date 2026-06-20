# TCER

> Token-to-Code Efficiency Ratio — 度量 AI 编程效率的离线分析工具

基于 Claude Code 会话数据（`~/.claude/` JSONL），多维度量化「每消耗多少 Token、产出多少有效代码」。原创五层指标体系，Tkinter 桌面界面，纯离线运行。

## 快速开始

```bash
python -m tcer
```

纯 Python ≥3.11 标准库，零依赖，免安装。

## 特性

- **45 项指标**：五层体系（数据 / 原始 / 效率 / 质量 / 经济 / 综合），鼠标悬停中文解释
- **综合效率指数**：多维合成评分，>2 优秀 / <0.1 极端低效
- **逐模型计价**：≈160 模型价表，混用多模型会话也精确
- **git-free LOC**：从工具调用回放统计代码增删，零外部依赖
- **可视化**：CTEI 排名条形图 + 趋势折线图
- **导出**：JSON / CSV / Markdown

## 结构

```
TCER/
├── tcer/                  Python 包
│   ├── core/              核心库（10 模块）
│   ├── gui/               GUI（MVC 架构，6 模块）
│   └── config/            价表 + 基准配置
├── tests/                 测试（76 项全过）
└── doc/                   详细文档
```

## 核心指标

| 指标 | 含义 |
|------|------|
| TCER | Token 转码效率比（行/百万Token） |
| CHR | 缓存命中率（越高越省钱） |
| CPE | 千行代码成本（可跨项目对比） |
| CTEI | 综合效率指数（多维合成评分） |

详见 [doc/metrics.md](doc/metrics.md)

## 文档

- [指标公式与计算步骤](doc/metrics.md)
- [JSONL 数据格式](doc/data-format.md)
- [架构与工程规范](doc/architecture.md)
- [项目规格](CLAUDE.md)

## 许可

MIT
