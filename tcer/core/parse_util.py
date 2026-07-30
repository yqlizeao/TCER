"""跨 reader 共享的小型解析工具（此前在四个 reader 中逐字复制）。

只放实现完全一致、无源特化的函数；``_path_hint`` 之类按源键集不同的
helper 留在各自 reader 里。
"""
from __future__ import annotations


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
