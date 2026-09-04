"""llm_prompts 单测：scope 三档分层 + 采样规则 + 粗估。"""
from pathlib import Path

from tcer.core import metrics
from tcer.core.llm_prompts import (
    PROMPT_VERSION, convergence_prompt, estimate_request_tokens, estimate_tokens,
    sample_user_texts,
)
from tcer.core.models import SessionMeta, TokenUsage, ToolOp, TurnStat


def _report():
    meta = SessionMeta(session_id="s1", cwd="/tmp", title=None,
                       path=Path("/tmp/s1.jsonl"), is_subagent=False)
    u = TokenUsage(input_tokens=200_000, output_tokens=100_000,
                   cache_read_input_tokens=700_000, models={"claude-opus-4-8"},
                   assistant_msgs=3, user_msgs=2,
                   tool_calls={"Read": 2, "Edit": 3})
    u.turn_stats = [
        TurnStat(0, ts=1, input_tokens=1000, cache_read=5000, output_tokens=800,
                 duration_ms=4000),
        TurnStat(1, ts=2, input_tokens=1200, cache_read=6000, output_tokens=900,
                 errors=1),
        TurnStat(2, ts=3, input_tokens=50_000, cache_write=6000,
                 cache_read=1000, output_tokens=5000),
    ]
    u.tool_ops = [ToolOp(0, "Read", "a.py"), ToolOp(1, "Edit", "a.py"),
                  ToolOp(1, "Edit", "a.py"), ToolOp(1, "Edit", "a.py")]
    return metrics.compute(meta, u, net_loc=38, task_type="feature")


def _derived():
    return {
        "stats": [
            TurnStat(0, ts=1, input_tokens=1000, cache_read=5000,
                     output_tokens=800, duration_ms=4000),
            TurnStat(1, ts=2, input_tokens=1200, cache_read=6000,
                     output_tokens=900, errors=1),
            TurnStat(2, ts=3, input_tokens=50_000, cache_write=6000,
                     cache_read=1000, output_tokens=5000),
        ],
        "cum_net": [10, 8, 38],
        "cum_cost": [0.010, 0.021, 0.500],
        "retry_spans": [(1, 1)],
        "retry_details": {"Edit:a.py": 3},
        "spike_turn": 2,
        "cinv_turns": [2],
        "compaction_turns": [1],
        "ops_by_turn": {0: [ToolOp(0, "Read", "a.py")],
                        1: [ToolOp(1, "Edit", "a.py"), ToolOp(1, "Edit", "a.py"),
                            ToolOp(1, "Edit", "a.py")]},
        "loc_by_turn": {1: (5, 2)},
        "hot_files": {"a.py": 4},
    }


def test_metrics_scope_excludes_texts_and_paths():
    system, user = convergence_prompt(_report(), _derived(), "metrics")
    assert PROMPT_VERSION in system
    assert "回合" in user and "TCER" in user          # 时序与指标在
    assert "重试循环区间(回合) 2-2" in user            # 事件仅回合号
    assert "a.py" not in user                          # 无任何路径
    assert "[消息" not in user                          # 无用户消息


def test_dialog_scope_falls_back_to_user_texts():
    """非 Claude 源（无 dialogue）：回退用户消息采样，仍无路径。"""
    system, user = convergence_prompt(
        _report(), _derived(), "dialog",
        dialogue=None, user_texts=["帮我做一个工具", "不对，这里改错了"])
    assert "[消息 1/2]" in user
    assert "不对，这里改错了" in user                   # 纠正消息入选
    assert "a.py" not in user
    assert "无 AI 回应文本" in user                     # 回退路径有标注


def test_dialog_scope_prefers_dialogue():
    """Claude 源：完整对话时间线是数据主体（用户全文+AI 摘要+工具行）。"""
    system, user = convergence_prompt(
        _report(), _derived(), "dialog",
        dialogue=["[用户] 帮我做一个 TCER 的图表",
                  "[AI] 好的，我先看一下现有代码结构。",
                  "[工具] Read tcer/gui/charts.py",
                  "[用户] 不对，我说的是趋势图"],
        user_texts=["不该出现"])
    assert "[对话时间线]" in user
    assert "[用户] 帮我做一个 TCER 的图表" in user
    assert "[AI] 好的，我先看一下现有代码结构。" in user
    assert "[工具] Read tcer/gui/charts.py" in user     # 工具名+路径在对话流里
    assert "不该出现" not in user                        # dialogue 优先于采样
    assert "无 AI 回应文本" not in user


def test_clip_dialogue_head_tail_kept():
    from tcer.core.llm_prompts import clip_dialogue
    lines = [f"[用户] 消息行内容{i}" for i in range(400)]
    text = clip_dialogue(lines, budget=1000)
    assert len(text) < 1300
    assert "中段省略" in text and "已截断" in text
    assert "消息行内容0" in text        # 头保留（意图在头）
    assert "消息行内容399" in text      # 尾保留（结局在尾）


def test_full_scope_adds_tool_detail_with_paths():
    system, user = convergence_prompt(_report(), _derived(), "full", ["消息"])
    assert "Edit a.py" in user                          # 工具+路径
    assert "(+5/-2)" in user                            # 增删行数
    assert "Edit:a.py ×3" in user                       # 重试明细
    assert "热点文件" in user


def test_dialog_scope_none_texts_safe():
    _, user = convergence_prompt(_report(), _derived(), "dialog", None)
    assert "（本会话无可用用户消息）" in user


def test_sample_user_texts_budget_and_priority():
    texts = ["第一条任务描述"] \
        + [f"普通消息{i}" + "字" * 60 for i in range(28)] \
        + ["不对，重来"]
    out = sample_user_texts(texts)
    joined = "\n".join(out)
    assert len(joined) <= 6000 + 200                    # 预算约束（含前缀余量）
    assert "[消息 1/30]" in joined                      # 首条必选
    assert "不对，重来" in joined                        # 纠正优先于更长普通条
    assert f"已采样 {len(out) - 1}/30" in joined or "已采样" not in joined


def test_sample_user_texts_empty():
    assert sample_user_texts([]) == []


def test_estimate_tokens_monotonic_nonzero():
    assert estimate_tokens("你好世界") > 0
    assert estimate_tokens("aaaa") < estimate_tokens("aaaaaaaa")
    # CJK 1 字/token：4 个汉字 > 4 个 ASCII（1 token）
    assert estimate_tokens("你好世界") > estimate_tokens("aaaa")


def test_estimate_request_tokens_grows_with_scope():
    r, d = _report(), _derived()
    m = estimate_request_tokens(r, d, "metrics")
    dg = estimate_request_tokens(r, d, "dialog", user_texts=["消息" * 10])
    f = estimate_request_tokens(r, d, "full", user_texts=["消息" * 10])
    assert 0 < m < dg < f


def test_multi_select_scopes():
    # 仅勾选指标与工具，不勾选对话
    _, user = convergence_prompt(_report(), _derived(), ["metrics", "tools"],
                                 dialogue=["[用户] 秘密对话"])
    assert "TCER" in user
    assert "Edit a.py" in user
    assert "秘密对话" not in user

    # 全部勾选
    _, user_all = convergence_prompt(_report(), _derived(), ["metrics", "dialog", "tools"],
                                     dialogue=["[用户] 秘密对话"])
    assert "TCER" in user_all
    assert "秘密对话" in user_all
    assert "Edit a.py" in user_all


def test_dynamics_prompt_and_payload_parsing():
    from tcer.core.llm_prompts import (
        DYNAMICS_PROMPT_VERSION, dynamics_prompt, parse_dynamics_payload,
    )
    system, user = dynamics_prompt(_report(), _derived(), ["metrics", "dialog", "tools"],
                                  dialogue=["[用户] 初始架构提示词"])
    assert DYNAMICS_PROMPT_VERSION in system
    assert "狄拉克" in system
    assert "吸引子" in system
    assert "[对话时间线]" in user

    sample_reply = (
        "1. 【初始意图降熵评估】\n"
        "首轮提示词质量较高。\n\n"
        "```json\n"
        "{\n"
        '  "convergence_type": "dirac",\n'
        '  "capabilities": {"intent_formalization": 90}\n'
        "}\n"
        "```"
    )
    text, data = parse_dynamics_payload(sample_reply)
    assert "首轮提示词质量较高" in text
    assert "```json" not in text
    assert data and data["convergence_type"] == "dirac"
    assert data["capabilities"]["intent_formalization"] == 90

    # 容错：纯文本无 JSON 时回退
    text_raw, data_none = parse_dynamics_payload("纯文本报告")
    assert text_raw == "纯文本报告" and data_none is None

    # 容错：尾部多余逗号清洗与 event 字段解析
    sample_reply_with_trailing_comma = (
        "报告内容分析\n\n"
        "```json\n"
        "{\n"
        '  "convergence_type": "trapped",\n'
        '  "attractor_trapped": true,\n'
        '  "trajectory": [\n'
        '    {"turn": 1, "semantic_distance": 0.8, "vector": "positive", "event": "normal",},\n'
        '    {"turn": 5, "semantic_distance": 0.85, "vector": "trapped", "event": "retry_loop",},\n'
        '  ],\n'
        '  "capabilities": {\n'
        '    "intent_formalization": 70,\n'
        '  },\n'
        "}\n"
        "```"
    )
    text2, data2 = parse_dynamics_payload(sample_reply_with_trailing_comma)
    assert "报告内容分析" in text2
    assert data2 and data2["convergence_type"] == "trapped"
    assert len(data2["trajectory"]) == 2
    assert data2["trajectory"][1]["event"] == "retry_loop"

    # 工具反馈行纳入出境
    _, user_feedback = convergence_prompt(_report(), _derived(), ["metrics", "tools"],
                                          dialogue=["[工具] Bash pytest", "[工具反馈:报错] FAILED test_x"])
    assert "[工具反馈:报错] FAILED test_x" in user_feedback
