"""llm_prefs 单测：monkeypatch _prefs_path 到 tmp（test_upload_config 模式）。"""
import json


def _use_tmp(monkeypatch, tmp_path):
    from tcer.core import llm_prefs
    monkeypatch.setattr(llm_prefs, "_prefs_path",
                        lambda: tmp_path / "tcer_llm.json")
    return llm_prefs


def test_missing_file_defaults_disabled(monkeypatch, tmp_path):
    lp = _use_tmp(monkeypatch, tmp_path)
    cfg = lp.load()
    assert cfg["base_url"] == "" and cfg["api_key"] == "" and cfg["model"] == ""
    assert cfg["scopes"] == ["metrics", "dialog", "tools"]
    assert cfg["scope"] == "full"
    assert lp.scopes() == ["metrics", "dialog", "tools"]
    assert lp.enabled() is False
    assert lp.base_url() is None and lp.model() is None


def test_enabled_requires_url_and_model(monkeypatch, tmp_path):
    lp = _use_tmp(monkeypatch, tmp_path)
    lp.save({"base_url": "http://x", "api_key": "", "model": "", "scope": "full"})
    assert lp.enabled() is False
    lp.save({"base_url": "", "api_key": "k", "model": "m", "scope": "metrics"})
    assert lp.enabled() is False
    lp.save({"base_url": "http://x", "api_key": "k", "model": "m", "scope": "full"})
    assert lp.enabled() is True


def test_save_load_roundtrip_trailing_slash(monkeypatch, tmp_path):
    lp = _use_tmp(monkeypatch, tmp_path)
    lp.save({"base_url": "http://x:11434/", "api_key": "k", "model": "qwen3:8b",
             "scope": "dialog"})
    assert lp.base_url() == "http://x:11434"   # getter 去尾斜杠
    assert lp.api_key() == "k"
    assert lp.model() == "qwen3:8b"
    assert lp.scope() == "dialog"
    assert lp.scopes() == ["metrics", "dialog"]

def test_invalid_scope_falls_back(monkeypatch, tmp_path):
    lp = _use_tmp(monkeypatch, tmp_path)
    (tmp_path / "tcer_llm.json").write_text(
        json.dumps({"base_url": "http://x", "model": "m", "scope": "mega"}),
        encoding="utf-8")
    assert lp.scope() == "full"       # 无效值回退默认全选
    assert lp.scopes() == ["metrics", "dialog", "tools"]
    assert lp.scope_level("mega") == 0
    assert lp.scope_level("full") == 2
    assert lp.scope_level(["metrics", "dialog", "tools"]) == 2
    assert lp.has_scope("metrics", ["metrics", "tools"]) is True
    assert lp.has_scope("dialog", ["metrics", "tools"]) is False
    assert lp.has_scope("tools", ["metrics", "tools"]) is True


def test_stored_config_raw_semantics(monkeypatch, tmp_path):
    lp = _use_tmp(monkeypatch, tmp_path)
    lp.save({"base_url": "", "api_key": "", "model": "", "scope": "metrics"})
    raw = lp.stored_config()
    assert raw["base_url"] == ""       # 原始空串（弹窗回填语义）
    assert lp.base_url() is None       # getter None 语义


def test_atomic_write_no_tmp_leftover(monkeypatch, tmp_path):
    lp = _use_tmp(monkeypatch, tmp_path)
    lp.save({"base_url": "http://x", "api_key": "", "model": "m",
             "scope": "metrics"})
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "tcer_llm.json").exists()
