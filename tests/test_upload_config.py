"""upload_config：客户端上传配置从 tcer_ui.json 的 upload 段读取。

历史上走环境变量 + .env（旧 env_config）；迁移后集中到 tcer_ui.json，
上传按钮默认常驻（upload_enabled 恒真），未配置字段回退内置默认。
"""
from __future__ import annotations

import json

from tcer.core import ui_prefs, upload_config


def _point_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_prefs, "_prefs_path", lambda: tmp_path / "tcer_ui.json")


def _write_prefs(tmp_path, data: dict) -> None:
    (tmp_path / "tcer_ui.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_defaults_when_unconfigured(tmp_path, monkeypatch):
    """无 tcer_ui.json / 无 upload 段：开源库无内置默认 URL → server_url 为 None，
    但按钮仍显示（upload_enabled 恒真），用户在弹窗里填地址。"""
    _point_prefs(tmp_path, monkeypatch)
    assert upload_config.DEFAULT_URL == ""               # 不硬编码任何地址
    assert upload_config.server_url() is None            # 未配置 → None
    assert upload_config.auth_token() is None             # 匿名
    assert upload_config.upload_detail() is upload_config.DEFAULT_DETAIL
    assert upload_config.upload_enabled() is True         # 默认常驻


def test_reads_configured_values(tmp_path, monkeypatch):
    _point_prefs(tmp_path, monkeypatch)
    _write_prefs(tmp_path, {"upload": {
        "url": "https://tcer.example.com/",   # 尾斜杠应被去掉
        "auth_token": "tcer_abc123",
        "detail": False,
    }})
    assert upload_config.server_url() == "https://tcer.example.com"
    assert upload_config.auth_token() == "tcer_abc123"
    assert upload_config.upload_detail() is False
    assert upload_config.upload_enabled() is True


def test_blank_fields_fall_back(tmp_path, monkeypatch):
    """空串字段视为未配置：url → None、token → 匿名。"""
    _point_prefs(tmp_path, monkeypatch)
    _write_prefs(tmp_path, {"upload": {"url": "  ", "auth_token": "  "}})
    assert upload_config.server_url() is None
    assert upload_config.auth_token() is None
    # detail 缺失 → 内置默认
    assert upload_config.upload_detail() is upload_config.DEFAULT_DETAIL


def test_malformed_upload_section_ignored(tmp_path, monkeypatch):
    """upload 段非 dict（历史脏数据）不崩，回退默认。"""
    _point_prefs(tmp_path, monkeypatch)
    _write_prefs(tmp_path, {"upload": "not-a-dict"})
    assert upload_config.server_url() is None
    assert upload_config.auth_token() is None
    assert upload_config.upload_detail() is upload_config.DEFAULT_DETAIL


def test_detail_non_bool_ignored(tmp_path, monkeypatch):
    """detail 非布尔（如字符串 'true'）不误判，用内置默认。"""
    _point_prefs(tmp_path, monkeypatch)
    _write_prefs(tmp_path, {"upload": {"detail": "true"}})
    assert upload_config.upload_detail() is upload_config.DEFAULT_DETAIL


def test_stored_config_returns_raw_values(tmp_path, monkeypatch):
    """stored_config 返回原始值（未回退默认/匿名），供 dialog 编辑回填。"""
    _point_prefs(tmp_path, monkeypatch)
    # 未配置：url/token 空串（原始值，非取值语义），detail 用默认，附带 default_url
    c = upload_config.stored_config()
    assert c == {"url": "", "auth_token": "",
                 "detail": upload_config.DEFAULT_DETAIL,
                 "default_url": upload_config.DEFAULT_URL}
    _write_prefs(tmp_path, {"upload": {"url": "https://x.io", "auth_token": "t1", "detail": False}})
    c2 = upload_config.stored_config()
    assert c2["url"] == "https://x.io" and c2["auth_token"] == "t1" and c2["detail"] is False


def test_save_roundtrip_and_getter_semantics(tmp_path, monkeypatch):
    """save 写回后 getter 读到新值；空 url/token 存空串 → 取值回退默认/匿名。"""
    _point_prefs(tmp_path, monkeypatch)
    upload_config.save(url="https://srv/", auth_token="  tok  ", detail=False)
    assert upload_config.server_url() == "https://srv"   # 去尾斜杠
    assert upload_config.auth_token() == "tok"            # 去空白
    assert upload_config.upload_detail() is False
    # 清空：存空串，取值 → 未配置（None）/ 匿名
    upload_config.save(url="", auth_token="", detail=True)
    assert upload_config.stored_config()["url"] == ""
    assert upload_config.server_url() is None
    assert upload_config.auth_token() is None
    assert upload_config.upload_detail() is True


def test_save_merges_preserves_other_sections(tmp_path, monkeypatch):
    """save 用 load-merge：不抹掉 tcer_ui.json 里的其它界面偏好段。"""
    _point_prefs(tmp_path, monkeypatch)
    _write_prefs(tmp_path, {"geometry": "1600x900+10+10", "last_project": "claude::x"})
    upload_config.save(url="https://srv", auth_token="", detail=True)
    data = json.loads((tmp_path / "tcer_ui.json").read_text(encoding="utf-8"))
    assert data["geometry"] == "1600x900+10+10"          # 其它段保留
    assert data["last_project"] == "claude::x"
    assert data["upload"]["url"] == "https://srv"