"""OpenAI-compatible chat 客户端（纯 urllib，零依赖）。

复刻 upload_client._post_json 的错误分类骨架。端点固定
``{base_url}/chat/completions``（非流式）；base_url 归一化到 ``/v1`` 结尾——
所有主流兼容服务（OpenAI/xAI/DeepSeek/Moonshot/Ollama/LM Studio/vLLM）的
chat completions 都挂在 /v1 下，用户贴裸 host 几乎必指 v1 API；不带 /v1 的
根路径代理 MVP 不支持（配置弹窗 placeholder 示例说明）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMEOUT_S = 120.0  # LLM 生成慢；urlopen 超时是逐次 socket 读写，非总墙钟
MAX_OUTPUT_TOKENS = 4096  # 四段结构化分析（≤1200 字）+ 引用余量
TEMPERATURE = 0.3


class LlmError(Exception):
    """人类可读的 LLM 调用失败（连接/HTTP/响应解析）。"""


def normalize_base_url(raw: str) -> str:
    """strip → 去尾斜杠 → 不以 /v1 结尾则补 /v1。"""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def chat_url(base_url: str) -> str:
    return normalize_base_url(base_url) + "/chat/completions"


def _post_chat(url: str, payload: dict, api_key: str | None, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    req.add_header("Connection", "close")
    if api_key:
        # Ollama 等本地服务无鉴权：空 key 不发 Authorization（部分代理对空
        # Bearer 返回 400）。
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"")
        except Exception:
            detail = None
        msg = detail.get("error") if isinstance(detail, dict) else detail
        if isinstance(msg, dict):  # {"error": {"message": ...}} 形态
            msg = msg.get("message")
        raise LlmError(f"HTTP {e.code}: {msg or e.reason}") from None
    except urllib.error.URLError as e:
        raise LlmError(f"无法连接 LLM 服务：{e.reason}") from None
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise LlmError("LLM 返回了无法解析的响应") from None


def chat(*, base_url: str, api_key: str | None, model: str, system: str,
         user: str, timeout: float = TIMEOUT_S,
         max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """一次非流式对话，返回首选项文本。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = _post_chat(chat_url(base_url), payload, api_key, timeout)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LlmError("LLM 响应缺少 choices[0].message.content") from None
    if not content:
        raise LlmError("LLM 返回了空内容")
    return content


def test_connection(*, base_url: str, api_key: str | None, model: str,
                    timeout: float = 30.0) -> str:
    """与真实链路同路径的最小 chat ping（验证端点+模型名+鉴权）。

    不用 GET /v1/models——部分兼容服务不实现该路由，而 chat ping 一次覆盖
    全部三个失败面。
    """
    return chat(base_url=base_url, api_key=api_key, model=model,
                system="You are a connectivity test.", user="ping",
                timeout=timeout, max_tokens=16)
