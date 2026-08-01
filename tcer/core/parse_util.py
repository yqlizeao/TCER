"""跨 reader 共享的小型解析工具（此前在四个 reader 中逐字复制）。

只放实现完全一致、无源特化的函数；``_path_hint`` 之类按源键集不同的
helper 留在各自 reader 里。
"""
from __future__ import annotations

import re

# 纠正措辞（保守清单）：用户对上一轮输出不满意的显式信号。只在消息前 200 字符
# 内匹配，避免长引用文本误报；命中即计入 correction_msg_count。跨源共享（Claude/
# omp/pi）——user 文本消息同构，共用一张正则避免各 reader 漂移。
CORRECTION_RE = re.compile(
    r"不对|错了|不是这样|重来|重新来|重新做|撤销|回退|回滚|别这么|别这样"
    r"|\bundo\b|\brevert\b|\bwrong\b|\bredo\b",
    re.IGNORECASE,
)


def is_slash_command(txt: str) -> bool:
    """True 当用户消息是斜杠命令或经命令面板发送（``<command-name>``）。"""
    return txt.startswith("/") or txt.startswith("<command-name>")


def is_correction(txt: str) -> bool:
    """True 当用户消息前 200 字符含显式纠正措辞。"""
    return bool(CORRECTION_RE.search(txt[:200]))


def as_int(v) -> int:
    """宽容整数化：None/bool/不可转换 → 0。"""
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def first_str(*values) -> str | None:
    """返回第一个非空字符串参数，全空则 None。"""
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None