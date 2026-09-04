"""LLM 解读报告的持久化（tcer_llm.json 的姊妹文件 tcer_llm_reports.json）。

报告由会话时间线弹窗的「LLM 解读」生成，保存后可在主界面「LLM 报告」页签
回看——生成它的弹窗早已关闭也不受影响。每条 entry 含会话快照（标题/模型/
回合数/净增/成本），不依赖会话文件仍然可读。保留最近 MAX_REPORTS 条，
超出删最旧。原子写同 upload_prefs/llm_prefs 模式。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from tcer.core.app_dirs import prefs_dir

MAX_REPORTS = 200


def _path() -> Path:
    return prefs_dir() / "tcer_llm_reports.json"


def load() -> list[dict]:
    """全部报告（新→旧）；文件缺失/损坏回空列表。"""
    try:
        with _path().open("r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(stored, list):
        return []
    reports = [r for r in stored if isinstance(r, dict)]
    reports.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return reports


def save(reports: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append(entry: dict) -> None:
    """追加一条并裁剪到 MAX_REPORTS（entry 需含 created_at 毫秒）。"""
    reports = load()
    reports.append(entry)
    reports.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    save(reports[:MAX_REPORTS])


def delete(report_id: str) -> None:
    save([r for r in load() if r.get("id") != report_id])


def clear() -> None:
    save([])
