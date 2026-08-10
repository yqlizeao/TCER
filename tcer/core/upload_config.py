"""TCER 客户端上传配置（存于 ``tcer_ui.json`` 的 ``upload`` 段）。

历史上这三项配置（服务器地址 / Auth Token / 是否附带明细）由环境变量 +
``.env`` 文件驱动（旧 ``env_config`` 模块）。但"整体包 + ``tcer_ui.json``"的
分发形态下，再单独维护一个 ``.env`` 很别扭——配置应集中到与界面偏好同一个
``tcer_ui.json`` 里，随包分发、随包覆盖。

存储位置：``app_dirs.prefs_dir() / tcer_ui.json`` 的 ``upload`` 子对象
（与几何/分栏/筛选等界面偏好同文件，见 ``ui_prefs``）：

    {
      "upload": {
        "url": "https://your-server.example",  # 服务器地址；空 → 未配置
        "auth_token": "",              # Auth Token；空 → 匿名上传
        "detail": true                 # 是否附带每会话明细（对话）
      },
      "geometry": ...                  # 其它界面偏好
    }

设计要点：
- **默认显示上传入口**：不再以"是否配置了 URL"作为显隐开关；上传按钮常驻。
  ``url`` 未配置且无内置默认（开源库 :data:`DEFAULT_URL` 为空）时，上传前提示
  用户在弹窗里填写服务器地址。
- **打包默认**：整体包可随附一份带 ``upload`` 段的 ``tcer_ui.json`` 固定上传
  目标；用户也能在弹窗里改（页面可自助生成 auth_token 自用）。
- ``auth_token`` 为空 → 匿名；非空 → 服务器按 token 归属用户填 ``person``。

读写全程容错：缺失/损坏字段回退默认，写失败静默（与 ``ui_prefs`` 一致）。
"""
from __future__ import annotations

from tcer.core import ui_prefs

# 内置默认上传目标——留空：本项目开源，不硬编码任何具体服务器地址。整体包按需
# 随附带 upload.url 的 tcer_ui.json 提供默认；用户也可在上传弹窗里自行填写。
DEFAULT_URL = ""
# 默认附带每会话明细（对话）。
DEFAULT_DETAIL = True

_SECTION = "upload"


def _section() -> dict:
    """当前 ``tcer_ui.json`` 里的 ``upload`` 段（缺失/非法 → 空 dict）。"""
    data = ui_prefs.load().get(_SECTION)
    return data if isinstance(data, dict) else {}


def server_url() -> str | None:
    """配置的服务器地址（去尾斜杠）；未配置且无内置默认时返回 None。

    ``DEFAULT_URL`` 默认为空串（开源库不硬编码内网地址），故未配置 → None；
    上传前须校验 None 并提示用户填写。整体包可把 ``DEFAULT_URL`` 或随包
    ``tcer_ui.json`` 的 ``upload.url`` 设为真实地址。"""
    v = str(_section().get("url") or "").strip().rstrip("/")
    return v or (DEFAULT_URL.rstrip("/") or None)


def auth_token() -> str | None:
    """配置的 Auth Token，未配置返回 None（→ 匿名上传）。"""
    v = str(_section().get("auth_token") or "").strip()
    return v or None


def upload_detail() -> bool:
    """是否附带每会话明细。未配置时用内置默认 :data:`DEFAULT_DETAIL`。"""
    v = _section().get("detail")
    return bool(v) if isinstance(v, bool) else DEFAULT_DETAIL


def upload_enabled() -> bool:
    """上传入口是否显示。恒为 True——上传按钮默认常驻（配置移入 tcer_ui.json 后
    不再以"是否配置了服务器地址"作为显隐开关）。"""
    return True


def stored_config() -> dict:
    """当前存储的上传配置原始值（供 dialog 编辑回填）。

    与 ``server_url()``/``auth_token()`` 的**取值语义**不同：这里返回用户实际填的
    原始串（未回退默认/匿名），空串即"未配置"。``detail`` 缺失时给内置默认，让
    复选框有确定初值。``default_url`` 附带内置默认，供输入框占位提示。
    """
    sec = _section()
    return {
        "url": str(sec.get("url") or "").strip(),
        "auth_token": str(sec.get("auth_token") or "").strip(),
        "detail": sec.get("detail") if isinstance(sec.get("detail"), bool) else DEFAULT_DETAIL,
        "default_url": DEFAULT_URL,
    }


def save(*, url: str, auth_token: str, detail: bool) -> None:
    """把上传配置写回 ``tcer_ui.json`` 的 ``upload`` 段（load-merge，不抹其它段）。

    只持久化用户填的原始值（url/token 去首尾空白）：空串照存空串——``server_url``/
    ``auth_token`` 的取值逻辑会把空串当"未配置"回退默认/匿名，故存空即"清除"。
    与 ``ui_prefs`` 一致：写失败静默（配置丢了重填即可，不值得打断）。
    """
    prefs = ui_prefs.load()
    prefs[_SECTION] = {
        "url": (url or "").strip(),
        "auth_token": (auth_token or "").strip(),
        "detail": bool(detail),
    }
    ui_prefs.save(prefs)