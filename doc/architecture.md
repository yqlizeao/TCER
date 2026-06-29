# 架构与工程规范

## 源码模块

| 模块 | 职责 |
|------|------|
| `tcer/core/reader.py` | Claude JSONL 发现/解析、isMeta 过滤、head/tail 取样、时间戳归一化、message.usage 聚合 |
| `tcer/core/codex_reader.py` | Codex JSONL 发现/解析、cwd 项目分组、token_count 聚合、工具调用/运行环境/上下文/限流/补丁事件统计、apply_patch LOC |
| `tcer/core/paths.py` | 定位 `~/.claude` / `~/.codex`、项目哈希编解码、统一项目引用 |
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
5. **纯离线**：不依赖任何版本管理工具（git 等）、不做任何联网操作。所有数据来自本地 `~/.claude/` 与 `~/.codex/` 目录的 JSONL 文件。

## Codex 支持

Codex 会话按 `~/.codex/sessions/YYYY/MM/DD/*.jsonl` 发现，并按 `session_meta.payload.cwd` 聚合为项目。GUI 默认统一展示 Claude / Codex 项目，同时提供来源切换器。

Codex v1 只读分析本地 JSONL，不读取 SQLite 日志库、不删除 Codex 会话。Token 来自 `event_msg.token_count.payload.info.last_token_usage`：`cached_input_tokens` 映射为缓存命中，缓存创建记为 0，`reasoning_output_tokens` 单独展示但不重复计入输出成本。任务时长优先使用 `task_complete.duration_ms`，首字延迟来自 `time_to_first_token_ms`，并把 `task_started` 计入 Task 工具事件。用户消息默认只统计数量、图片数量，打开弹窗时再按需读取正文。

Codex 深度指标来自官方 Codex 本地 transcript 与 `openai/codex` 协议源码中已持久化的 JSONL 字段：`session_meta` 提供 CLI 版本、来源、模型供应商、git 分支/提交；`turn_context` 提供模型、审批策略、沙箱策略、协作模式、推理强度和上下文窗口；`token_count.rate_limits` 提供限流快照；`response_item` / `event_msg` 提供 Web 搜索、上下文压缩、补丁应用、工具失败、任务完成/中断等事件。所有字段均为可空提取：旧 Codex 记录或 Claude 会话缺失时显示 `-`。

LOC 只从可解析 `apply_patch` 调用计算；普通 shell 命令不推断文件改动，因此无可靠 LOC 的 Codex 会话会将 TCER / CPE / CTEI 显示为 `-`。

## 关键设计决策

### LOC 的 git-free 设计

净增 LOC 来自会话自身文件改写工具调用，不依赖 git。历史方案曾用 `git log --numstat`，已废弃：git 净增只反映最终提交、受提交习惯影响、且时间窗归因不可靠。

### 按 message.id 去重

一次 assistant API 响应常被拆成多行写入 JSONL（thinking / text / 每个 tool_use 各一行），每行重复携带 usage。必须按 message.id 只计一次，否则 token 成倍虚高（实测全局 55.9% 重复计数）。tool_use 块各自唯一，LOC 统计不受影响。

### 逐模型计价

TokenUsage.per_model 按 message.model 分桶，merge 自动合并。cost_usd 对每个分桶用各自价表算成本再相加，混用多模型的会话也精确。

## 测试覆盖

测试覆盖 reader / codex_reader / paths / metrics / pricing / loc / export / baselines / metric_defs / calibrate。新增 Codex fixture 覆盖 cwd 分组、标题读取、token 去重、缓存映射、工具失败、apply_patch LOC、运行环境、上下文窗口、首字延迟、限流、Web 搜索、图片输入和补丁成功率。
