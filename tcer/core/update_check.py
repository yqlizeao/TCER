"""应用内检查 GitHub Release 最新版本（opt-in 联网）。

纯标准库实现，**任何网络/解析异常都静默降级**（返回 ``None`` / ``False``），
绝不把网络错误抛到 GUI 主线程。版本号取自 ``tcer.__version__``；打包时由
CI 从 git tag 注入（见 ``.github/workflows/build-release.yml``）。

与 ``upload_client`` 同属「用户显式 opt-in 的联网行为」，未触发时零联网，
符合项目纯离线定位（CLAUDE.md 规范 3）。
"""
from __future__ import annotations

import json
import urllib.request

from tcer import __version__

# 仓库标识 —— 检查更新请求的目标。仓库迁移时同步这里。
GITHUB_REPO = "yqlizeao/TCER"
_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_TIMEOUT = 8  # 秒：后台线程联网检查，不应长时间阻塞


def parse_version(s):
    """把版本串解析为可比较的整数元组。

    ``"v1.2.3"`` → ``(1, 2, 3)``；``"1.2.0-beta"`` → ``(1, 2, 0)``；``""``/``None``
    → ``(0,)``。逐段取前导数字（非法/缺失段当 0），忽略预发布后缀。
    """
    s = str(s if s is not None else "").strip().lstrip("vV")
    if not s:
        return (0,)
    parts = []
    for seg in s.split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def _pad(a, b):
    """把两个版本元组右侧补 0 到等长，避免 ``(1,2)`` 与 ``(1,2,0)`` 比较歧义。"""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def latest_release():
    """查询 GitHub 最新 release。

    成功返回 dict::

        tag      — 原始 tag 名（如 "v1.2.3"）
        version  — 解析后的版本元组
        url      — release HTML 页（用户下载页）
        notes    — 发布说明正文（可能为空串）
        assets   — [(name, browser_download_url), ...]

    网络/解析失败、或返回体缺少 tag 时返回 ``None``。
    """
    try:
        req = urllib.request.Request(
            _API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "TCER/" + str(__version__),
            },
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    return {
        "tag": tag,
        "version": parse_version(tag),
        "url": (data.get("html_url") or "").strip(),
        "notes": (data.get("body") or "").strip(),
        "assets": [
            ((a.get("name") or ""), (a.get("browser_download_url") or ""))
            for a in data.get("assets", [])
        ],
    }


def is_newer(remote_tag, local=None):
    """``remote_tag`` 的版本是否**严格高于**本地版本。

    ``local`` 默认取 ``tcer.__version__``。任何输入异常返回 ``False``
    （宁可不提示，也不误报「有新版」）。
    """
    try:
        r, c = _pad(
            parse_version(remote_tag),
            parse_version(local if local is not None else __version__),
        )
        return r > c
    except Exception:
        return False
