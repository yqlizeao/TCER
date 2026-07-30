"""Tests for core/update_check.py（版本解析 + GitHub release 查询）。

latest_release 全程 mock urllib，不依赖网络即可跑。
"""
import json
from unittest import mock

from tcer.core import update_check


# --- parse_version ----------------------------------------------------------

def test_parse_version_basic():
    assert update_check.parse_version("v1.2.3") == (1, 2, 3)
    assert update_check.parse_version("1.2.3") == (1, 2, 3)
    assert update_check.parse_version("V2.0") == (2, 0)


def test_parse_version_prerelease_and_garbage():
    # 预发布后缀被忽略（只取每段前导数字）
    assert update_check.parse_version("1.2.0-beta") == (1, 2, 0)
    assert update_check.parse_version("1.2.3-rc1") == (1, 2, 3)
    # 非法/缺失段当 0
    assert update_check.parse_version("1.x.3") == (1, 0, 3)


def test_parse_version_empty():
    assert update_check.parse_version("") == (0,)
    assert update_check.parse_version(None) == (0,)


# --- is_newer（等长补齐，避免 1.2 vs 1.2.0 歧义）---------------------------

def test_is_newer_true():
    assert update_check.is_newer("v1.2.3", "1.2.0") is True
    assert update_check.is_newer("2.0.0", "1.9.9") is True


def test_is_newer_false_equal_and_lower():
    assert update_check.is_newer("1.2.0", "1.2.0") is False    # 相等
    assert update_check.is_newer("1.2", "1.2.0") is False      # 补齐后相等（1.2 == 1.2.0）
    assert update_check.is_newer("1.0.0", "1.0.1") is False    # 更低


def test_is_newer_bad_input_safe():
    # 异常输入不抛、不误报「有新版」
    assert update_check.is_newer(None, "1.0.0") is False
    assert update_check.is_newer("garbage", "1.0.0") is False


# --- latest_release（mock urllib，不联网）----------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload.encode("utf-8")

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_latest_release_ok():
    payload = {
        "tag_name": "v1.2.3",
        "html_url": "https://github.com/yqlizeao/TCER/releases/tag/v1.2.3",
        "body": "修复若干问题",
        "assets": [
            {"name": "TCER-windows-x64.exe",
             "browser_download_url": "https://example.com/win.exe"},
        ],
    }
    with mock.patch("urllib.request.urlopen",
                    return_value=_FakeResp(json.dumps(payload))):
        r = update_check.latest_release()
    assert r is not None
    assert r["tag"] == "v1.2.3"
    assert r["version"] == (1, 2, 3)
    assert r["url"].endswith("v1.2.3")
    assert r["notes"] == "修复若干问题"
    assert r["assets"][0][0] == "TCER-windows-x64.exe"


def test_latest_release_network_error_returns_none():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
        assert update_check.latest_release() is None


def test_latest_release_missing_tag_returns_none():
    payload = {"html_url": "x"}  # 无 tag_name
    with mock.patch("urllib.request.urlopen",
                    return_value=_FakeResp(json.dumps(payload))):
        assert update_check.latest_release() is None
