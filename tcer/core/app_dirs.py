"""TCER 自有配置文件的存储目录(与 Claude 数据源 ``~/.claude`` 分离)。

Why: ``~/.claude`` 是 Claude Code 的目录,TCER 是独立产品,不应把自身配置塞
进去。数据仍从 ``~/.claude`` 等读(Claude 会话数据在那里),但 TCER 自己的
配置文件(``tcer_ui.json`` / ``tcer_upload.json``)写到本模块返回的目录。

定位策略:
- 发布版(PyInstaller 打包,``sys.frozen``):优先 **exe 同目录**(便携——
  删 exe 即净配置,适合绿色免安装分发);
- exe 同目录不可写(只读目录 / 权限不足),或源码运行(``python -m tcer``):
  回退到 ``~/.tcer/``。

迁移:首次定位到新目录时,若旧位置 ``~/.claude/`` 下存在 TCER 配置文件且新目录
没有,一次性复制过来(老用户不丢设置)。旧文件不删(那是 Claude 的目录)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_LEGACY_DIR = Path.home() / ".claude"
# 旧位置曾存放的 TCER 配置文件名(迁移用)。
_LEGACY_FILES = ("tcer_ui.json", "tcer_upload.json")
_CACHE: Path | None = None


def _writable(base: Path) -> bool:
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".tcer_probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _compute() -> Path:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path.home() / ".tcer")
    for base in candidates:
        if _writable(base):
            return base
    # 全都不可写:仍返回 ~/.tcer(save 容错会静默失败,不至于崩)
    return Path.home() / ".tcer"


def _migrate_legacy(new_dir: Path) -> None:
    try:
        if not _LEGACY_DIR.exists():
            return
        for name in _LEGACY_FILES:
            src = _LEGACY_DIR / name
            dst = new_dir / name
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
    except OSError:
        pass


def prefs_dir() -> Path:
    """TCER 配置目录(发布版优先 exe 同目录,回退 ~/.tcer;结果进程内缓存)。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _compute()
        _migrate_legacy(_CACHE)
    return _CACHE


def reset_cache() -> None:
    """清掉进程内缓存(测试用:换目录后重新计算)。"""
    global _CACHE
    _CACHE = None
