"""TokenUsage.merge 完备性护栏。

merge() 手写逐字段求和——历史上新增字段极易漏合并。本测试反射遍历全部
dataclass 字段、构造非零值,断言「与空对象 merge 后逐字段保持原值」:
任何被 merge() 遗漏的字段会落回构造默认值而当场失败。
"""
from __future__ import annotations

from dataclasses import fields

from tcer.core.models import ModelUsage, TokenUsage, ToolOp, TurnStat

# 按字段名特化的样本值(容器/时间戳等无法从类型统一推断的字段)。
_SPECIAL = {
    "models": {"model-a", "model-b"},
    "per_model": None,  # 用 bucket() 填,见下
    "tool_calls": {"Read": 3, "Edit": 2},
    "tool_variants": {"Skill:dataviz": 2, "Agent:Explore": 1},
    "tool_errors_by_tool": {"Edit": 1},
    "tool_ops": [ToolOp(0, "Read", "a.py"), ToolOp(1, "Edit", "a.py")],
    "turn_stats": [TurnStat(0, ts=1_700_000_000_000, input_tokens=5,
                            output_tokens=3, duration_ms=100)],
    "user_message_texts": ["你好"],
    "rate_limit_names": {"primary"},
    "ttft_ms_samples": [500, 900],
    "abort_reasons": {"interrupted": 2},
    "mcp_calls_by_attr": {"monolith/editor_query": 3},
    "started_at": 1_700_000_000_000,
    "ended_at": 1_700_000_060_000,
    "session_duration_ms": 60_000,  # 与 started/ended 一致,merge 重算后不变
}


def _populated_usage() -> TokenUsage:
    u = TokenUsage()
    n = 100
    for f in fields(TokenUsage):
        if f.name in _SPECIAL:
            v = _SPECIAL[f.name]
            if v is not None:
                setattr(u, f.name, v)
            continue
        # 其余全是 int / int|None 计数或量值:给每个字段一个唯一非零整数。
        n += 1
        setattr(u, f.name, n)
    u.bucket("model-a").add(10, 2, 3, 4, 1)
    return u


def test_merge_with_empty_preserves_every_field():
    a = _populated_usage()
    merged = a.merge(TokenUsage())
    for f in fields(TokenUsage):
        got = getattr(merged, f.name)
        want = getattr(a, f.name)
        assert got == want, (
            f"TokenUsage.merge 丢失字段 {f.name!r}: {got!r} != {want!r} "
            "(新增字段后忘了改 merge()?)"
        )


def test_merge_empty_left_side_preserves_every_field():
    a = _populated_usage()
    merged = TokenUsage().merge(a)
    for f in fields(TokenUsage):
        got = getattr(merged, f.name)
        want = getattr(a, f.name)
        assert got == want, f"TokenUsage().merge(a) 丢失字段 {f.name!r}"


def test_model_usage_add_covers_every_field():
    """ModelUsage.add 覆盖全部计数字段(同样防新增字段漏加)。"""
    a = ModelUsage()
    n = 0
    kwargs = []
    for f in fields(ModelUsage):
        n += 1
        setattr(a, f.name, n)
        kwargs.append(n)
    b = ModelUsage()
    b.add(*kwargs)
    for f in fields(ModelUsage):
        assert getattr(b, f.name) == getattr(a, f.name), (
            f"ModelUsage.add 未覆盖字段 {f.name!r}")
