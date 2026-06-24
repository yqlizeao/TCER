"""Single source of truth for the six-group metric panel.

Defines every metric the GUI shows — its Chinese display name, unit, plain-text
explanation, semantic color level, and how to extract+format its value from a
``SessionReport``. Both the metric grid (``views.MetricPanel``) and the glossary
popup render from ``GROUPS``, so adding or renaming a metric is a one-line change
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
    level: str    # basic / compound → theme.LEVEL_COLORS
    sentiment: str = ""  # "up"=越高越好, "down"=越低越好, ""=中性
    fmt: str = ""        # format spec token (filled from _SESSION_FMT / _MODEL_FMT at
                         # import); drives format_value. "" / "text" = pass-through string.


@dataclass(frozen=True)
class Subgroup:
    name: str             # 子类名（基础 / 行为 / 质量）
    metrics: list[Metric]


@dataclass(frozen=True)
class Group:
    id: str       # G1..G6
    name: str     # 会话概况 / Token 用量 / …
    metrics: list[Metric]              # 扁平列表（所有消费方都以此为准）
    subgroups: list[Subgroup] | None = None  # 可选子类划分，仅指标面板用于分组渲染


def _subgrouped(gid: str, name: str, subgroups: list[Subgroup]) -> Group:
    """Build a Group whose ``metrics`` is the flattened concat of its subgroups,
    so flat consumers (ALL_KEYS / 雷达图 / 趋势图) stay unchanged while the panel
    can render the subdivision."""
    flat = [m for sg in subgroups for m in sg.metrics]
    return Group(gid, name, flat, subgroups)


GROUPS: list[Group] = [
    Group("G1", "会话概况", [
        Metric("subagent", "子代理", "",
               "并入该会话的子代理数量。子代理是 Claude Code 为复杂任务自动拆出的并行助手，"
               "其 Token 与代码行合并计入父会话。", "basic"),
        Metric("turns", "助手回合", "",
               "Claude 助手回复的总条数（仅计有真实 token 消耗的回合）；"
               "括号内为跳过的零 usage 回合（thinking stub 等）。", "basic"),
        Metric("started", "开始时间", "", "会话第一条助手回复的时间戳。", "basic"),
        Metric("last_time", "结束时间", "", "会话最后一条助手回复的时间戳，配合「开始时间」可判断活跃时段。", "basic"),
        Metric("duration", "持续时长", "",
               "首条到末条助手回复的时间差，含用户阅读暂停，非 AI 纯计算时间。", "basic"),
        Metric("models", "模型", "", "该会话使用的 AI 模型（友好名），同一会话可能混用多个。", "basic"),
        Metric("tools", "工具调用", "",
               "Claude Code 调用的工具及次数，点击查看详细列表。", "basic"),
        Metric("latency", "平均延迟", "秒",
               "公式：首尾时间差 ÷ 回合数\n"
               "推荐：越低越好（仅供粗略参考）\n"
               "说明：每回合平均耗时，含用户暂停，不代表 AI 真实响应速度。", "basic", "down"),
        Metric("user_msgs", "用户消息", "",
               "你主动发送的消息条数，点击查看全部；与「助手回合」对比可看交互密度。", "basic"),
        Metric("entrypoint", "启动方式", "",
               "会话启动入口：claude-vscode（VS Code 扩展）、claude-cli（命令行）等。", "basic"),
        Metric("memory_files", "项目记忆文件", "个",
               "当前项目 memory/ 目录下的文件数量（项目级指标，仅在项目汇总视图显示计数，"
               "会话视图为 -）。点击查看文件列表并可跳转到目录。", "basic"),
    ]),
    Group("G2", "Token 用量", [
        Metric("total_tokens", "总 Token", "",
               "公式：输入 + 输出 + 缓存\n"
               "推荐：视任务而定\n"
               "说明：约 1 英文词≈1.3 Token、1 中文字≈2 Token。", "basic"),
        Metric("input", "输入", "", "非缓存的输入 Token，按全价计费。单价随模型而定（Anthropic 通用回退价约 $3/百万，mimo/glm 等更低），实际逐模型计价。", "basic"),
        Metric("output", "输出", "", "AI 生成的输出 Token（代码、文字、思考），单价最高。随模型而定（Anthropic 回退价约 $15/百万），实际逐模型计价。", "basic"),
        Metric("cache_write", "缓存创建", "",
               "首次写入缓存的 Token 数。单价随模型而定（Anthropic 回退价约 $3.75/百万，部分路由模型为 0）。", "basic"),
        Metric("cache_read", "缓存命中", "",
               "从缓存读取的 Token 数，单价远低于普通输入（Anthropic 回退价约 $0.30/百万，为输入的 1/10），命中越多越省。", "basic"),
    ]),
    Group("G3", "缓存效率", [
        Metric("chr", "缓存命中率", "",
               "AI 读进来的上下文里，有多少是从缓存直接拿的（便宜），而不是重新付全价。\n"
               "怎么看：≥85% 优秀 · 70–85% 良好，越高越省钱（缓存读只要全价的 1/10）。\n"
               "💡 想提高：别频繁改提示词/系统设定，保持上下文稳定。", "basic", "up"),
        Metric("io_ratio", "输入输出比", "",
               "AI 每写出 1 个字（Token），背后读进了多少字的上下文。\n"
               "怎么看：没有绝对好坏。对话越长、缓存越多这个值越大，更多反映「上下文有多厚」而非效率；>300 可能上下文过于庞大。\n"
               "🔢 总输入 ÷ 输出", "basic"),
        Metric("caf", "缓存复用因子", "",
               "输入里有多大比例是「反复复用的缓存」，而不是每次都新读一遍。\n"
               "怎么看：=1 表示完全没复用，越大越好。\n"
               "💡 偏技术的修正项，主要用于公平比较效率，日常可不深究。", "basic", "up"),
        Metric("cache_efficiency", "缓存读写比", "",
               "从缓存「读出」的量 ÷ 写入缓存的量。>1 就说明缓存回本了——同一份上下文被重复利用。\n"
               "怎么看：>1 回本，≥2 说明工作流成熟、上下文很稳定。", "basic", "up"),
        Metric("cache_write_ratio", "缓存写入占比", "",
               "这次输入里，有多少是「第一次见、需要新建缓存」的部分。\n"
               "怎么看：越低越好（说明大多命中了已有缓存）；首次会话或一次性灌入大量新内容时偏高很正常。", "basic", "down"),
        Metric("non_cached_input_ratio", "非缓存输入占比", "",
               "输入里完全没走缓存、按全价付费的比例。\n"
               "怎么看：越低越好（越多来自缓存＝越省）；首次会话天然偏高。", "basic", "down"),
    ]),
    _subgrouped("G4", "代码产出与质量", [
        Subgroup("基础", [
            Metric("net_loc", "净增行", "",
                   "公式：写入行 − 删除行\n"
                   "推荐：视任务而定（正值=增长，负值=重构/精简）\n"
                   "说明：来自会话内 Write/Edit/MultiEdit 逐条统计，不依赖 git。", "basic"),
            Metric("added", "写入行", "", "工具调用写入的总代码行数（含重写/覆盖）。", "basic"),
            Metric("deleted", "删除行", "", "工具调用删除的总代码行数。", "basic"),
            Metric("files_touched", "涉及文件", "",
                   "会话中读取、写入或编辑过的独立文件数，点击查看列表。", "basic"),
            Metric("test_loc", "测试行", "",
                   "测试文件（*test*.py、*/tests/ 等）的净增行，反映测试投入。", "basic"),
            Metric("doc_loc", "文档行", "",
                   "文档文件（*.md、*/docs/ 等）的净增行，反映文档投入。", "basic"),
        ]),
        Subgroup("行为", [
            Metric("read_write_ratio", "读写比", "",
                   "公式：Read ÷（Write + Edit）\n"
                   "推荐：Read 为改动数的 3 倍以上较健康（仅供参考）\n"
                   "说明：反映「先读后改」习惯。⚠️ 仅统计 Read 工具，经 Bash 的 cat/head 阅读不计入；故创作/调试类(多用 Bash 看文件)会偏低，不必据此判定「盲写」。", "basic", "up"),
            Metric("edit_ratio", "编辑占比", "",
                   "公式：Edit ÷（Edit + Write）\n"
                   "推荐：>70%\n"
                   "说明：越高越偏增量修改而非整文件重写；新建文件多时自然偏低。", "basic", "up"),
            Metric("exploration_ratio", "探索占比", "",
                   "公式：（Grep + Glob）÷ 总工具调用\n"
                   "推荐：视任务而定（仅供参考）\n"
                   "说明：⚠️ 分子只含 Grep/Glob，但 Claude Code 大量探索走 Bash（rg/find/cat）与子代理,均不计入；分母含 Bash/TodoWrite 等，故实测普遍低于直觉。仅作粗略趋势参考。", "basic"),
            Metric("search_edit_ratio", "搜索后编辑比", "",
                   "公式：搜索（Grep/Glob）后 3 回合内发生 Edit/Write 的占比\n"
                   "推荐：越高越好\n"
                   "说明：衡量「搜完是否跟进修改」的工作流;按回合就近匹配(不绑定具体文件，因 Grep 的 path 多为目录)；偏低=搜索后未动手(探索过度)。", "basic", "up"),
            Metric("thinking_count", "思考次数", "",
                   "AI 输出 thinking（推理）内容块的次数,消耗 output token。⚠️ 对推理类模型(mimo/glm/带思考的 Claude)几乎每回合都有,≈回合数,并非「复杂度」信号;关闭思考的会话则恒为 0。", "basic"),
        ]),
        Subgroup("质量", [
            Metric("churn", "返工率", "",
                   "公式：自返工删除行 ÷ 写入行\n"
                   "推荐：≤15%，越低越好\n"
                   "说明：只计「本会话先写入、随后又被自己删除/替换」的行(真正的返工);删除会话之外的既有代码算正常编辑，不计入。0% 即一次写对。", "basic", "down"),
            Metric("read_before_write", "先读后写率", "",
                   "公式：写入/编辑前曾被 Read 的文件占比\n"
                   "推荐：越高越好（仅供参考）\n"
                   "说明：低值多见于「新建文件」(无需先读)或经 Bash cat 阅读(不计入)的情形;并非都等于危险盲写,需结合任务类型看。", "basic", "up"),
            Metric("tool_error_rate", "工具错误率", "",
                   "公式：工具出错次数 ÷ 总工具调用\n"
                   "推荐：越低越好\n"
                   "说明：反映操作可靠性，偏高常因文件不存在、命令失败、Edit 匹配不到；审查/探索类自然略高。", "basic", "down"),
            Metric("high_churn_files", "高返工文件", "",
                   "公式：被编辑 ≥3 次的文件数\n"
                   "推荐：越少越好\n"
                   "说明：同一文件反复修改多为一次没改对，比整体返工率更聚焦的质量信号。", "basic", "down"),
            Metric("unseen_writes", "盲写文件", "次",
                   "公式：Write 写入「此前未读取/未接触」文件的次数\n"
                   "推荐：越低越可靠\n"
                   "说明：既反映「盲写」习惯，也是净增行高估的上界——覆写已有文件会把旧内容计为新增。", "basic", "down"),
        ]),
    ]),
    Group("G5", "成本分析", [
        Metric("cost", "总成本", "美元",
               "公式：各模型 Token × 官方标价，求和\n"
               "推荐：视任务而定（越低越好，无绝对基准）\n"
               "说明：按 API list price 估算的理论成本，非订阅实际扣费。", "basic", "down"),
        Metric("cost_per_mt", "每百万Token成本", "美元/百万",
               "公式：总成本 ÷ 总 Token（百万）\n"
               "推荐：典型 $0.5–5/百万（视 CHR 与输出占比）\n"
               "说明：反映每百万 Token 平均实付，受缓存命中率影响大。", "basic", "down"),
        Metric("cpe", "千行代码成本", "美元/千行",
               "公式：总成本 ÷ 净增行 × 1000\n"
               "推荐：<$10 优秀 · $10–30 良好 · >$30 需改进\n"
               "说明：每千行净代码花费，可跨项目/模型对比；调试任务偏高属正常。", "basic", "down"),
    ]),
    Group("G6", "综合评分", [
        Metric("tcer", "TCER", "行/百万",
               "核心指标：每烧 100 万 Token，AI 写出了多少行净代码——衡量「划不划算」。\n"
               "怎么看：越高越省；新功能通常 >50，参考中位数约 76.6；调试/重构天然偏低，正常。\n"
               "💡 想提高：提高缓存命中率、少返工、别让 AI 反复重写整文件。", "basic", "up"),
        Metric("ctei", "综合效率分", "",
               "把 4 件事打包成一个总分：产出效率(TCER) × 对项目的贡献(贡献度) × 每行省不省钱 × 缓存用得好不好，再跟一条参考线比。\n"
               "怎么看：>2 优秀 · 1–2 良好 · 0.5–1 中等 · 0.1–0.5 低效 · <0.1 极端低效。\n"
               "⚠️ 它是 4 个比率相乘，数值可能很大(单会话偶尔上百)，别纠结绝对值——主要看排名页的相对高低和趋势。要看「这次到底干得好不好」，直接看 TCER、返工率、每千行成本更靠谱。\n"
               "⚠️ 只看单会话；「全部会话」聚合视图显示「-」。", "compound", "up"),
        Metric("grade", "评级", "",
               "上面「综合效率分」对应的等级标签：优秀/良好/中等/低效/极端低效，颜色和排名页的条形图一致。\n"
               "⚠️ 聚合视图显示「-」，请在排名页看每个会话的评级。", "basic"),
        Metric("task_type", "任务类型", "",
               "你这次主要在干啥：写新代码(创作)、改老代码(维护)、还是不写代码(调研/审查)。\n"
               "💡 选对很重要——下面的「归一化效率」靠它把不同任务拉到同一起跑线公平比。点下拉看三类区别(创作100% · 维护45% · 非编码20%)。", "compound"),
        Metric("ttaf", "任务类型系数", "",
               "不同任务天生产出的代码量不同：写新功能多、调试少。这是给当前类型的「难度折扣」(创作1.0 / 维护0.45 / 非编码0.2)。\n"
               "怎么看：系数越小，说明这类任务产出代码本就少，不代表你效率差。\n"
               "💡 它只是「归一化效率」的中间量，一般不用单独盯着看。", "basic"),
        Metric("ntcer", "归一化效率", "行/百万",
               "把 TCER 按任务难度折扣还原后的效率，让「调试」和「写新功能」能公平比较。\n"
               "怎么看：越高越好；跨任务类型比较时，看这个比看 TCER 更公平。\n"
               "例：调试 TCER=30，但调试本就难，还原后 ≈75，其实不差。", "compound", "up"),
        Metric("ncpi", "代码库贡献度", "",
               "这次写的净代码，相当于整个项目现有代码的百分之几。\n"
               "怎么看：新项目能到 10%+，成熟大项目 1–2% 就算显著；项目越大占比越低很自然。\n"
               "⚠️ 单会话指标；聚合视图显示「-」(累计写入会超过项目现有规模)。", "basic", "up"),
        Metric("psac", "阶段调整系数", "",
               "项目越大，改一点点就越难产出新行(「维护税」)。这个系数给大项目的效率打个补偿，>1 表示项目还年轻。\n"
               "💡 偏技术的修正项，日常可忽略；它只用来算下面的「阶段调整后效率」。", "compound"),
        Metric("tcer_phase_adj", "阶段调整后效率", "行/百万",
               "把项目规模的影响剔除后的 TCER，方便拿大项目和小项目公平比。\n"
               "怎么看：越高越好。\n"
               "💡 和「归一化效率」一样是公平化后的值，日常看 TCER 本身就够。", "compound", "up"),
        Metric("bl_tcer", "TCER 基准", "行/百万",
               "一条「参考线」，不是你的成绩——「综合效率分」拿你的 TCER 跟它比来打分。默认 76.59，来自框架自带的 16 个样本会话。\n"
               "💡 想改成「跟你自己的历史平均」比，可用本项目数据生成个人基准来替换。", "basic"),
        Metric("bl_ncpi", "贡献度基准", "",
               "「综合效率分」里给贡献度打分用的参考线(默认 0.101)，不是你的成绩。可用个人基准替换。", "basic"),
        Metric("bl_cpe", "成本基准", "美元/千行",
               "「综合效率分」里给每千行成本打分用的参考线(默认 8.22)，不是你的成绩。可用个人基准替换。", "basic"),
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
        hours = (u.ended_at - u.started_at) / 1000 / 3600
        if hours < 1:
            minutes = hours * 60
            return f"{minutes:.0f} 分钟"
        return f"{hours:.1f} 小时"
    return "-"


def _tools_summary(report: SessionReport) -> str:
    tc = report.usage.tool_calls
    if not tc:
        return "-"
    return f"{sum(tc.values())} 次（{len(tc)} 种）"


def _turns_display(u) -> str:
    """助手回合：真实回合数，有跳过时追加 (+N 跳过)"""
    total = u.assistant_msgs
    skipped = u.empty_usage_skipped
    if skipped:
        return f"{fmt.fmt_int(total)}（+{skipped} 跳过）"
    return fmt.fmt_int(total)


def _task_category_name(category_key: str | None) -> str | None:
    """将任务大类 key 转换为中文名称"""
    if not category_key:
        return None
    category_info = _metrics.TASK_CATEGORIES.get(category_key)
    return category_info["name"] if category_info else category_key


# ============================================================
# Metric engine — the single source of truth for extraction + formatting.
#
# Three concerns, one home:
#   • format_value(fmt, native)  — native value → display string (panel, popups)
#   • raw_value(report, key)     — numeric value in display magnitude (charts)
#   • format_plot(key, raw, m)   — chart-tooltip formatting of a raw value
# Consumers (views.py, popups.py) call these instead of re-deriving their own.
# ============================================================

# Format spec per session metric key. Drives _format_native; populated onto each
# Metric.fmt at import so consumers can also read it off the Metric object.
_SESSION_FMT: dict[str, str] = {
    # G1 — text / custom display (see _DISPLAY_EXTRACTORS), grade is plain text
    "subagent": "text", "turns": "text", "started": "text", "last_time": "text",
    "duration": "text", "models": "text", "tools": "text", "entrypoint": "text",
    "task_type": "text", "grade": "text",
    "latency": "float:0.0", "user_msgs": "int",
    # G2
    "total_tokens": "int", "input": "int", "output": "int",
    "cache_write": "int", "cache_read": "int",
    # G3
    "chr": "pct", "io_ratio": "float:0.1", "caf": "float:0.00",
    "cache_efficiency": "float:0.00", "cache_write_ratio": "pct",
    "non_cached_input_ratio": "pct",
    # G4
    "net_loc": "int", "added": "int", "deleted": "int", "churn": "pct",
    "test_loc": "int", "doc_loc": "int", "read_write_ratio": "float:0.0",
    "edit_ratio": "pct", "exploration_ratio": "pct", "thinking_count": "int",
    "files_touched": "int", "search_edit_ratio": "pct", "read_before_write": "pct",
    "tool_error_rate": "pct", "high_churn_files": "int", "unseen_writes": "int",
    "memory_files": "int",
    # G5
    "cost": "money", "cost_per_mt": "money2", "cpe": "money",
    # G6
    "tcer": "float:0.0", "ctei": "float:0.000", "ttaf": "float:0.00",
    "ntcer": "float:0.00", "ncpi": "float:0.000", "psac": "float:0.000",
    "tcer_phase_adj": "float:0.00",
    "bl_tcer": "float:0.00", "bl_ncpi": "float:0.000", "bl_cpe": "float:0.00",
}

# key → attribute name on SessionReport when they differ from the metric key.
_REPORT_ATTR = {
    "churn": "churn_ratio", "added": "code_added", "deleted": "code_deleted",
    "test_loc": "test_net_loc", "doc_loc": "doc_net_loc",
    "high_churn_files": "high_churn_file_count", "latency": "avg_turn_latency_sec",
}
# key → attribute name on report.usage (token counters live there).
_USAGE_ATTR = {
    "total_tokens": "total", "input": "input_tokens", "output": "output_tokens",
    "cache_write": "cache_creation_input_tokens", "cache_read": "cache_read_input_tokens",
    "user_msgs": "user_msgs", "thinking_count": "thinking_count",
}
# key → callable returning the current baseline constant (read-only reference).
_BASELINE = {
    "bl_tcer": lambda: _metrics.TCER_BASELINE,
    "bl_ncpi": lambda: _metrics.NCPI_BASELINE,
    "bl_cpe": lambda: _metrics.CPE_BASELINE,
}


def _format_native(fmt_spec: str, v) -> str:
    """Format a *native* value (chr=0.959, cost in USD, …) per a fmt spec token.

    Mirrors the per-key formatting the GUI has always used, so output is byte-for-byte
    identical to the old hand-written ``report_values``.
    """
    if fmt_spec == "int":
        return fmt.fmt_int(v)
    if fmt_spec == "pct":
        return fmt.fmt_pct(v)
    if fmt_spec.startswith("float:"):
        return fmt.fmt_float(v, fmt_spec.split(":", 1)[1])
    if fmt_spec == "money":
        return fmt.fmt_money(v)
    if fmt_spec == "money2":
        return f"${v:.2f}" if v is not None else "-"
    # "text" / "" — already a string (or None)
    if v is None:
        return "-"
    return v if isinstance(v, str) else str(v)


def format_value(key: str, native) -> str:
    """Native value → display string, using the metric's declared fmt."""
    m = METRIC_BY_KEY.get(key)
    return _format_native(m.fmt if m else "", native)


# Metrics whose display string is genuinely custom (not just fmt(native)).
_DISPLAY_EXTRACTORS = {
    "subagent": lambda r: str(r.subagent_count or 0),
    "turns": lambda r: _turns_display(r.usage),
    "started": lambda r: fmt.fmt_dt(r.usage.started_at),
    "last_time": lambda r: fmt.fmt_dt(r.usage.ended_at),
    "duration": _duration_hours,
    "models": lambda r: fmt.models_label(r.usage) if r.usage.models else "-",
    "tools": _tools_summary,
    "entrypoint": lambda r: r.meta.entrypoint or "-",
    "task_type": lambda r: _task_category_name(r.task_type) or "-",
    "memory_files": lambda r: str(len(r.memory_files)) if r.memory_files is not None else "-",
}


def _native(report: SessionReport, key: str):
    """The underlying value for *key* (chr=0.959, cost USD, grade str, …)."""
    if key in _USAGE_ATTR:
        return getattr(report.usage, _USAGE_ATTR[key], None)
    if key in _BASELINE:
        return _BASELINE[key]()
    return getattr(report, _REPORT_ATTR.get(key, key), None)


def display(report: SessionReport, key: str) -> str:
    """The display string for one metric of one SessionReport (session/aggregate)."""
    ext = _DISPLAY_EXTRACTORS.get(key)
    if ext is not None:
        return ext(report)
    return _format_native(_SESSION_FMT.get(key, ""), _native(report, key))


def report_values(report: SessionReport) -> dict[str, str]:
    """Format every metric key for one SessionReport (works for aggregate or single).

    The single place that maps metric ``key`` → display string, so the grid just
    looks up ``values[key]``. Now data-driven from the SSOT (``_SESSION_FMT`` +
    ``_DISPLAY_EXTRACTORS``) — output is identical to the former hand-written map.
    """
    return {key: display(report, key) for key in ALL_KEYS}


def raw_value(report, key: str) -> float | None:
    """Extract the raw numeric value for *key* for charts.

    Returns None when the metric is unavailable or not numeric. Only ``chr`` is
    scaled to 0–100 (matching the GUI's long-standing behaviour); every other
    metric is returned in its native magnitude. The single source for trend /
    scatter / dashboard / radar value extraction.
    """
    u = report.usage
    # key → attribute name on SessionReport, for the numeric/chart path. Note this
    # is intentionally NARROWER than _REPORT_ATTR (no high_churn_files / latency):
    # it reproduces the former views.metric_raw_value exactly.
    _RAW_ATTR = {
        "churn": "churn_ratio", "added": "code_added", "deleted": "code_deleted",
        "test_loc": "test_net_loc", "doc_loc": "doc_net_loc",
    }
    try:
        if key == "chr":
            return report.chr * 100.0 if report.chr is not None else None
        if key == "duration":
            if u.started_at and u.ended_at:
                return (u.ended_at - u.started_at) / 3600_000
            return None
        if key == "latency":
            return report.avg_turn_latency_sec
        if key == "tools":
            return float(sum(u.tool_calls.values())) if u.tool_calls else None
        if key in _USAGE_ATTR:
            return float(getattr(u, _USAGE_ATTR[key]))
        if key == "turns":
            return float(u.assistant_msgs)
        if key == "subagent":
            return float(report.subagent_count)
        attr = _RAW_ATTR.get(key, key)
        v = getattr(report, attr, None)
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def format_plot(key: str, raw: float, m: "Metric | None") -> str:
    """Format a *display-magnitude* raw value for a chart tooltip (lightweight)."""
    if key == "chr":
        return f"{raw:.1f}%"
    if key in ("cost", "cpe", "cost_per_mt"):
        return f"${raw:.4f}"
    if m and m.unit in ("行", "个"):
        return f"{raw:,.0f}"
    return f"{raw:g}"


# All keys referenced by GROUPS — used by tests to guard against drift
# between the definitions and ``report_values``.
ALL_KEYS = {m.key for group in GROUPS for m in group.metrics}

# Flat key → Metric index across every group (session metrics; MODEL_GROUPS
# appended below once defined).
METRIC_BY_KEY: dict[str, Metric] = {m.key: m for group in GROUPS for m in group.metrics}

# Populate each session Metric's ``fmt`` from the central _SESSION_FMT map so the
# format spec lives on the Metric object too (frozen dataclass → object.__setattr__).
for _m in METRIC_BY_KEY.values():
    object.__setattr__(_m, "fmt", _SESSION_FMT.get(_m.key, "text"))


# ============================================================
# Per-model SSOT (模型对比). Reuses the Metric dataclass to describe each
# ModelComparison display field. Values come from _MODEL_EXTRACTORS; tooltips are
# borrowed from the linked session metric via _MODEL_TIP_KEY so naming and
# explanation stay consistent across tabs. model_display reproduces the model
# tab's exact strings (K/M suffix, 免费 / ∞ / - special cases).
# ============================================================

def _fmt_tok(n: float) -> str:
    """Format a token count with K/M suffix (e.g. 1_500_000 → '1.5M').

    Retained for any external caller, but the model 对比 tab no longer uses it —
    per-model token metrics now render through ``format_value`` so they read
    identically to the 指标分类 grid (full comma-separated numbers).
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


MODEL_GROUPS: list[Group] = [
    Group("M_TOK", "Token 用量", [
        Metric("m_total_tokens", "总 Token", "", "", "basic"),
        Metric("m_input", "输入", "", "", "basic"),
        Metric("m_output", "输出", "", "", "basic"),
        Metric("m_cache_write", "缓存创建", "", "", "basic"),
        Metric("m_cache_read", "缓存命中", "", "", "basic"),
    ]),
    Group("M_COST", "成本", [
        Metric("m_cost", "总成本", "", "", "basic", "down"),
        Metric("m_cost_share", "成本占比", "", "", "basic"),
        Metric("m_tokens_per_dollar", "Token 效率", "", "", "basic", "up"),
        Metric("m_code_per_dollar", "代码效率", "", "", "basic", "up"),
    ]),
    Group("M_EFF", "效率", [
        Metric("m_token_share", "Token 占比", "", "", "basic"),
        Metric("m_cache_hit_ratio", "缓存命中率", "", "", "basic", "up"),
        Metric("m_session_count", "会话数", "", "", "basic"),
    ]),
    Group("M_QUAL", "代码质量与行为", [
        Metric("m_net_loc_per_session", "净增行/会话", "", "", "basic", "up"),
        Metric("m_tool_error_rate", "工具错误率", "", "", "basic", "down"),
        Metric("m_exploration_ratio", "探索占比", "", "", "basic"),
        Metric("m_edit_ratio", "编辑占比", "", "", "basic", "up"),
        Metric("m_read_write_ratio", "读写比", "", "", "basic", "up"),
        Metric("m_churn", "返工率", "", "", "basic", "down"),
        Metric("m_read_before_write", "先读后写率", "", "", "basic", "up"),
        Metric("m_files_per_session", "涉及文件/会话", "", "", "basic"),
    ]),
]

MODEL_BY_KEY: dict[str, Metric] = {m.key: m for g in MODEL_GROUPS for m in g.metrics}

# model metric key → session metric key whose name+tip is borrowed for the tooltip
# (None / absent → no tooltip, matching the model tab's prior behaviour).
_MODEL_TIP_KEY = {
    "m_total_tokens": "total_tokens", "m_input": "input", "m_output": "output",
    "m_cache_write": "cache_write", "m_cache_read": "cache_read", "m_cost": "cost",
    "m_cache_hit_ratio": "chr", "m_tool_error_rate": "tool_error_rate",
    "m_exploration_ratio": "exploration_ratio", "m_edit_ratio": "edit_ratio",
    "m_read_write_ratio": "read_write_ratio", "m_churn": "churn",
    "m_read_before_write": "read_before_write",
}


def _tpd_text(mc) -> str:
    if mc.tokens_per_dollar:
        return f"{mc.tokens_per_dollar:,.0f}/$"   # full count, like the grid
    return "∞" if mc.cost == 0 else "-"


def _cpd_text(mc) -> str:
    if mc.code_per_dollar is not None:
        return f"{mc.code_per_dollar:.1f} 行/$"
    return "∞" if mc.cost == 0 else "-"


def _cost_text(mc) -> str:
    return "免费" if mc.cost == 0 else format_value("cost", mc.cost)


# key → (numeric value for best-in-row, display string). Metrics that mirror a
# session metric format through ``format_value`` with that session key, so the
# model 对比 tab reads byte-identically to the 指标分类 grid. Model-only metrics
# (shares, per-session averages) use explicit formats consistent with the grid's
# conventions (full token counts, 1-dp ratios).
_MODEL_EXTRACTORS = {
    "m_total_tokens": (lambda mc: mc.total_tokens, lambda mc: format_value("total_tokens", mc.total_tokens)),
    "m_input": (lambda mc: mc.input_tokens, lambda mc: format_value("input", mc.input_tokens)),
    "m_output": (lambda mc: mc.output_tokens, lambda mc: format_value("output", mc.output_tokens)),
    "m_cache_write": (lambda mc: mc.cache_creation_tokens, lambda mc: format_value("cache_write", mc.cache_creation_tokens)),
    "m_cache_read": (lambda mc: mc.cache_read_tokens, lambda mc: format_value("cache_read", mc.cache_read_tokens)),
    "m_cost": (lambda mc: mc.cost, _cost_text),
    "m_cost_share": (lambda mc: mc.cost_share, lambda mc: f"{mc.cost_share:.1f}%"),
    "m_tokens_per_dollar": (lambda mc: mc.tokens_per_dollar, _tpd_text),
    "m_code_per_dollar": (lambda mc: mc.code_per_dollar, _cpd_text),
    "m_token_share": (lambda mc: mc.token_share, lambda mc: f"{mc.token_share:.1f}%"),
    "m_cache_hit_ratio": (lambda mc: mc.cache_hit_ratio, lambda mc: format_value("chr", mc.cache_hit_ratio)),
    "m_session_count": (lambda mc: mc.session_count, lambda mc: str(mc.session_count)),
    "m_net_loc_per_session": (lambda mc: mc.net_loc_per_session, lambda mc: f"{mc.net_loc_per_session:,.0f}" if mc.net_loc_per_session is not None else "-"),
    "m_tool_error_rate": (lambda mc: mc.tool_error_rate, lambda mc: format_value("tool_error_rate", mc.tool_error_rate)),
    "m_exploration_ratio": (lambda mc: mc.exploration_ratio, lambda mc: format_value("exploration_ratio", mc.exploration_ratio)),
    "m_edit_ratio": (lambda mc: mc.edit_ratio, lambda mc: format_value("edit_ratio", mc.edit_ratio)),
    "m_read_write_ratio": (lambda mc: mc.read_write_ratio, lambda mc: format_value("read_write_ratio", mc.read_write_ratio)),
    "m_churn": (lambda mc: mc.churn_ratio, lambda mc: format_value("churn", mc.churn_ratio)),
    "m_read_before_write": (lambda mc: mc.read_before_write, lambda mc: format_value("read_before_write", mc.read_before_write)),
    "m_files_per_session": (lambda mc: mc.files_per_session, lambda mc: f"{mc.files_per_session:.1f}" if mc.files_per_session is not None else "-"),
}


def model_raw(mc, key: str):
    """Numeric value of a per-model metric (for best-in-row highlighting)."""
    ext = _MODEL_EXTRACTORS.get(key)
    return ext[0](mc) if ext else None


def model_display(mc, key: str) -> str:
    """Display string of a per-model metric (model 对比 tab)."""
    ext = _MODEL_EXTRACTORS.get(key)
    return ext[1](mc) if ext else "-"


def model_tip(key: str) -> str | None:
    """Tooltip for a per-model metric, borrowed from the linked session metric."""
    sk = _MODEL_TIP_KEY.get(key)
    m = METRIC_BY_KEY.get(sk) if sk else None
    return f"{m.name}\n{m.tip}" if m else None


# ============================================================
# CTEI factor decomposition (综合效率分排名 → CTEI 因子分解).
# The four multiplicative factors of CTEI. Keys match export.ctei_decompose.
# Labels / formula text / formatting / 好坏阈值 live here (SSOT), so the ranking
# tab no longer defines its own.
# ============================================================

@dataclass(frozen=True)
class CteiFactor:
    key: str        # matches a key from export.ctei_decompose
    name: str       # display label (效率因子 / 产出密度 / …)
    formula: str    # short formula shown beside the bar


CTEI_FACTORS: list[CteiFactor] = [
    CteiFactor("eff_factor", "效率因子", "TCER÷基准"),
    CteiFactor("density_factor", "产出密度", "NCPI÷基准"),
    CteiFactor("cost_factor", "成本效率", "基准÷CPE"),
    CteiFactor("cache_factor", "缓存因子", "1+CHR×0.5"),
]

# A factor ≥ this sits at/above baseline (good); below it drags CTEI down.
CTEI_FACTOR_GOOD_THRESHOLD = 1.0


def format_factor(val: float) -> str:
    """Format a CTEI factor value (2 dp), matching the ranking tab."""
    return f"{val:.2f}"
