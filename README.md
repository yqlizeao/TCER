# TCER

> **Token-to-Code Efficiency Ratio** — 度量 AI 编程效率的离线分析工具

基于 Claude Code 会话数据（`~/.claude/` JSONL），多维度量化「每消耗多少 Token、产出多少有效代码」。

![主界面](img/主界面.png)

## 快速开始

```bash
python -m tcer
```

纯 Python ≥3.11 标准库，零依赖，免安装。

## 特性

### 49 项指标 · 6 组分类

| 组 | 名称 | 数量 | 内容 |
|---|------|------|------|
| G1 | 会话概况 | 11 | 元数据（时长、模型、回合、工具调用、用户消息等） |
| G2 | Token 用量 | 5 | 原始消耗（输入/输出/缓存） |
| G3 | 缓存效率 | 6 | 缓存利用率比率 |
| G4 | 代码产出与质量 | 13 | LOC、返工率、工具行为比率、搜索后编辑比等 |
| G5 | 成本分析 | 3 | 金钱代价 |
| G6 | 综合评分 | 11 | 效率指标 + CTEI 评分 + 基准参数 |

> **字体颜色**：白色 = 基准值/纯数据；黄色 = 含 magic number，仅作参考

### 综合效率指数排名

多维合成评分（CTEI），>2 优秀 / <0.1 极端低效。按项目聚合，一眼看出哪个项目效率最高。

![综合效率指数排名](img/综合效率指数排名.png)

### 趋势分析

按时间维度追踪效率变化，支持按周/月/全部筛选。

![趋势](img/趋势.png)

### 六维效率雷达

综合效率、缓存命中、千行成本、返工率、读写比、编码效率六维可视化，绝对刻度归一化。

![子窗口-雷达图](img/子窗口-雷达图.png)

### 逐模型详情

四色堆叠条展示每种模型的 Token 构成（输入/输出/缓存写入/缓存读取），支持 ≈160 模型定价，双向前缀匹配 + 归一化。

![子窗口-模型使用详情](img/子窗口-模型使用详情.png)

### 成本明细

按模型成本降序排列，显示 Token 效率（每美元 Token 数），前三名金银铜配色。

![子窗口-成本明细](img/子窗口-成本明细.png)

### 工具调用统计

成功/错误双色堆叠条，统计摘要 + 详情。

![子窗口-工具调用](img/子窗口-工具调用.png)

### 用户消息

卡片式布局，蓝色序号徽章 + 字符数统计。

![子窗口-用户消息](img/子窗口-用户消息.png)

## 核心指标

| 指标 | 含义 | 字体色 |
|------|------|--------|
| TCER | Token 转码效率比（行/百万Token） | ⬜ 白 |
| NTCER | 归一化效率（TCER ÷ 任务类型系数） | ⬜ 白 |
| CHR | 缓存命中率（越高越省钱） | ⬜ 白 |
| CPE | 千行代码成本（可跨项目对比） | ⬜ 白 |
| CTEI | 综合效率指数（多维合成评分） | 🟨 黄 |
| 搜索后编辑比 | 3 回合内搜索→编辑的转化率 | ⬜ 白 |
| 先读后写率 | 写入文件之前被读过的比例 | ⬜ 白 |

> 详见 [doc/metrics.md](doc/metrics.md)

## 任务类型体系

3 大类任务，每类有独立的调整系数（TTAF）：

| 大类 | 系数 | 典型 TCER | 说明 |
|------|------|-----------|------|
| 代码创作 | 1.0 | 60-120 | 新功能开发、功能扩展、测试编写 |
| 代码维护 | 0.45 | 25-65 | 调试排查、代码重构 |
| 非编码 | 0.2 | 0-30 | 代码审查、调研研究 |

`NTCER = TCER ÷ TTAF`，归一化后可跨任务类型公平比较。

## 架构

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

## 关键设计

- **按 message.id 去重**：一次 API 响应被拆成多行写入 JSONL，必须按 id 只计一次
- **ccswitch 兼容**：mimo 消息的 thinking 桩（usage=0）会释放 id 锁，允许后续行贡献真实 token
- **`<synthetic>` 过滤**：ccswitch 429 限流注入的伪消息，usage 全为零，reader 层直接过滤
- **双向前缀匹配**：JSONL 短名称（`claude-opus-4-6`）自动解析到定价表带日期 key（`claude-opus-4-6-20260206`）
- **git-free LOC**：从工具调用回放统计代码增删，零外部依赖

> 详见 [CLAUDE.md](CLAUDE.md)

## 文档

- [指标公式与计算步骤](doc/metrics.md)
- [JSONL 数据格式](doc/data-format.md)
- [架构与工程规范](doc/architecture.md)
- [项目规格](CLAUDE.md)

## 许可

MIT
