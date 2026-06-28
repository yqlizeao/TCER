# 架构与工程规范

## 源码模块

| 模块 | 职责 |
|------|------|
| `tcer/core/reader.py` | JSONL 发现/解析、isMeta 过滤、head/tail 取样、时间戳归一化、message.usage 聚合 |
| `tcer/core/paths.py` | 定位 `~/.claude`、项目哈希编解码 |
| `tcer/core/loc.py` | git-free 代码量统计：session_loc（工具调用统计增删）+ tree_loc（扫描工作目录） |
| `tcer/core/metrics.py` | 全部公式：TCER/CHR/CPE/CAF/TTAF/TA-TCER/PSAC/NCPI/churn/CTEI + 评级 + 逐模型成本 |
| `tcer/core/pricing.py` | 逐模型计价：从 `tcer/config/model_pricing.json`（≈162 模型）解析 $/MTok |
| `tcer/core/models.py` | 数据类：TokenUsage / ModelUsage / SessionMeta / SessionReport |
| `tcer/core/analyze.py` | 编排层：项目→会话→指标，GUI 调用 |
| `tcer/core/calibrate.py` | LOC 精度校准（库，GUI 不暴露——纯离线工具不依赖 git） |
| `tcer/core/format.py` | 纯值格式化器（千分位/百分比/时间戳/模型名） |
| `tcer/core/export.py` | JSON/CSV/Markdown 序列化 + CTEI 排名数据 + 文本条形图 |
| `tcer/gui/` | Tkinter 图形界面（MVC 架构） |

## GUI MVC 架构

```
tcer/gui/
├── __init__.py     # main() 入口
├── __main__.py     # python -m tcer.gui 兼容入口
├── app.py          # 控制器：状态/后台线程/事件装配
├── theme.py        # 颜色/字体/Style 常量
├── metric_defs.py  # 指标元数据单一数据源（中文标签/单位/说明/分层）
├── widgets.py      # 通用组件：Tooltip/ScrollFrame/Card/MetricCell
├── views.py        # 面板：FilterBar/ProjectColumn/SessionColumn/MetricPanel + 图表
└── popups.py       # 弹窗：模型详情/工具调用/高频改动文件
```

### 分层职责

| 层 | 模块 | 职责 |
|----|------|------|
| Model | tcer/core/* | 数据采集、公式、编排 |
| 格式/导出 | core/format.py + core/export.py | 值格式化、JSON/CSV/Markdown |
| 数据定义 | gui/metric_defs.py | 指标元数据（中文标签/单位/说明/分层） |
| 通用件 | gui/widgets.py | 可复用 Tk 组件 |
| 面板 | gui/views.py | 各面板、图表、弹窗 |
| 控制器 | gui/app.py | 状态、后台线程、事件 |

## 工程规范

1. **禁止新增中间产物**：截图、临时脚本、草稿 md 等用完即删，不提交。必要的经验/理解写入 CLAUDE.md。
2. **GUI 全中文、代码用缩写**：界面显示完整中文（如「缓存命中率」而非「CHR」），仅 TCER 保留英文缩写。代码标识符可用缩写（chr/ctei/ncpi…）。
3. **运行方式**：`python -m tcer`（从仓库根目录，绿色免安装）。
4. **库层不动**：tcer/core/ 下模块有完整测试覆盖，改动需谨慎。GUI 改动集中在 tcer/gui/。
5. **纯离线**：不依赖任何版本管理工具（git 等）、不做任何联网操作。所有数据来自本地 `~/.claude/` 目录的 JSONL 文件。

## 关键设计决策

### LOC 的 git-free 设计

净增 LOC 来自会话自身文件改写工具调用，不依赖 git。历史方案曾用 `git log --numstat`，已废弃：git 净增只反映最终提交、受提交习惯影响、且时间窗归因不可靠。

### 按 message.id 去重

一次 assistant API 响应常被拆成多行写入 JSONL（thinking / text / 每个 tool_use 各一行），每行重复携带 usage。必须按 message.id 只计一次，否则 token 成倍虚高（实测全局 55.9% 重复计数）。tool_use 块各自唯一，LOC 统计不受影响。

### 逐模型计价

TokenUsage.per_model 按 message.model 分桶，merge 自动合并。cost_usd 对每个分桶用各自价表算成本再相加，混用多模型的会话也精确。

## 测试覆盖

76 项测试覆盖：reader（16）/ paths（3）/ metrics（20）/ pricing（5）/ loc（8）/ export（9）/ baselines（3）/ metric_defs（3）/ calibrate（9）。
