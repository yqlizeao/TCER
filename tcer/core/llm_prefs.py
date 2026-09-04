"""LLM 语义解读的配置持久化（opt-in 联网的第二处例外，同上传先例）。

存储 ``app_dirs.prefs_dir()/tcer_llm.json``：``base_url / api_key / model / scope``。
独立文件而非 tcer_ui.json 的 llm 段——换取 upload_prefs 同款原子写，并避开
ui_prefs 整体落盘对段的覆盖竞态（见 app._save_upload_config 的坑）。

api_key 明文存本机（与上传 token 同信任模型：本地单用户），配置弹窗声明。
未配置（base_url 或 model 为空）时 GUI 不出现任何 LLM 入口，零联网。
"""
from __future__ import annotations

import json
import os
import tempfile

from tcer.core.app_dirs import prefs_dir

SCOPES = ("metrics", "dialog", "tools")
DEFAULT_SCOPES = ["metrics", "dialog", "tools"]
DEFAULT_SCOPE = "full"
SCOPE_LABELS = {
    "metrics": "会话指标与时序",
    "dialog": "对话交互时间线",
    "tools": "工具调用与代码变更",
    "full": "完整明细",  # 兼容旧展示
}
SCOPE_DESCRIPTIONS = {
    "metrics": "包含会话概况（回合/Token/成本/TCER效率）、关键事件与逐回合走势数据",
    "dialog": "包含用户完整需求指令与 AI 回应全文（完整呈现交互全貌，不作摘要截断）",
    "tools": "包含调用工具命令、涉及文件路径及代码编辑详情（Edit差异/Write内容/命令行）",
    "full": "包含指标时序、完整对话全文与工具及代码编辑详情",
}


def _prefs_path():
    return prefs_dir() / "tcer_llm.json"


def normalize_scopes(raw) -> list[str]:
    """将历史 scope 字符串（'metrics'/'dialog'/'full'）或 scopes 列表规范化为列表。

    默认全部勾选：["metrics", "dialog", "tools"]。
    """
    if raw is None:
        return list(DEFAULT_SCOPES)
    if isinstance(raw, (list, tuple, set)):
        out: list[str] = []
        for item in raw:
            s = str(item).strip().lower()
            if s == "full":
                for k in ("metrics", "dialog", "tools"):
                    if k not in out:
                        out.append(k)
            elif s in SCOPES and s not in out:
                out.append(s)
        return out if out else list(DEFAULT_SCOPES)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s == "full":
            return ["metrics", "dialog", "tools"]
        if s == "dialog":
            return ["metrics", "dialog"]
        if s == "metrics":
            return ["metrics"]
        if s in SCOPES:
            return [s]
    return list(DEFAULT_SCOPES)


def scopes_summary(scopes: list[str]) -> str:
    """生成用于向后兼容 scope() 与报告展示的摘要标签。"""
    s_set = set(scopes)
    if s_set >= {"metrics", "dialog", "tools"}:
        return "full"
    if "dialog" in s_set and "tools" not in s_set and "metrics" in s_set:
        return "dialog"
    if s_set == {"metrics"}:
        return "metrics"
    short = {"metrics": "指标", "dialog": "对话", "tools": "工具"}
    return "+".join(short[s] for s in SCOPES if s in s_set) or "无"


def has_scope(target: str, scopes=None) -> bool:
    """判断目标范围是否在给定范围集合（或当前配置）中。"""
    active = normalize_scopes(scopes) if scopes is not None else scopes()
    s_set = set(active)
    if target == "full":
        return "tools" in s_set or "full" in s_set
    return target in s_set


def scope_level(scope) -> int:
    """档位比较兼容：metrics=0 / dialog=1 / full=2 / tools=2。"""
    if isinstance(scope, (list, tuple, set)):
        s_set = set(scope)
        if "tools" in s_set or "full" in s_set:
            return 2
        if "dialog" in s_set:
            return 1
        return 0
    s = str(scope).strip().lower()
    if s in ("full", "tools"):
        return 2
    if s == "dialog":
        return 1
    return 0

def load() -> dict:
    """Return the stored prefs merged over defaults (never raises)."""
    prefs = {
        "base_url": "", "api_key": "", "model": "",
        "scopes": list(DEFAULT_SCOPES), "scope": DEFAULT_SCOPE,
    }
    try:
        with _prefs_path().open("r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return prefs
    if not isinstance(stored, dict):
        return prefs
    for key in ("base_url", "api_key", "model"):
        val = stored.get(key)
        prefs[key] = val.strip() if isinstance(val, str) else ""
    raw_scopes = stored.get("scopes") if "scopes" in stored else stored.get("scope")
    prefs["scopes"] = normalize_scopes(raw_scopes)
    prefs["scope"] = scopes_summary(prefs["scopes"])
    return prefs


def save(cfg: dict) -> None:
    """Atomically persist the LLM config (mirrors upload_prefs.save)."""
    p = _prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, str(p))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def enabled() -> bool:
    """已配置（base_url 与 model 均非空）→ GUI 出现 LLM 入口。"""
    cfg = load()
    return bool(cfg["base_url"]) and bool(cfg["model"])


def stored_config() -> dict:
    """原始值回填（空串=未配置），供配置弹窗编辑。"""
    return load()


def base_url() -> str | None:
    url = load()["base_url"].rstrip("/")
    return url or None


def api_key() -> str | None:
    key = load()["api_key"]
    return key or None


def model() -> str | None:
    m = load()["model"]
    return m or None


def scope() -> str:
    return load()["scope"]


def scopes() -> list[str]:
    cfg = load()
    cur = scope()
    if cur and cur != cfg.get("scope"):
        return normalize_scopes(cur)
    return cfg["scopes"]
