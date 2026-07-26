"""GUI 界面偏好持久化：窗口几何 / 分栏位置 / 筛选状态 / 上次项目。

落盘 ``~/.claude/tcer_ui.json``（与 upload_prefs 同模式）。读写全程容错：
文件缺失/损坏返回空 dict，写失败静默——界面偏好丢了重设即可，不值得报错。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_PATH = Path.home() / ".claude" / "tcer_ui.json"

# WxH+X+Y（负偏移 = 多显示器/越界，允许）
_GEOMETRY_RE = re.compile(r"^\d{3,5}x\d{3,5}[+-]-?\d+[+-]-?\d+$")


def _prefs_path() -> Path:
    return _PATH


def load() -> dict:
    try:
        data = json.loads(_prefs_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(prefs: dict) -> None:
    try:
        p = _prefs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError:
        pass


def valid_geometry(geometry) -> bool:
    """True 当 geometry 是形如 ``1600x900+160+40`` 的合法串。"""
    return isinstance(geometry, str) and bool(_GEOMETRY_RE.match(geometry))
