"""llm_client 单测：全 mock urlopen，CI 零真实联网。"""
import io
import json
from unittest import mock

import pytest
import urllib.error

from tcer.core import llm_client


class _FakeResp:
    """test_update_check.py 同款：bytes 响应 + 上下文管理器协议。"""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok(content="分析结果"):
    return _FakeResp(json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode("utf-8"))


def _chat(**kw):
    args = dict(base_url="http://x", api_key=None, model="m", system="s", user="u")
    args.update(kw)
    return llm_client.chat(**args)


def test_chat_ok_and_request_shape():
    with mock.patch("urllib.request.urlopen", return_value=_ok()) as ur:
        out = _chat(base_url="http://localhost:11434", api_key="sk-x",
                    model="qwen3:8b")
    assert out == "分析结果"
    req = ur.call_args[0][0]
    assert req.full_url == "http://localhost:11434/v1/chat/completions"
    assert req.get_method() == "POST"
    assert req.headers["Content-type"] == "application/json; charset=utf-8"
    assert req.headers["Authorization"] == "Bearer sk-x"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "s"}
    assert body["messages"][1] == {"role": "user", "content": "u"}


def test_chat_no_key_omits_authorization():
    with mock.patch("urllib.request.urlopen", return_value=_ok("ok")) as ur:
        _chat()
    req = ur.call_args[0][0]
    assert "Authorization" not in req.headers


@pytest.mark.parametrize("err_body,want_msg", [
    (json.dumps({"error": "bad key"}), "bad key"),                       # error 为 str
    (json.dumps({"error": {"message": "model not found"}}), "model not found"),  # dict 形态
])
def test_chat_http_error_shapes(err_body, want_msg):
    err = urllib.error.HTTPError("http://x", 401, "Unauthorized", None,
                                 io.BytesIO(err_body.encode("utf-8")))
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(llm_client.LlmError) as ei:
            _chat()
    assert "HTTP 401" in str(ei.value)
    assert want_msg in str(ei.value)


def test_chat_url_error():
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError(OSError("refused"))):
        with pytest.raises(llm_client.LlmError) as ei:
            _chat()
    assert "无法连接" in str(ei.value)


def test_chat_bad_json():
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp(b"not-json")):
        with pytest.raises(llm_client.LlmError, match="无法解析"):
            _chat()


def test_chat_missing_choices_and_empty_content():
    with mock.patch("urllib.request.urlopen",
                    return_value=_FakeResp(json.dumps({"object": "list"}).encode())):
        with pytest.raises(llm_client.LlmError, match="choices"):
            _chat()
    with mock.patch("urllib.request.urlopen", return_value=_ok("")):
        with pytest.raises(llm_client.LlmError, match="空内容"):
            _chat()


@pytest.mark.parametrize("raw,want", [
    ("http://localhost:11434", "http://localhost:11434/v1"),
    ("http://localhost:11434/", "http://localhost:11434/v1"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1"),
    ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
    ("https://api.x.ai/v1", "https://api.x.ai/v1"),
    ("  https://api.x.ai  ", "https://api.x.ai/v1"),
    ("https://proxy.example/api", "https://proxy.example/api/v1"),  # 文档化行为
    ("", ""),
])
def test_normalize_base_url_matrix(raw, want):
    assert llm_client.normalize_base_url(raw) == want


def test_test_connection_two_paths():
    with mock.patch("urllib.request.urlopen", return_value=_ok("pong")):
        assert llm_client.test_connection(base_url="http://x", api_key=None,
                                          model="m") == "pong"
    err = urllib.error.HTTPError("http://x", 404, "Not Found", None,
                                 io.BytesIO(b"{}"))
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(llm_client.LlmError):
            llm_client.test_connection(base_url="http://x", api_key=None, model="m")
