"""TypeSafe Jev (System One) 单元测试：纯 mock urlopen，零真实联网。"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest import mock

import pytest

from tcer.core import llm_prefs, llm_prompts, models, typesafe_client


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# =========================================================================
# 1. typesafe_client 基础请求与 URL 规范化
# =========================================================================

def test_normalize_base_url():
    assert typesafe_client.normalize_base_url("https://api.typesafe.ai") == "https://api.typesafe.ai"
    assert typesafe_client.normalize_base_url("https://api.typesafe.ai/") == "https://api.typesafe.ai"
    assert typesafe_client.normalize_base_url("https://api.typesafe.ai/v1") == "https://api.typesafe.ai"
    assert typesafe_client.normalize_base_url("https://api.typesafe.ai/v1/") == "https://api.typesafe.ai"
    assert typesafe_client.normalize_base_url("") == "https://api.typesafe.ai"


def test_evaluate_request_shape():
    mock_answer = {
        "model": "jev-latest",
        "answers": {
            "convergence_type": {"type": "choice", "choice": "dirac: 收敛", "confidence": 0.95}
        }
    }
    resp_bytes = json.dumps(mock_answer).encode("utf-8")

    with mock.patch("urllib.request.urlopen", return_value=_FakeResp(resp_bytes)) as mock_open:
        res = typesafe_client.evaluate(
            state={"total_turns": 5},
            questions={"convergence_type": {"type": "choice", "criteria": ["dirac", "wandering"]}},
            api_key="ts-secret-key",
            base_url="https://api.typesafe.ai",
            model="jev-latest",
        )

    assert res == mock_answer
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://api.typesafe.ai/v1/systemone"
    assert req.get_method() == "POST"
    assert req.headers["Authorization"] == "Bearer ts-secret-key"
    assert req.headers["Content-type"] == "application/json; charset=utf-8"

    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "jev-latest"
    assert body["state"] == {"total_turns": 5}
    assert "convergence_type" in body["questions"]


def test_evaluate_requires_api_key():
    with pytest.raises(typesafe_client.TypesafeError, match="未提供"):
        typesafe_client.evaluate(state={}, questions={"q": {"type": "choice"}}, api_key="")


def test_evaluate_requires_questions():
    with pytest.raises(typesafe_client.TypesafeError, match="不能为空"):
        typesafe_client.evaluate(state={}, questions={}, api_key="test-key")


# =========================================================================
# 2. HTTP 错误码细分转译
# =========================================================================

def test_evaluate_http_401():
    err = urllib.error.HTTPError(
        url="https://api.typesafe.ai/v1/systemone",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Invalid API key"}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(typesafe_client.TypesafeError, match="401"):
            typesafe_client.evaluate(state={}, questions={"q": {"type": "choice"}}, api_key="bad-key")


def test_evaluate_http_422():
    err = urllib.error.HTTPError(
        url="https://api.typesafe.ai/v1/systemone",
        code=422,
        msg="Unprocessable Entity",
        hdrs={},
        fp=io.BytesIO(b'{"detail": [{"loc": ["body", "questions"], "msg": "field required"}]}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(typesafe_client.TypesafeError, match="422"):
            typesafe_client.evaluate(state={}, questions={"q": {"type": "choice"}}, api_key="key")


def test_evaluate_http_429():
    err = urllib.error.HTTPError(
        url="https://api.typesafe.ai/v1/systemone",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Rate limit exceeded"}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(typesafe_client.TypesafeError, match="429"):
            typesafe_client.evaluate(state={}, questions={"q": {"type": "choice"}}, api_key="key")


# =========================================================================
# 3. llm_prefs TypeSafe 配置（显式填写，刻意不提供本机凭据检测）
# =========================================================================

def test_llm_prefs_typesafe_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_prefs, "_prefs_path", lambda: tmp_path / "tcer_llm.json")

    # 初始默认值
    assert llm_prefs.typesafe_enabled() is False
    assert llm_prefs.typesafe_base_url() == "https://api.typesafe.ai"
    assert llm_prefs.typesafe_model() == "jev-latest"

    # 保存配置
    llm_prefs.save({
        "typesafe_key": "ts-my-secret",
        "typesafe_base_url": "https://custom.typesafe.ai",
        "typesafe_model": "jev-preview",
    })

    assert llm_prefs.typesafe_enabled() is True
    assert llm_prefs.typesafe_api_key() == "ts-my-secret"
    assert llm_prefs.typesafe_base_url() == "https://custom.typesafe.ai"
    assert llm_prefs.typesafe_model() == "jev-preview"

    # 刻意不存在本机凭据检测入口（用户约定：不从本机环境/其他工具配置注入）
    assert not hasattr(llm_prefs, "detect_local_typesafe_key")


# =========================================================================
# 4. 相空间问题构建与综合动力学数据合成
# =========================================================================

def _make_dummy_report():
    u = models.TokenUsage(assistant_msgs=8, user_msgs=3, input_tokens=10000, output_tokens=2500, tool_calls=10, tool_errors=1)
    u.turn_stats = [
        models.TurnStat(turn=0, user_turn=1, input_tokens=1000, output_tokens=200, duration_ms=2000, tool_calls=2, errors=0),
        models.TurnStat(turn=1, user_turn=1, input_tokens=1500, output_tokens=300, duration_ms=2500, tool_calls=2, errors=1),
        models.TurnStat(turn=4, user_turn=2, input_tokens=2500, output_tokens=500, duration_ms=3000, tool_calls=3, errors=0),
        models.TurnStat(turn=7, user_turn=3, input_tokens=4000, output_tokens=800, duration_ms=4500, tool_calls=3, errors=0),
    ]

    class DummyReport:
        usage = u
        meta = models.SessionMeta('dummy-sid', 'dummy-proj', 1000, 2000, False)
        net_loc = 150
        code_reworked = 20
        files_touched_details = {}
        subagent_density = 0.0
        cost = 0.15

    return DummyReport()


def test_build_jev_dynamics_payload():
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    state, questions = llm_prompts.build_jev_dynamics_payload(report, derived)

    # 校验解耦四块契约（意图 / 终态交付 / 资源耗散 / 控制时序）；
    # 里程碑只在 control_sequence 序列化一次——不再有 session_summary /
    # phase_singularities / milestone_turns 冗余副本
    assert set(state.keys()) == {
        "intent_specification", "terminal_deliverable",
        "thermodynamic_dissipation", "control_sequence",
    }
    assert state["terminal_deliverable"]["total_turns"] == 4
    assert state["terminal_deliverable"]["final_net_loc"] == 150
    assert len(state["control_sequence"]) == 4
    for node in state["control_sequence"]:
        assert node["user_turn"] in (1, 2, 3)

    # 校验全局 questions
    for k in ("convergence_type", "primary_bottleneck", "barrier_crossed", "attractor_trapped",
              "intent_formalization", "drift_sensitivity", "feedback_mutual_info", "epistemic_balance"):
        assert k in questions

    # 校验里程碑 questions 包含 regime_t*, trigger_t*, vector_t*, event_t*, distance_t*
    assert "regime_t1" in questions
    assert "trigger_t1" in questions
    assert "vector_t1" in questions
    assert "event_t1" in questions
    assert "distance_t1" in questions
    # 问题瘦身（#8）：「先查后改」由本地确定性推导（debt_local），不再发 debt_t{n}
    assert not any(k.startswith("debt_") for k in questions)
    # 转折直接指认（#3）：候选 = 里程碑回合号 + none
    assert "turnaround_pick" in questions
    pick_keys = set(questions["turnaround_pick"]["criteria"])
    assert {"t1", "none"} <= pick_keys
    # 证据供给（#2）：终局交付证据 + 本地调查风格
    assert "final_actions" in state["terminal_deliverable"]
    assert "verification_performed" in state["terminal_deliverable"]
    assert "exit_clean" in state["terminal_deliverable"]
    for node in state["control_sequence"]:
        assert node["investigation_style"] in ("prudent", "reckless", "neutral")

    # 校验 TypeSafe API Schema 契约：
    # choice 题型的 criteria 必须为 dict（选项键 -> 描述）；
    # noul 题型的 criteria 必须为 dict (含 true/false) 或缺省；
    # score 题型的 criteria 必须为 list（等级描述列表）
    for q_id, q in questions.items():
        q_type = q["type"]
        assert "instructions" in q
        if q_type == "choice":
            assert isinstance(q.get("criteria"), dict), f"{q_id} choice criteria must be dict"
            assert len(q["criteria"]) >= 2
        elif q_type == "score":
            assert isinstance(q.get("criteria"), list), f"{q_id} score criteria must be list"
            assert len(q["criteria"]) >= 2
        elif q_type == "noul":
            if "criteria" in q:
                assert isinstance(q["criteria"], dict), f"{q_id} noul criteria must be dict"
                assert "true" in q["criteria"] and "false" in q["criteria"]


def test_synthesize_jev_dynamics_data():
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    state, questions = llm_prompts.build_jev_dynamics_payload(report, derived)

    # 模拟 Jev 判定响应（含后验概率分布）
    mock_resp = {
        "model": "jev-latest",
        "answers": {
            "convergence_type": {
                "type": "choice",
                "choice": "dirac: 狄拉克目标快速收敛",
                "confidence": 0.95,
                "probabilities": {"dirac": 0.82, "escaped": 0.12, "trapped": 0.06},
            },
            "primary_bottleneck": {"type": "choice", "choice": "none: 全流程平稳顺畅", "confidence": 0.9},
            "barrier_crossed": {"type": "noul", "noul": 0.92},
            "attractor_trapped": {"type": "noul", "noul": 0.08},
            "intent_entropy": {"type": "choice", "choice": "low: 初始意图清晰完整", "confidence": 0.9},
            "intent_formalization": {"type": "score", "score": 3.8, "confidence": 0.9},
            "drift_sensitivity": {"type": "score", "score": 3.2, "confidence": 0.85},
            "feedback_mutual_info": {"type": "score", "score": 4.0, "confidence": 0.95},
            "epistemic_balance": {"type": "score", "score": 3.0, "confidence": 0.8},
        }
    }
    for k in questions:
        if k.startswith("regime_"):
            mock_resp["answers"][k] = {"type": "choice", "choice": "liquid: 灵活探索与演化", "confidence": 0.9}
        elif k.startswith("trigger_"):
            mock_resp["answers"][k] = {"type": "choice", "choice": "ai: 模型自主推进", "confidence": 0.9}
        elif k.startswith("vector_"):
            mock_resp["answers"][k] = {"type": "choice", "choice": "positive: 建设性收敛推进", "confidence": 0.9}
        elif k.startswith("event_"):
            mock_resp["answers"][k] = {"type": "choice", "choice": "barrier_leap: 跨越鞍点势垒", "confidence": 0.92}
        elif k.startswith("debt_"):
            mock_resp["answers"][k] = {"type": "choice", "choice": "prudent: 充分探查克制修改", "confidence": 0.88}
        elif k.startswith("distance_"):
            mock_resp["answers"][k] = {"type": "score", "score": 1.2, "confidence": 0.9}

    text, dyn_data = llm_prompts.synthesize_jev_dynamics_data(report, derived, mock_resp)

    # 校验动力学数据与 test_dynamics_theory 口径完全吻合
    assert dyn_data["convergence_type"] == "dirac"
    assert dyn_data["barrier_crossed"] is True
    assert dyn_data["attractor_trapped"] is False
    assert isinstance(dyn_data["damping_ratio"], float)
    assert isinstance(dyn_data["carnot_efficiency"], float)

    # 校验四维能力评级已归一化到 0..100
    caps = dyn_data["capabilities"]
    assert len(caps) == 4
    for val in caps.values():
        assert 0 <= val <= 100

    # 校验轨迹与相空间物理场
    traj = dyn_data["trajectory"]
    assert len(traj) == 4
    for pt in traj:
        assert 0.0 <= pt["semantic_distance"] <= 1.0
        assert "potential_energy" in pt
        assert "epistemic_debt" in pt
        assert pt["regime"] in ("gas", "liquid", "glass", "crystal")
        assert pt["trigger"] in ("user", "ai", "env", "none")
        assert pt["vector"] in ("positive", "neutral", "negative")
        assert 0.0 <= pt["snr"] <= 1.0

    # 校验文本自包含 JSON 可由 parse_dynamics_payload 解析
    parsed_text, parsed_data = llm_prompts.parse_dynamics_payload(text, derived)
    assert parsed_data["convergence_type"] == "dirac"
    assert len(parsed_data["trajectory"]) == 4


# =========================================================================
# 5. 层次化两阶段级联架构与微观法医单测
# =========================================================================

def test_detect_phase_singularities():
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    singularities = llm_prompts.detect_phase_singularities(report, derived)
    assert len(singularities) >= 3
    turns = [s["turn"] for s in singularities]
    assert turns == sorted(turns)
    kinds = [s["kind"] for s in singularities]
    assert "init" in kinds
    assert "crystal" in kinds


def test_jev_turnaround_pick_priority_and_tension_and_blame():
    """#3 转折指认优先 + #4 交叉校验张力 + #5 低置信标记 + #6 责任占比。"""
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    singularities = llm_prompts.detect_phase_singularities(report, derived)
    turns = [s["turn"] for s in singularities]

    p1 = {"answers": {
        "convergence_type": {"type": "choice", "choice": "trapped", "confidence": 0.55,
                             "probabilities": {"trapped": 0.55, "escaped": 0.35}},
        "primary_bottleneck": {"type": "choice", "choice": "none", "confidence": 0.9},
        "barrier_crossed": {"type": "noul", "noul": 0.5},
        "attractor_trapped": {"type": "noul", "noul": 0.9},
        "intent_entropy": {"type": "choice", "choice": "low", "confidence": 0.9},
        "intent_formalization": {"type": "score", "score": 3.0, "confidence": 0.9},
        "drift_sensitivity": {"type": "score", "score": 3.0, "confidence": 0.9},
        "feedback_mutual_info": {"type": "score", "score": 3.0, "confidence": 0.9},
        "epistemic_balance": {"type": "score", "score": 3.0, "confidence": 0.9},
        "turnaround_pick": {"type": "choice", "choice": f"t{turns[1]}",
                            "confidence": 0.9},
        f"trigger_t{turns[1]}": {"type": "choice", "choice": "user", "confidence": 0.9},
    }}
    for t in turns:
        p1["answers"].setdefault(f"regime_t{t}", {"type": "choice", "choice": "liquid", "confidence": 0.9})
        p1["answers"].setdefault(f"trigger_t{t}", {"type": "choice", "choice": "ai", "confidence": 0.9})
        p1["answers"].setdefault(f"vector_t{t}", {"type": "choice", "choice": "positive", "confidence": 0.9})
        p1["answers"].setdefault(f"event_t{t}", {"type": "choice", "choice": "normal", "confidence": 0.9})
        p1["answers"].setdefault(f"distance_t{t}", {"type": "score", "score": 2.0, "confidence": 0.9})

    p2 = {"answers": {
        "crit_causal_attribution": {"type": "choice", "choice": "specification_gap", "confidence": 0.9},
        "crit_counterfactual_preventable": {"type": "noul", "noul": 0.7},
        "crit_waterbed_breakage": {"type": "noul", "noul": 0.2},
        "crit_cognitive_overload": {"type": "score", "score": 1.0, "confidence": 0.9},
        "blame_ai": {"type": "score", "score": 3.0, "confidence": 0.9},
        "blame_user": {"type": "score", "score": 1.0, "confidence": 0.9},
        "blame_env": {"type": "score", "score": 0.0, "confidence": 0.9},
        "prescriptive_action": {"type": "choice", "choice": "pin_test_anchor", "confidence": 0.9},
    }}

    text, dyn = llm_prompts.synthesize_authoritative_dynamics_report(
        report, derived, p1, p2, singularities)

    # #3：Jev 指认的转折点优先（turns[1] 不是 barrier 候选也生效）
    assert dyn["turnaround_turn"] == turns[1]
    assert "用户的指令或纠偏" in text
    # #4：trapped 判定 vs 本地事实（净增 150 行 + 末回合干净）→ 张力警示
    assert dyn["evidence_tension"], "trapped + 净增产出的矛盾应产生张力警示"
    assert "判定与客观证据的张力" in text
    # #5：主概率 0.55 < 0.6 → 低置信标记与升级建议
    assert dyn["low_confidence"] is True
    assert "判定不确定性较高" in text
    # #6：责任占比归一化（3:1:0 → 75%/25%/0%）
    b = dyn["autopsy"]["blame"]
    assert b is not None
    assert abs(b["ai"] - 0.75) < 1e-6 and abs(b["user"] - 0.25) < 1e-6
    assert "责任占比" in text


def test_jev_report_audit_zero_warnings():
    """回归：Jev 级联报告为本地确定性合成章节，audit_warnings 按其实际标题
    校验必备节——此前沿用 general 的「速读摘要/关键转折」子集导致每份 Jev
    报告都误报「缺少必备小节」警示。"""
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    singularities = llm_prompts.detect_phase_singularities(report, derived)

    p1 = {"answers": {
        "convergence_type": {"type": "choice", "choice": "escaped: 扰动逃逸",
                             "confidence": 0.92, "probabilities": {"escaped": 0.8}},
        "primary_bottleneck": {"type": "choice", "choice": "retry_loop", "confidence": 0.88},
        "barrier_crossed": {"type": "noul", "noul": 0.9},
        "attractor_trapped": {"type": "noul", "noul": 0.1},
        "intent_entropy": {"type": "choice", "choice": "low", "confidence": 0.9},
        "intent_formalization": {"type": "score", "score": 3.5, "confidence": 0.9},
        "drift_sensitivity": {"type": "score", "score": 3.0, "confidence": 0.85},
        "feedback_mutual_info": {"type": "score", "score": 4.0, "confidence": 0.95},
        "epistemic_balance": {"type": "score", "score": 3.8, "confidence": 0.9},
    }}
    for s in singularities:
        t = s["turn"]
        p1["answers"][f"regime_t{t}"] = {"type": "choice", "choice": "liquid", "confidence": 0.9}
        p1["answers"][f"trigger_t{t}"] = {"type": "choice", "choice": "ai", "confidence": 0.9}
        p1["answers"][f"vector_t{t}"] = {"type": "choice", "choice": "positive", "confidence": 0.9}
        p1["answers"][f"event_t{t}"] = {"type": "choice", "choice": "normal", "confidence": 0.9}
        p1["answers"][f"debt_t{t}"] = {"type": "choice", "choice": "prudent", "confidence": 0.9}
        p1["answers"][f"distance_t{t}"] = {"type": "score", "score": 1.2, "confidence": 0.9}

    text, _dyn = llm_prompts.synthesize_authoritative_dynamics_report(
        report, derived, p1, None, singularities)
    assert llm_prompts.audit_warnings(text, is_dynamics=True, provider="typesafe") == []


def test_jev_pass2_evidence_from_derived_not_dialogue_prefix():
    """回归：Pass 2 证据必须来自 derived 确定性遥测（ops_by_turn 邻域）——
    dialogue 行只有 [用户]/[AI]/[工具] 前缀、无 [T{n}] 标号，按回合前缀匹配
    恒落空，曾恒用 dialogue[:8]（会话开头）冒充转折点证据。"""
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    singularities = llm_prompts.detect_phase_singularities(report, derived)
    # 构造带 [用户] 前缀、但不含任何回合标号的 dialogue（真实 read_dialogue 格式）
    dialogue = [
        "[用户] 帮我实现一个 CLI 工具",
        "[AI] 好的，我先看一下项目结构。",
        "[工具] Read src/main.py",
    ]
    state, _q = llm_prompts.build_jev_pass2_autopsy_payload(
        report, derived,
        pass1_response={"answers": {}},
        dialogue=dialogue,
        singularities=singularities,
    )
    ev = state["critical_singularity"]["evidence_summary"]
    # 用户消息摘录须显式标注「非回合绑定」（NOT turn-bound），
    # 不再伪装成转折回合证据；证据标签为英文（Jev 判定题面语言）
    user_lines = [ln for ln in ev if ln.startswith("User message excerpt")]
    assert user_lines and all("NOT turn-bound" in ln for ln in user_lines)
    # 无 ops 邻域证据时至少有回合指标兜底（dummy 会话无 tool_ops）
    assert ev, "evidence_summary 不应为空"


def test_build_jev_pass2_autopsy_payload():
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)
    singularities = llm_prompts.detect_phase_singularities(report, derived)

    # 构造 Pass 1 响应（模拟发现初次分岔与瓶颈）
    pass1_resp = {
        "model": "jev-latest",
        "answers": {
            "convergence_type": {"choice": "escaped"},
            "primary_bottleneck": {"choice": "retry_loop"},
            "barrier_crossed": {"noul": 0.85},
            "attractor_trapped": {"noul": 0.15},
        }
    }
    state, questions = llm_prompts.build_jev_pass2_autopsy_payload(
        report, derived, pass1_response=pass1_resp, singularities=singularities
    )
    assert "critical_singularity" in state
    assert "evidence_summary" in state["critical_singularity"]
    assert "crit_causal_attribution" in questions
    assert "crit_counterfactual_preventable" in questions
    assert "crit_waterbed_breakage" in questions
    assert "crit_cognitive_overload" in questions
    assert "prescriptive_action" in questions


def test_evaluate_dynamics_cascade_end_to_end():
    report = _make_dummy_report()
    derived = llm_prompts.build_llm_derived(report)

    # 模拟两阶段响应
    pass1_data = {
        "model": "jev-latest",
        "answers": {
            "convergence_type": {
                "type": "choice",
                "choice": "escaped: 扰动逃逸",
                "confidence": 0.92,
                "probabilities": {"dirac": 0.1, "escaped": 0.8, "trapped": 0.1},
            },
            "primary_bottleneck": {"type": "choice", "choice": "retry_loop: 报错重试死锁", "confidence": 0.88},
            "barrier_crossed": {"type": "noul", "noul": 0.9},
            "attractor_trapped": {"type": "noul", "noul": 0.1},
            "intent_formalization": {"type": "score", "score": 3.5, "confidence": 0.9},
            "drift_sensitivity": {"type": "score", "score": 3.0, "confidence": 0.85},
            "feedback_mutual_info": {"type": "score", "score": 4.2, "confidence": 0.95},
            "epistemic_balance": {"type": "score", "score": 3.8, "confidence": 0.9},
        }
    }
    pass2_data = {
        "model": "jev-latest",
        "answers": {
            "crit_causal_attribution": {
                "type": "choice",
                "choice": "cognitive_gap: 提示词语义偏离认知基准",
                "confidence": 0.94,
            },
            "crit_counterfactual_preventable": {"type": "noul", "noul": 0.85},
            "crit_waterbed_breakage": {"type": "noul", "noul": 0.12},
            "crit_cognitive_overload": {"type": "noul", "noul": 0.65},
            "prescriptive_action": {
                "type": "choice",
                "choice": "isolate_interface: 固化接口类型定义与测试夹具",
                "confidence": 0.91,
            },
        }
    }

    call_count = 0
    def _mock_urlopen(req, timeout=10.0):
        nonlocal call_count
        call_count += 1
        body_str = req.data.decode("utf-8")
        body = json.loads(body_str)
        if "crit_causal_attribution" in body.get("questions", {}):
            return _FakeResp(json.dumps(pass2_data).encode("utf-8"))
        return _FakeResp(json.dumps(pass1_data).encode("utf-8"))

    progress_msgs = []
    with mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        text, dyn_data = typesafe_client.evaluate_dynamics_cascade(
            report,
            derived,
            api_key="ts-test-key",
            base_url="https://api.typesafe.ai",
            model="jev-latest",
            on_progress=lambda msg: progress_msgs.append(msg),
        )

    assert call_count == 2
    assert len(progress_msgs) >= 3
    assert dyn_data["convergence_type"] == "escaped"
    assert "关键转折点深挖" in text
    assert "反事实验证" in text
    assert "85.0%" in text
    assert "连带破坏" in text

