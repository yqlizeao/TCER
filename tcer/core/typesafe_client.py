"""TypeSafe AI (System One / Jev) 客户端（纯 urllib，零第三方依赖）。

端点：``POST {base_url}/v1/systemone``
文档参考：https://docs.typesafe.ai/api
专职于 System One 判定模型（如 jev-latest / jev-preview），接收 state 与强类型
questions 映射，返回带校准概率与置信度（confidence）的类型化决策。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
TIMEOUT_S = 25.0


class TypesafeError(Exception):
    """人类可读的 TypeSafe API 调用失败（网络/鉴权/参数/限流）。"""


def normalize_base_url(raw: str) -> str:
    """strip → 去尾斜杠 → 去除 /v1 尾部（内部统一管理端点路径）。"""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return DEFAULT_BASE_URL
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url or DEFAULT_BASE_URL


def evaluate_url(base_url: str) -> str:
    return normalize_base_url(base_url) + "/v1/systemone"


def _send_request(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        detail = None
        if body:
            try:
                detail = json.loads(body.decode("utf-8", errors="ignore"))
            except Exception:
                detail = None
        err_msg = ""
        if isinstance(detail, dict):
            err_msg = detail.get("error") or detail.get("message") or ""
            if isinstance(err_msg, dict):
                err_msg = err_msg.get("message") or str(err_msg)
        if not err_msg and body:
            err_msg = body.decode("utf-8", errors="ignore")[:300]

        if e.code == 401:
            raise TypesafeError(f"TypeSafe 鉴权失败 (HTTP 401)：API Key 无效或缺失。{err_msg}".strip()) from None
        elif e.code == 422:
            raise TypesafeError(f"TypeSafe 请求校验失败 (HTTP 422)：{err_msg or '参数格式不符合 Schema 规范'}") from None
        elif e.code == 429:
            raise TypesafeError(f"TypeSafe 请求过于频繁 (HTTP 429)：触发速率限制，请稍后重试。{err_msg}".strip()) from None
        elif e.code == 529:
            raise TypesafeError(f"TypeSafe 服务暂时过载 (HTTP 529)：请稍后重试。{err_msg}".strip()) from None
        else:
            raise TypesafeError(f"TypeSafe 服务异常 (HTTP {e.code})：{err_msg or e.reason}") from None
    except urllib.error.URLError as e:
        raise TypesafeError(f"无法连接 TypeSafe 服务：{e.reason}") from None
    except Exception as e:
        raise TypesafeError(f"TypeSafe 请求失败：{e}") from None

    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise TypesafeError("TypeSafe 返回了无法解析的响应数据") from None


def evaluate(
    state: dict | list | str,
    questions: dict,
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = TIMEOUT_S,
) -> dict:
    """向 TypeSafe System One 发起一次单请求多问题并行评估（Parallel Fan-Out）。

    Returns:
        {
          "model": "jev-1.13.0",
          "answers": {
            "<question_id>": {
              "type": "choice" | "noul" | "score",
              "choice": ..., "probabilities": ..., "confidence": ...,
              ...
            }
          },
          "usage": {"input_tokens": 120, "output_tokens": 30}
        }
    """
    key = (api_key or "").strip()
    if not key:
        raise TypesafeError("未提供 TypeSafe API Key")
    if not questions:
        raise TypesafeError("问题字典 (questions) 不能为空")

    url = evaluate_url(base_url)
    payload = {
        "state": state,
        "model": model or DEFAULT_MODEL,
        "questions": questions,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Connection", "close")

    data = _send_request(req, timeout)
    if not isinstance(data, dict) or "answers" not in data:
        raise TypesafeError("TypeSafe 返回数据缺少 'answers' 字段")
    return data


def evaluate_dynamics_cascade(
    report,
    derived: dict,
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    dialogue: list[str] | None = None,
    timeout: float = TIMEOUT_S,
    on_progress=None,
) -> tuple[str, dict]:
    """执行两阶段层次化级联动力学判定（Pass 1 宏观拓扑 + Pass 2 微观法医）。

    1. 自动锁定动力学奇点
    2. Pass 1: 向 Jev 发送宏观相空间拓扑判定
    3. Pass 2: 提取核心卡点微观真实证据链，向 Jev 发送微观法医反事实裁决
    4. 结合非平衡相变物理微积分场合成权威级复盘报告

    Returns:
        (markdown_report_text, dynamics_data_dict)
    """
    from tcer.core import llm_prompts

    if on_progress:
        on_progress("正在提取关键里程碑节点…")
    singularities = llm_prompts.detect_phase_singularities(report, derived)

    if on_progress:
        on_progress(f"阶段 1/2：全局形态与关键节点判定（共 {len(singularities)} 个节点）…")
    s1_state, s1_questions = llm_prompts.build_jev_pass1_topology_payload(
        report, derived, dialogue=dialogue, singularities=singularities
    )
    pass1_resp = evaluate(
        state=s1_state,
        questions=s1_questions,
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
    )

    if on_progress:
        on_progress("阶段 2/2：深挖关键转折点成因与反事实推演…")
    s2_state, s2_questions = llm_prompts.build_jev_pass2_autopsy_payload(
        report,
        derived,
        pass1_response=pass1_resp,
        dialogue=dialogue,
        singularities=singularities,
    )

    pass2_resp = None
    if s2_questions:
        try:
            pass2_resp = evaluate(
                state=s2_state,
                questions=s2_questions,
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout=timeout,
            )
        except Exception:
            # 微观法医请求如果遇到边缘网络抖动，优雅降级回退至 Pass 1 结果
            pass2_resp = None

    if on_progress:
        on_progress("正在结合本地遥测合成分析报告…")
    text, dyn_data = llm_prompts.synthesize_authoritative_dynamics_report(
        report,
        derived,
        pass1_response=pass1_resp,
        pass2_response=pass2_resp,
        singularities=singularities,
    )
    return text, dyn_data

