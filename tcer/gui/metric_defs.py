"""Single source of truth for the five-layer metric panel.

Defines every metric the GUI shows — its Chinese display name, unit, plain-text
explanation, semantic color level, and how to extract+format its value from a
``SessionReport``. Both the metric grid (``views.MetricPanel``) and the glossary
popup render from ``LAYERS``, so adding or renaming a metric is a one-line change
here. No Tkinter dependency.

Code keys stay abbreviated (``chr`` / ``ctei`` / ``ncpi`` …); only the ``name``
shown to users is full Chinese (TCER is the sole English abbreviation kept).
"""
from __future__ import annotations

from dataclasses import dataclass

from tcer.core import format as fmt
from tcer.core import metrics as _metrics
from tcer.core.models import SessionReport


@dataclass(frozen=True)
class Metric:
    key: str      # attribute lookup key into report_values()
    name: str     # full-Chinese display name (TCER excepted)
    unit: str     # Chinese unit, "" if none
    tip: str      # plain-Chinese explanation
    level: str    # basic / advanced / compound / ultimate → theme.LEVEL_COLORS


@dataclass(frozen=True)
class Layer:
    id: str       # L0..L5
    name: str     # 数据层 / 原始层 / …
    desc: str     # short subtitle
    metrics: list[Metric]


LAYERS: list[Layer] = [
    Layer("L0", "数据层", "原始统计", [
        Metric("subagent", "子代理", "",
               "并入该会话的子代理（subagent）数量。Claude Code 的子代理是模型为完成复杂任务自动拆出的并行助手，"
               "它们的 Token 和代码行会合并计入父会话，不单独计为一个会话。", "basic"),
        Metric("turns", "助手回合", "",
               "Claude 助手回复的条数。每条回复可能包含一次或多次工具调用（如同时读 3 个文件）。"
               "回合数反映你与 AI 的交互轮次。", "basic"),
        Metric("started", "开始时间", "", "会话中第一条助手回复的时间戳。", "basic"),
        Metric("last_time", "最后时间", "", "会话中最后一条助手回复的时间戳。配合「开始时间」可判断会话活跃时段。", "basic"),
        Metric("duration", "持续时长", "小时",
               "首条到末条助手回复的时间差。注意：这包含用户暂停阅读的时间，不是 AI 纯计算时间。"
               "一个持续 2 小时的会话，AI 可能只活跃了其中 30 分钟。", "basic"),
        Metric("models", "模型", "", "该会话使用的 AI 模型（友好名）。同一会话可能混用多个模型。", "basic"),
        Metric("skipped", "跳过回合", "",
               "usage 全为 0 的轻量回复数（如纯思考/thinking 不产生实际输出）。"
               "这些回合的 Token 消耗无法统计，已从效率计算中排除以避免拉低均值。", "basic"),
        Metric("tools", "工具调用", "",
               "Claude Code 调用的工具及次数（如 Read/Write/Edit/Grep/Glob 等）。"
               "点击查看详细列表。工具调用模式反映 AI 的工作方式。", "basic"),
        Metric("latency", "平均延迟", "秒",
               "每回合平均耗时（首尾回复时间差 ÷ 回合数）。注意：包含用户暂停阅读的时间，"
               "因此只能做粗略参考，不代表 AI 真实响应速度。", "basic"),
    ]),
    Layer("L1", "原始层", "Token 用量明细", [
        Metric("total_tokens", "总消耗", "百万",
               "总 Token 消耗 = 输入 + 输出 + 缓存。Token 是 AI 处理文本的基本计量单位，"
               "约 1 个英文单词 ≈ 1.3 Token，1 个中文字 ≈ 2 Token。", "basic"),
        Metric("input", "输入", "千", "非缓存的输入 Token。每次新对话轮次都需要发送上下文，"
               "这部分是「首次见到」的内容，按全价计费（$3/百万 Token）。", "basic"),
        Metric("output", "输出", "千", "AI 生成的输出 Token。包含代码、文字、思考过程等。"
               "输出是 AI 真正「工作」的部分，价格最高（$15/百万 Token）。", "basic"),
        Metric("cache_write", "缓存创建", "千",
               "首次写入缓存的 Token 数。Claude 会把输入上下文缓存起来，下次复用时更便宜。"
               "写入缓存的单价是 $3.75/百万 Token，比普通输入稍贵。", "basic"),
        Metric("cache_read", "缓存命中", "千",
               "从缓存读取的 Token 数。缓存读取单价仅 $0.30/百万 Token，是普通输入的 1/10。"
               "缓存命中越多越省钱。", "basic"),
    ]),
    Layer("L2", "效率层", "Token 转化效率", [
        Metric("tcer", "TCER", "行/百万",
               "核心效率指标：净增代码行 ÷ 百万 Token。每花 100 万 Token 能产出多少行净代码。"
               "越高说明 Token 利用率越好。框架参考中位数 76.6 行/百万。"
               "新功能开发通常 >50，调试/重构偏低属正常。", "ultimate"),
        Metric("chr", "缓存命中率", "",
               "缓存读取 ÷ 总输入。缓存读取单价（$0.30/百万）仅为普通输入（$3.00）的 1/10，"
               "因此缓存命中率越高，实际花费越少。85% 以上优秀，70-85% 良好。"
               "缓存效率取决于提示词稳定性——重复使用相似上下文时缓存更有效。", "advanced"),
        Metric("io_ratio", "输入输出比", "",
               "总输入 ÷ 输出 Token。反映上下文密集程度：代码审查、重构等需要大量阅读的任务，"
               "输入远多于输出，比值高（>200），是结构现象而非低效。新功能开发比值通常较低（50-150）。"
               "比值过高（>300）可能说明 AI 上下文过于庞大。", "advanced"),
        Metric("caf", "缓存调整因子", "",
               "总输入 ÷（普通输入 + 缓存写入）。≥1，越大说明缓存复用越多。"
               "用于消除缓存对效率比较的干扰——两个相同 TCER 的会话，CAF 更高的那个实际花费更少，"
               "真实效率更好。CAF=1 表示完全没有缓存复用。", "advanced"),
        Metric("cache_efficiency", "缓存效率", "倍",
               "缓存读取 ÷ 缓存写入。>1 表示缓存「回本」——读出的内容比写入的多，说明同一段上下文被反复利用。"
               "值越高说明上下文复用越好，意味着你写的提示词和工作上下文越稳定。"
               "2 倍以上通常说明工作流比较成熟。", "advanced"),
        Metric("cache_write_ratio", "缓存写入占比", "",
               "缓存写入 ÷ 总输入。反映「首次见到」的上下文比例。占比越低说明越多的输入是从缓存读取的（更便宜）。"
               "如果持续偏高，可以考虑减少上下文切换、保持提示词一致性。", "advanced"),
        Metric("non_cached_input_ratio", "非缓存输入占比", "",
               "普通输入（非缓存）÷ 总输入。与缓存写入占比类似，但更直观：越低 = 越多的输入来自缓存 = 越省钱。"
               "注意：首次会话或大量新上下文注入时，这个值会自然偏高。", "advanced"),
    ]),
    Layer("L3", "质量层", "代码产出与工具使用质量", [
        Metric("net_loc", "净增行", "行",
               "写入 − 删除 = 净增代码行。正值表示代码量增长，负值表示代码减少（重构/删除冗余）。"
               "来源是会话内 Write/Edit/MultiEdit 工具调用的逐条统计，不依赖 git。", "basic"),
        Metric("added", "写入行", "行", "工具调用写入的总代码行数（含重写/覆盖）。", "basic"),
        Metric("deleted", "删除行", "行", "工具调用删除的总代码行数。", "basic"),
        Metric("churn", "返工率", "",
               "删除行 ÷ 写入行。越低越好——0% 表示「一次写对」，没有返工。"
               "15% 以上说明反复修改较多，可能需要改进提示词或拆分任务。"
               "注意：「写入→删除→重写同一段」算 2 次写入 + 1 次删除，返工率会高于直觉。", "basic"),
        Metric("high_churn", "高频改动文件", "个",
               "被改动 ≥3 次的文件数。这些文件是反复修改的「热点」，可能存在需求不清、设计问题、"
               "或需要拆分重构。点击查看具体文件名和改动次数。", "basic"),
        Metric("test_loc", "测试代码", "行",
               "测试文件（*test*.py、*/tests/ 等）的净增行。反映对测试的投入。"
               "测试代码占比越高，说明项目质量意识越强。", "basic"),
        Metric("doc_loc", "文档代码", "行",
               "文档文件（*.md、*/docs/ 等）的净增行。反映对文档的投入。", "basic"),
        Metric("read_write_ratio", "读写比", "",
               "Read 工具调用 ÷（Write + Edit）。反映「先读后改」的习惯：≥3 说明 AI 做了充分的代码阅读和理解"
               "后再动手修改（健康模式）；<1 说明改多读少，容易引入 bug。"
               "代码审查类任务比值自然偏高。", "advanced"),
        Metric("edit_ratio", "编辑占比", "",
               "Edit 调用 ÷（Edit + Write）。越高说明越偏增量修改（在已有代码基础上精确改动），"
               "而非整文件重写。>70% 通常表示良好的增量开发模式。"
               "新文件创建多时，此值自然偏低。", "advanced"),
        Metric("exploration_ratio", "探索占比", "",
               "（Grep + Glob）÷ 总工具调用。反映 AI 在代码库中的「搜索探索」比例。"
               "偏高说明 AI 花大量时间在寻找代码位置——可能表示代码库复杂或缺乏导航线索。"
               "适度探索（20-40%）是健康的。", "advanced"),
        Metric("subagent_density", "子代理密度", "",
               "子代理数 ÷ 助手回合。>0 说明 AI 使用了并行子代理来加速工作。"
               "值越高说明任务越复杂、需要拆分执行。0.1-0.3 是正常使用范围。"
               "过高（>1）可能说明任务过于复杂，需要考虑拆分。", "advanced"),
    ]),
    Layer("L4", "经济层", "成本分析", [
        Metric("cost", "总成本", "美元",
               "按各模型官方标价（list price）分别估算并求和的总花费。"
               "这不是订阅实际扣费，而是按 API 定价的理论成本。"
               "不同模型价格差异大：Claude Opus 输入 $3/百万、输出 $15/百万。", "basic"),
        Metric("cost_per_mt", "每百万Token成本", "$/百万",
               "总成本 ÷ 总 Token（百万）。反映每百万 Token 的平均实付成本。"
               "受缓存命中率影响大——CHR=90% 时，每百万 Token 成本可能只有 CHR=0% 时的 1/3。"
               "典型范围 $0.5-5/百万（取决于 CHR 和输出占比）。", "advanced"),
        Metric("cpe", "千行代码成本", "$/千行",
               "总成本 ÷ 净增行 × 1000。每写 1000 行净代码花了多少美元。"
               "可跨项目、跨模型对比。<$10 优秀，$10-30 良好，>$30 需改进。"
               "调试任务 CPE 偏高属正常（代码产出少但 Token 消耗多）。", "advanced"),
    ]),
    Layer("L5", "综合层", "最终评分", [
        Metric("ctei", "综合效率指数", "",
               "综合评分：把效率（TCER）、产出密度（NCPI）、成本效率（CPE）、缓存利用（CHR）合成一个数字。"
               ">2 优秀 · 1~2 良好 · 0.5~1 中等 · 0.1~0.5 低效 · <0.1 极端低效。"
               "基准值见下方 TCER/NCPI/CPE 基准，修改 composite_baselines.json 可用个人数据替换。", "compound"),
        Metric("grade", "评级", "",
               "综合效率指数对应的等级：优秀/良好/中等/低效/极端低效，颜色与「综合效率指数排名」标签页的条形图一致。", "basic"),
        Metric("task_type", "任务类型", "",
               "你选择的任务类型（如新功能/调试/重构/代码审查/测试等）。"
               "不同任务天然产出代码量不同：调试 40%、重构 50%、审查 20%。"
               "选对类型才能让「任务调整效率」给出公平的跨任务比较。", "compound"),
        Metric("ta_tcer", "任务调整效率", "行/百万",
               "TCER ÷ 任务类型系数。让不同类型的任务能在同一刻度下比较。"
               "例如：调试任务产出 30 行/百万看似低效，但除以调试系数 0.4 后 = 75，"
               "说明在这个任务类型下已经做得不错了。", "compound"),
        Metric("ncpi", "净产出指数", "",
               "净增行 ÷ 代码库总行数。衡量你对整个代码库的「贡献密度」。"
               "新项目中可能高达 10%+，成熟项目中 1-2% 已属显著改动。"
               "项目越大，每次改动占比越低——这是自然趋势，不是低效。", "advanced"),
        Metric("psac", "阶段调整系数", "",
               "用来抵消大型代码库的结构性效率下降。原理：代码库越大，每次改动需要注入的上下文越多"
               "（理解现有代码、保持一致性），TCER 自然下降——就像「维护税」。"
               "PSAC 把这种结构性因素剔除，让你比较的效率更纯粹。>1 表示代码库还在早期阶段。", "compound"),
        Metric("tcer_phase_adj", "阶段调整后效率", "行/百万",
               "TCER × 阶段调整系数。剔除代码库规模影响后的效率值。"
               "如果你维护一个 10 万行的项目和一个 1000 行的项目，用这个指标比较才公平。", "compound"),
        Metric("bl_tcer", "TCER 基准", "行/百万",
               "综合效率指数计算中使用的 TCER 基准值（框架默认 76.59）。"
               "综合效率指数 =（TCER÷此基准）×（NCPI÷NCPI基准）×（CPE基准÷CPE）× 缓存因子。"
               "修改 data/composite_baselines.json 可替换为个人基准。", "basic"),
        Metric("bl_ncpi", "NCPI 基准", "",
               "综合效率指数计算中使用的 NCPI 基准值（框架默认 0.101）。", "basic"),
        Metric("bl_cpe", "CPE 基准", "$/千行",
               "综合效率指数计算中使用的 CPE 基准值（框架默认 8.22）。", "basic"),
    ]),
]


# Non-numeric concept notes appended to the glossary popup.
# (name, explanation, level)
CONCEPT_NOTES: list[tuple[str, str, str]] = [
    ("LOC 来源",
     "本工具不依赖 git：净增代码来自会话里 Write/Edit/MultiEdit 工具调用的逐条统计，"
     "按会话精确归因；代码库累计行来自扫描工作目录。不安装任何包、不改 PATH。", "basic"),
    ("LOC 统计假设 ⚠️",
     "【重要】Write 工具调用假设写入的是新文件（原大小 = 0）。若 Write 覆盖已有文件，"
     "added 会高估、deleted 会遗漏。Edit 不受影响（只看增量）。「高频改动文件」计数是潜在高估的上界。"
     "若需精确量化偏差，用「校准 LOC」功能对标 git 历史。", "basic"),
    ("如何提高效率",
     "想提升 TCER/CTEI？几个实用建议：①保持提示词稳定（提高缓存命中率）；"
     "②用 Edit 而非 Write 修改已有文件（更精确，返工率低）；"
     "③让 AI 先 Grep/Glob 搜索再动手（提高读写比）；"
     "④选对任务类型（调试的 TCER 天然低于新功能，这很正常）。", "basic"),
]


def _duration_hours(report: SessionReport) -> str:
    u = report.usage
    if u.started_at and u.ended_at:
        return f"{(u.ended_at - u.started_at) / 1000 / 3600:.1f}"
    return "-"


def _tools_summary(report: SessionReport) -> str:
    tc = report.usage.tool_calls
    if not tc:
        return "-"
    return f"{sum(tc.values())} 次（{len(tc)} 种）"


def report_values(report: SessionReport) -> dict[str, str]:
    """Format every metric key for one SessionReport (works for aggregate or single).

    The single place that maps metric ``key`` → display string, so the grid just
    looks up ``values[key]``. Keys without a value fall back to ``"-"`` upstream.
    """
    u = report.usage
    return {
        # L0 数据层
        "subagent": str(report.subagent_count or 0),
        "turns": fmt.fmt_int(u.assistant_msgs),
        "started": fmt.fmt_dt(u.started_at),
        "last_time": fmt.fmt_dt(u.ended_at),
        "duration": _duration_hours(report),
        "models": fmt.models_label(u) if u.models else "-",
        "skipped": fmt.fmt_int(u.empty_usage_skipped),
        "tools": _tools_summary(report),
        "latency": fmt.fmt_float(report.avg_turn_latency_sec, "0.0"),
        # L1 原始层
        "total_tokens": f"{u.total / 1e6:.2f}",
        "input": f"{u.input_tokens / 1e3:.1f}",
        "output": f"{u.output_tokens / 1e3:.1f}",
        "cache_write": f"{u.cache_creation_input_tokens / 1e3:.1f}",
        "cache_read": f"{u.cache_read_input_tokens / 1e3:.1f}",
        # L2 效率层
        "tcer": fmt.fmt_float(report.tcer, "0.0"),
        "chr": fmt.fmt_pct(report.chr),
        "io_ratio": fmt.fmt_float(report.io_ratio, "0.1"),
        "caf": fmt.fmt_float(report.caf, "0.00"),
        "cache_efficiency": fmt.fmt_float(report.cache_efficiency, "0.00"),
        "cache_write_ratio": fmt.fmt_pct(report.cache_write_ratio),
        "non_cached_input_ratio": fmt.fmt_pct(report.non_cached_input_ratio),
        # L3 质量层
        "net_loc": fmt.fmt_int(report.net_loc),
        "added": fmt.fmt_int(report.code_added),
        "deleted": fmt.fmt_int(report.code_deleted),
        "churn": fmt.fmt_pct(report.churn_ratio),
        "high_churn": str(report.high_churn_file_count),
        "test_loc": fmt.fmt_int(report.test_net_loc),
        "doc_loc": fmt.fmt_int(report.doc_net_loc),
        "read_write_ratio": fmt.fmt_pct(report.read_write_ratio),
        "edit_ratio": fmt.fmt_pct(report.edit_ratio),
        "exploration_ratio": fmt.fmt_pct(report.exploration_ratio),
        "subagent_density": fmt.fmt_float(report.subagent_density, "0.00"),
        # L4 经济层
        "cost": fmt.fmt_money(report.cost),
        "cost_per_mt": f"{report.cost_per_mt:.2f}" if report.cost_per_mt is not None else "-",
        "cpe": fmt.fmt_money(report.cpe),
        # L5 综合层
        "ctei": fmt.fmt_float(report.ctei, "0.00"),
        "grade": report.grade or "-",
        "task_type": report.task_type or "-",
        "ta_tcer": fmt.fmt_float(report.ta_tcer, "0.00"),
        "ncpi": fmt.fmt_float(report.ncpi, "0.000"),
        "psac": fmt.fmt_float(report.psac, "0.000"),
        "tcer_phase_adj": fmt.fmt_float(report.tcer_phase_adj, "0.00"),
        # Current CTEI baselines (read-only reference from config)
        "bl_tcer": fmt.fmt_float(_metrics.TCER_BASELINE, "0.00"),
        "bl_ncpi": fmt.fmt_float(_metrics.NCPI_BASELINE, "0.000"),
        "bl_cpe": fmt.fmt_float(_metrics.CPE_BASELINE, "0.00"),
    }


# All keys referenced by LAYERS — used by tests to guard against drift
# between the definitions and ``report_values``.
ALL_KEYS = {m.key for layer in LAYERS for m in layer.metrics}
