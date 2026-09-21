"""TypeSafe AI (System One / Jev) 客户端（纯 urllib，零第三方依赖）。

端点：``POST {base_url}/v1/systemone``
文档参考：https://docs.typesafe.ai/api
专职于 System One 判定模型（如 jev-latest / jev-preview），接收 state 与强类型
questions 映射，返回带校准概率与置信度（confidence）的类型化决策。

判定缓存（doc/jev-research.md 方案 D）：按 (base_url, model, state, questions)
内容哈希把响应落盘到 prefs 目录，跨进程重启复用。三个动机：
1. 省钱——同一会话重放/重新生成报告不重复计费（输出免费、输入按量）；
2. 可复现——Jev 官方实测逐问题采样噪声 σ≈0.01（consistency cookbook），缓存后
   同输入恒同输出，与 TCER 的 audit 可对账精神一致；
3. 取舍说明——缓存键用调用方传入的 model 字符串：钉版本 ID（jev-1.13.0）时
   天然随版本隔离；用别名（jev-latest）时发版后沿用旧判定，动力学判定对
   小版本更替不敏感，接受该取舍换取更高命中。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
TIMEOUT_S = 25.0

_CACHE_FILENAME = "tcer_typesafe_cache.json"
_CACHE_MAX_ENTRIES = 128
_cache_lock = threading.Lock()
_cache_store: dict[str, dict] = {}
_cache_loaded = False

# 实际计费记账（方案 F）：只记真实网络调用的 usage，缓存命中不计（没花钱）。
# opt-in 付费功能必须让用户看得见账单（jev-research §8：按「每解决一个任务」算账）。
_USAGE_FILENAME = "tcer_typesafe_usage.json"
_USAGE_PRICE_PER_MTOK = 0.042  # 官方输入单价（$0.042/MTok，输出免费）


def _usage_path() -> Path:
    from tcer.core import app_dirs

    return app_dirs.prefs_dir() / _USAGE_FILENAME


def _record_usage(data: dict) -> None:
    """真实调用成功后累加 usage 并落盘；任何 IO 失败静默（记账是观测不是依赖）。"""
    try:
        usage = data.get("usage") or {}
        with _cache_lock:
            _load_usage()
            _usage_state["requests"] += 1
            _usage_state["input_tokens"] += int(usage.get("input_tokens") or 0)
            _usage_state["output_tokens"] += int(usage.get("output_tokens") or 0)
            path = _usage_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(_usage_state, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        pass


def usage_stats() -> dict:
    """返回累计计费口径（惰性加载磁盘）。"""
    global _usage_state
    with _cache_lock:
        _load_usage()
        return dict(_usage_state)


def usage_cost_usd(stats: dict | None = None) -> float:
    """按官方输入单价折算美元成本（输出免费；缓存命中不计，见 _record_usage）。"""
    s = stats if stats is not None else usage_stats()
    return s.get("input_tokens", 0) / 1_000_000 * _USAGE_PRICE_PER_MTOK


def _load_usage() -> None:
    global _usage_loaded
    if _usage_loaded:
        return
    _usage_loaded = True
    try:
        data = json.loads(_usage_path().read_bytes().decode("utf-8"))
        if isinstance(data, dict):
            _usage_state["requests"] = int(data.get("requests") or 0)
            _usage_state["input_tokens"] = int(data.get("input_tokens") or 0)
            _usage_state["output_tokens"] = int(data.get("output_tokens") or 0)
    except Exception:
        pass


_usage_state: dict[str, int] = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
_usage_loaded = False


class TypesafeError(Exception):
    """人类可读的 TypeSafe API 调用失败（网络/鉴权/参数/限流）。"""


# ---------------------------------------------------------------------------
# 判定缓存（方案 D）：磁盘 JSON，原子写，任何 IO 失败静默降级为无缓存。
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    from tcer.core import app_dirs

    return app_dirs.prefs_dir() / _CACHE_FILENAME


def _cache_load() -> dict[str, dict]:
    """惰性加载磁盘缓存一次；损坏/缺失时静默返回空表。"""
    global _cache_loaded
    if _cache_loaded:
        return _cache_store
    _cache_loaded = True
    try:
        data = json.loads(_cache_path().read_bytes().decode("utf-8"))
    except Exception:
        return _cache_store
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, dict) and "resp" in v:
                _cache_store[k] = v
    return _cache_store


def _cache_save() -> None:
    """原子落盘（tmp + os.replace，Windows 下替换已关闭文件安全）。"""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        items = dict(list(_cache_store.items())[-_CACHE_MAX_ENTRIES:])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _cache_key(base_url: str, model: str, state, questions) -> str:
    payload = json.dumps(
        {"u": base_url, "m": model, "s": state, "q": questions},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_clear() -> None:
    """清空判定缓存（测试 / 强制重新判定）。"""
    global _cache_loaded
    with _cache_lock:
        _cache_store.clear()
        _cache_loaded = False
    try:
        _cache_path().unlink(missing_ok=True)
    except Exception:
        pass


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
    use_cache: bool = True,
) -> dict:
    """向 TypeSafe System One 发起一次单请求多问题并行评估（Parallel Fan-Out）。

    Args:
        use_cache: 命中内容哈希缓存时直接返回（不联网）。API Key 不参与缓存键。

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

    cache_key = None
    if use_cache:
        cache_key = _cache_key(base_url, model or DEFAULT_MODEL, state, questions)
        with _cache_lock:
            hit = _cache_load().get(cache_key)
        if hit is not None:
            # 深拷贝：调用方突变返回值不得污染缓存
            return copy.deepcopy(hit["resp"])

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

    _record_usage(data)  # 只记真实网络调用；缓存命中路径在上面提前返回

    if cache_key is not None:
        with _cache_lock:
            _cache_load()[cache_key] = {"resp": copy.deepcopy(data),
                                        "cached_at": int(time.time())}
            _cache_save()
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

