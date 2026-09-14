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

TIMEOUT_S = 300.0  # LLM 生成慢（长输出 2500~5000 字时更甚；Gemini 3.8 Flash 实测 TTFT p95 ~9s、
                   # 吞吐慢尾 ~58 字符/秒，思考+正文合计可达数分钟）；urlopen 超时是逐次 socket
                   # 读写，非总墙钟
MAX_OUTPUT_TOKENS = 32768  # 审计级深挖报告（2500~5000 字）+ 遥测 JSON 余量。刻意留双倍余量：
                           # Gemini 系思考 token 计入输出预算（medium 档默认 8192），不吃满时无副作用；
                           # 65,536 是 Gemini 3.8 Flash 的输出上限，32768 在其内安全。
# 注意：刻意不发送 temperature——Gemini 3.6+（含 3.8 Flash）官方已忽略采样参数且预告未来
# 版本将报 400；Claude 新模型（Fable/Sonnet 5+）非默认值同样 400。跨端点兼容的最稳做法
# 是完全不发采样参数，让服务端用自己的默认（审计任务靠 system prompt 约束风格）。


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
    """一次非流式对话，返回首选项文本。

    刻意不发 temperature 等采样参数（见模块头注释：Gemini 3.6+ 忽略并将报错、
    Claude 新模型非默认值直接 400）；思考预算也不显式设置（reasoning_effort 与
    thinking_level 二选一且各端点支持不一，模型默认档即可）。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
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
