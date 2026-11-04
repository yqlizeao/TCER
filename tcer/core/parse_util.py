"""跨 reader 共享的小型解析工具（此前在四个 reader 中逐字复制）。

只放实现完全一致、无源特化的函数；``_path_hint`` 之类按源键集不同的
helper 留在各自 reader 里。
"""
from __future__ import annotations

import ntpath
import os
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


_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def is_windows_path(p: str | None) -> bool:
    """True 当 *p* 是 Windows 盘符路径（``C:\\...`` / ``C:/...``）。"""
    return bool(p) and bool(_DRIVE_RE.match(p))


def norm_path(p: str) -> str:
    """与宿主 OS 无关的路径规范化。

    Windows 盘符路径一律按 Windows 语义（``ntpath``）规范化——同一份会话
    数据在 Windows / macOS / Linux 下得到相同的项目 key 与展示名；
    POSIX 路径仍走 ``os.path``。在 Windows 上与 ``os.path.normpath`` 完全一致。
    """
    if is_windows_path(p):
        return ntpath.normpath(p)
    return os.path.normpath(p)


def normcase_path(p: str) -> str:
    """与宿主 OS 无关的 normcase+normpath（项目聚合 key 用）。

    在 Windows 上与 ``os.path.normcase(os.path.normpath(p))`` 完全一致；
    在 POSIX 上让 ``C:\\GitHub\\TCER`` 与 ``c:/github/TCER`` 正常折叠合并。
    """
    if is_windows_path(p):
        return ntpath.normcase(ntpath.normpath(p))
    return os.path.normcase(os.path.normpath(p))


def basename_of(p: str) -> str:
    """与宿主 OS 无关的 basename（``C:\\repo\\app`` 在任何平台都得 ``app``）。

    在 Windows 上与 ``Path(p).name`` 一致；空结果（如裸盘符 ``C:\\``）返回 ""。
    """
    if is_windows_path(p):
        return ntpath.basename(ntpath.normpath(p))
    return os.path.basename(os.path.normpath(p))


def has_parent_dir(p: str) -> bool:
    """路径是否含父目录（排除 ``C:\\`` 这类裸盘符根）。"""
    if is_windows_path(p):
        _drive, rest = ntpath.splitdrive(norm_path(p))
        return rest not in ("", "\\", "/")
    from pathlib import Path

    return len(Path(p).parts) > 1