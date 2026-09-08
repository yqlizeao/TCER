"""相空间收敛动力学五大前沿理论具象化单测与确定性护栏。

测试覆盖：
  1. 理论一：Waddington 双阱势能曲面方程与极值点
  2. 理论二：变分自由能与认知负债比
  3. 理论三：闭环控制论阻尼比与水床效应检测
  4. 理论四：统计物理隐式切换动力系统 (SLDS) 4 相态映射
  5. 理论五：计算热力学朗道尔信息擦除耗散功与卡诺效率
  6. 遥测协议兼容性与确定性后备推导
  7. GUI 相空间相图视口冒烟测试 (无头模式)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tcer.core import metrics
from tcer.core.models import SessionMeta, TokenUsage, TurnStat
from tcer.core.llm_prompts import (
    DYNAMICS_PROMPT_VERSION,
    _DYNAMICS_SYSTEM,
    ground_dynamics_user_turns,
    parse_dynamics_payload,
)

tk = pytest.importorskip("tkinter")


# ============================================================
# 1. Waddington 双阱势能曲面方程测试
# ============================================================

def test_waddington_potential_extrema():
    # 极值点：狄拉克目标点 (Ds=0.05)、平庸吸引盆 (Ds=0.86)、鞍点势垒 (Ds=0.55)
    v_dirac = metrics.compute_waddington_potential(0.05)
    v_trap = metrics.compute_waddington_potential(0.86)
    v_barrier = metrics.compute_waddington_potential(0.55)

    # 鞍点势垒严格高于两侧势阱
    assert v_barrier > v_dirac, f"Barrier ({v_barrier}) should be higher than Dirac ({v_dirac})"
    assert v_barrier > v_trap, f"Barrier ({v_barrier}) should be higher than Trap ({v_trap})"
    # 狄拉克目标深阱比平庸陷阱更深（全局极小值）
    assert v_dirac < v_trap, f"Dirac well ({v_dirac}) should be deeper than Trap well ({v_trap})"

    # 边界约束 [-1.2, 0.8]
    assert -1.2 <= v_dirac <= 0.8
    assert -1.2 <= v_trap <= 0.8
    assert -1.2 <= v_barrier <= 0.8

    # 极大成本输入时仍被上界钳位
    v_high_cost = metrics.compute_waddington_potential(0.55, cost_fraction=50.0)
    assert v_high_cost == 0.8, f"Expected clamped 0.8, got {v_high_cost}"

    # 异常输入容错 (负值与超出 1.0 输入)
    v_neg = metrics.compute_waddington_potential(-0.5)
    v_zero = metrics.compute_waddington_potential(0.0)
    assert isinstance(v_neg, float)
    assert v_neg == v_zero


# ============================================================
# 2. 变分自由能与认知负债比测试
# ============================================================

def test_epistemic_debt_boundaries():
    # 零输入
    assert metrics.compute_epistemic_debt(0, 0, 0) == 0.0

    # 纯探查：负债为 0
    assert metrics.compute_epistemic_debt(500, 0, 10) == 0.0

    # 临界阈值 1.0 (写 100 行，读 100 行)
    ed_balanced = metrics.compute_epistemic_debt(100, 100, 0)
    assert ed_balanced == 1.0

    # Grep 权重测试 (1 次 Grep 相当于 20 行读感知)
    ed_grep = metrics.compute_epistemic_debt(20, 60, grep_ops=2)
    # J_epistemic = 20 + 2 * 20 = 60, J_pragmatic = 60 -> Ed = 1.0
    assert ed_grep == 1.0

    # 认知盲区动刀 (Ed >= 4.0)
    ed_danger = metrics.compute_epistemic_debt(10, 200, grep_ops=0)
    assert ed_danger >= 4.0

    # 单调性：修改行增加，Ed 严格单调递增
    ed1 = metrics.compute_epistemic_debt(100, 50, 0)
    ed2 = metrics.compute_epistemic_debt(100, 150, 0)
    assert ed2 > ed1


# ============================================================
# 3. 闭环控制论阻尼比与水床效应测试
# ============================================================

def test_cybernetic_damping():
    # 1. 单调向心收敛轨迹 -> 临界阻尼 (0.70 <= zeta <= 1.10)
    convergent_traj = [
        {"turn": 1, "semantic_distance": 0.85},
        {"turn": 2, "semantic_distance": 0.60},
        {"turn": 3, "semantic_distance": 0.35},
        {"turn": 4, "semantic_distance": 0.10},
    ]
    zeta, cat_key, cat_cn = metrics.compute_cybernetic_damping(convergent_traj)
    assert 0.70 <= zeta <= 1.10
    assert cat_key == "critical"
    assert cat_cn == "临界收敛"

    # 2. 反复震荡颠簸轨迹 -> 欠阻尼震荡 (zeta < 0.70)
    oscillating_traj = [
        {"turn": 1, "semantic_distance": 0.80},
        {"turn": 2, "semantic_distance": 0.40},
        {"turn": 3, "semantic_distance": 0.75},
        {"turn": 4, "semantic_distance": 0.30},
        {"turn": 5, "semantic_distance": 0.70},
        {"turn": 6, "semantic_distance": 0.25},
    ]
    zeta_osc, cat_key_osc, cat_cn_osc = metrics.compute_cybernetic_damping(oscillating_traj)
    assert zeta_osc < 0.70
    assert cat_key_osc == "underdamped"
    assert cat_cn_osc == "欠阻尼震荡"

    # 3. 停滞/微弱漂移轨迹 -> 过阻尼迟缓 (zeta > 1.10)
    stagnant_traj = [
        {"turn": 1, "semantic_distance": 0.820},
        {"turn": 2, "semantic_distance": 0.825},
        {"turn": 3, "semantic_distance": 0.822},
        {"turn": 4, "semantic_distance": 0.828},
    ]
    zeta_stag, cat_key_stag, cat_cn_stag = metrics.compute_cybernetic_damping(stagnant_traj)
    # 3b. 单向远离目标漂移 (无反转，严重迟缓离心) -> 过阻尼迟缓 (zeta = 1.40)
    drift_traj = [
        {"turn": 1, "semantic_distance": 0.65},
        {"turn": 2, "semantic_distance": 0.70},
        {"turn": 3, "semantic_distance": 0.75},
        {"turn": 4, "semantic_distance": 0.80},
    ]
    zeta_drift, cat_key_drift, cat_cn_drift = metrics.compute_cybernetic_damping(drift_traj)
    assert zeta_drift == 1.40
    assert cat_key_drift == "overdamped"
    assert cat_cn_drift == "过阻尼迟缓"

    assert zeta_stag > 1.10
    assert cat_key_stag == "overdamped"
    assert cat_cn_stag == "过阻尼迟缓"

    # 4. 边界容错
    assert metrics.compute_cybernetic_damping([]) == (1.0, "critical", "临界收敛")
    assert metrics.compute_cybernetic_damping([{"semantic_distance": 0.5}]) == (1.0, "critical", "临界收敛")


def test_waterbed_events_detection():
    # 正常推进紧接着测试报错 -> 触发水床效应 (修复 A 导致 B 破损)
    traj_with_waterbed = [
        {"turn": 1, "vector": "positive", "semantic_distance": 0.85},
        {"turn": 2, "vector": "positive", "semantic_distance": 0.50},
        {"turn": 3, "event": "test_fail", "semantic_distance": 0.52},
        {"turn": 4, "vector": "positive", "semantic_distance": 0.30},
    ]
    wb_turns = metrics.detect_waterbed_events(traj_with_waterbed)
    assert wb_turns == [3]

    # 若前一轮并非正向推进，则不标记为水床效应
    traj_no_waterbed = [
        {"turn": 1, "vector": "negative", "semantic_distance": 0.70},
        {"turn": 2, "event": "test_fail", "semantic_distance": 0.75},
    ]
    assert metrics.detect_waterbed_events(traj_no_waterbed) == []


# ============================================================
# 4. 统计物理隐式切换动力系统 (SLDS) 4 相态映射测试
# ============================================================

def test_phase_regime_inference():
    # 1. 晶态终态 (Crystal)
    assert metrics.infer_phase_regime(0.12, "positive", "normal", 1.0) == "crystal"
    assert metrics.infer_phase_regime(0.60, "positive", "breakthrough", 1.0) == "crystal"
    assert metrics.infer_phase_regime(0.40, "positive", "dirac", 0.8) == "crystal"

    # 2. 玻璃态死锁 (Glass)
    assert metrics.infer_phase_regime(0.65, "negative", "retry_loop", 1.0) == "glass"
    assert metrics.infer_phase_regime(0.55, "negative", "test_fail", 1.0) == "glass"
    assert metrics.infer_phase_regime(0.55, "positive", "normal", 4.5) == "glass"

    # 3. 高熵气态 (Gas)
    assert metrics.infer_phase_regime(0.80, "neutral", "explore", 0.2) == "gas"
    assert metrics.infer_phase_regime(0.75, "neutral", "normal", 0.3) == "gas"

    # 4. 凝聚液态 (Liquid)
    assert metrics.infer_phase_regime(0.50, "positive", "normal", 1.5) == "liquid"
    assert metrics.infer_phase_regime(0.45, "convergent", "normal", 2.0) == "liquid"


# ============================================================
# 5. 朗道尔信息擦除耗散与广义卡诺计算效率测试
# ============================================================

def test_landauer_dissipation_and_carnot():
    # 1. 高效创作场景 (低返工、无压缩)
    q_ratio, eta = metrics.compute_landauer_dissipation(
        net_loc=500, rework_loc=20, compaction_tokens=0, total_tokens=100_000
    )
    assert 0.0 <= q_ratio <= 1.0
    assert 0.0 <= eta <= 1.0
    assert q_ratio < 0.10  # 擦除耗散极小
    assert eta > 0.75      # 卡诺效率极高

    # 2. 极端死锁耗散场景 (零净产、海量返工与压缩)
    q_ratio_bad, eta_bad = metrics.compute_landauer_dissipation(
        net_loc=0, rework_loc=500, compaction_tokens=80_000, total_tokens=200_000
    )
    assert q_ratio_bad == 1.0  # 100% 耗散
    assert eta_bad == 0.0      # 0% 有效卡诺功

    # 3. 极值范围约束
    for net, rew, comp, tok in [
        (0, 0, 0, 0),
        (10, 10, 800, 1000),
        (10_000, 50_000, 500_000, 5_000_000),
    ]:
        qr, et = metrics.compute_landauer_dissipation(net, rew, comp, tok)
        assert 0.0 <= qr <= 1.0
        assert 0.0 <= et <= 1.0


def test_damping_spectrum_sliding_window():
    # 模拟复合会话：前期向心收敛 -> 中期反复横跳失控 -> 后期逃逸收敛
    traj = [
        {"turn": 1, "semantic_distance": 0.85},
        {"turn": 2, "semantic_distance": 0.70},
        {"turn": 3, "semantic_distance": 0.55},
        # 震荡区间 (欠阻尼)
        {"turn": 4, "semantic_distance": 0.65},
        {"turn": 5, "semantic_distance": 0.50},
        {"turn": 6, "semantic_distance": 0.68},
        {"turn": 7, "semantic_distance": 0.45},
        # 最终平稳收敛
        {"turn": 8, "semantic_distance": 0.25},
        {"turn": 9, "semantic_distance": 0.10},
    ]

    spec = metrics.compute_damping_spectrum(traj, window_size=4)
    assert len(spec["turn_damping"]) == 9
    assert "bifurcations" in spec
    # 应当检测到失控分岔点
    assert any(b["to_cat"] == "underdamped" for b in spec["bifurcations"])
    assert 0.0 < spec["instability_share"] < 1.0


def test_waterbed_causality_asset_chains():
    class DummyOp:
        def __init__(self, turn, tool, path, is_error=False):
            self.turn = turn
            self.tool = tool
            self.path = path
            self.is_error = is_error

    traj = [
        {"turn": 1, "vector": "positive", "semantic_distance": 0.80},
        {"turn": 2, "vector": "positive", "semantic_distance": 0.55},
        {"turn": 3, "event": "test_fail", "semantic_distance": 0.60},
    ]
    ops_by_turn = {
        2: [DummyOp(2, "Edit", "tcer/core/models.py")],
        3: [DummyOp(3, "Bash", "pytest tests/test_pricing.py", is_error=True)],
    }

    causal = metrics.analyze_waterbed_causality(traj, ops_by_turn)
    assert len(causal) >= 1
    link = causal[0]
    assert link["source_turn"] == 2
    assert link["target_turn"] == 3
    assert link["lag_turns"] == 1
    assert link["source_file"] == "tcer/core/models.py"
    assert link["blast_radius"] == 1


def test_swarm_synergy_and_multibody_coupling():
    # 1. 高协同多体系统：子代理皆向心推进
    traj_high_synergy = [
        {"turn": 1, "semantic_distance": 0.85, "subagents": [
            {"name": "Scout", "status": "convergent", "semantic_delta": -0.05},
            {"name": "Worker", "status": "convergent", "semantic_delta": -0.08},
        ]},
        {"turn": 2, "semantic_distance": 0.40},
    ]
    s_high = metrics.compute_swarm_synergy(traj_high_synergy)
    assert s_high["total_subagents"] == 2
    assert s_high["convergent_count"] == 2
    assert s_high["synergy_score"] >= 80.0
    assert s_high["net_semantic_delta"] < 0

    # 2. 负向发散多体系统：子代理引入噪音
    traj_low_synergy = [
        {"turn": 1, "semantic_distance": 0.60, "subagents": [
            {"name": "Worker1", "status": "divergent", "semantic_delta": +0.06},
            {"name": "Worker2", "status": "divergent", "semantic_delta": +0.05},
        ]},
    ]
    s_low = metrics.compute_swarm_synergy(traj_low_synergy)
    assert s_low["divergent_count"] == 2
    assert s_low["synergy_score"] < 50.0

# ============================================================
# 6. 遥测协议兼容性与确定性后备补全测试
# ============================================================

def test_telemetry_protocol_and_deterministic_backfill():
    # 验证 DYNAMICS_PROMPT_VERSION 已更新为 2026-09-dyn-v3
    assert DYNAMICS_PROMPT_VERSION == "2026-09-dyn-v3"
    assert "2026-09-dyn-v3" in _DYNAMICS_SYSTEM
    assert "Waddington" in _DYNAMICS_SYSTEM
    assert "epistemic_balance" in _DYNAMICS_SYSTEM
    assert "damping_ratio" in _DYNAMICS_SYSTEM
    assert "carnot_efficiency" in _DYNAMICS_SYSTEM

    # 提示词中明确禁止 LaTeX 代码标记
    assert "严禁使用任何 LaTeX 数学公式代码语法" in _DYNAMICS_SYSTEM
    assert "通俗易懂的中文工程师自然语言" in _DYNAMICS_SYSTEM

    # 模拟历史旧版模型输出（缺少 potential_energy, epistemic_debt, regime 等新字段）
    legacy_reply = (
        "正文深度复盘分析报告\n\n"
        "```json\n"
        "{\n"
        '  "convergence_type": "dirac",\n'
        '  "trajectory": [\n'
        '    {"turn": 1, "semantic_distance": 0.85, "vector": "positive", "event": "normal"},\n'
        '    {"turn": 5, "semantic_distance": 0.45, "vector": "positive", "event": "normal"},\n'
        '    {"turn": 10, "semantic_distance": 0.10, "vector": "positive", "event": "normal"}\n'
        '  ],\n'
        '  "capabilities": {\n'
        '    "intent_formalization": 85,\n'
        '    "drift_sensitivity": 75,\n'
        '    "feedback_mutual_info": 80\n'
        '  }\n'
        "}\n"
        "```"
    )

    text, data = parse_dynamics_payload(legacy_reply)
    assert "正文深度复盘分析报告" in text
    assert data is not None

    # 断言缺失字段已被 ground_dynamics_user_turns 确定性补齐
    assert "damping_ratio" in data
    assert 0.70 <= data["damping_ratio"] <= 1.10
    assert "carnot_efficiency" in data
    assert 0.0 <= data["carnot_efficiency"] <= 1.0
    assert "barrier_crossed" in data
    assert data["barrier_crossed"] is True  # 0.85 -> 0.45 越过 0.55 势垒

    traj = data.get("trajectory", [])
    assert len(traj) == 3
    # 逐节点物理场均已补全
    for pt in traj:
        assert "potential_energy" in pt
        assert -1.2 <= pt["potential_energy"] <= 0.8
        assert "epistemic_debt" in pt
        assert pt["epistemic_debt"] >= 0.0
        assert "regime" in pt
        assert pt["regime"] in ("gas", "liquid", "glass", "crystal")

    # 末节点 Ds=0.10 自动归入 crystal
    assert traj[-1]["regime"] == "crystal"

    # 能力字典自动补全第 4 项 epistemic_balance
    assert "epistemic_balance" in data["capabilities"]
    assert 20 <= data["capabilities"]["epistemic_balance"] <= 100

    # 验证 derived=None 且完全无 trajectory 时的确定性后备补全
    bare_data = ground_dynamics_user_turns({"convergence_type": "trapped"}, derived=None)
    assert bare_data is not None
    assert bare_data["damping_ratio"] == 1.0
    assert 0.0 <= bare_data["carnot_efficiency"] <= 1.0
    assert bare_data["barrier_crossed"] is False
    assert bare_data["trajectory"] == []


# ============================================================
# 7. GUI 相空间动力学相图视口冒烟测试 (无头 Tk)
# ============================================================

def test_long_session_dynamic_sampling_and_gap_boosting():
    from tcer.core.llm_prompts import dynamics_prompt

    # 1. 模拟 762 轮超长会话的 prompt 契约要求
    stats_762 = [TurnStat(i, ts=i * 1000, input_tokens=1000, output_tokens=500,
                          user_turn=(i // 150) + 1 if i % 150 == 0 else None) for i in range(762)]
    derived_762 = {
        "stats": stats_762,
        "retry_spans": [(70, 74), (309, 313)],
        "compaction_turns": [381],
        "ops_by_turn": {},
        "loc_by_turn": {},
    }
    meta = SessionMeta(session_id="s762", cwd="/tmp", title="超长攻坚会话",
                       path=Path("/tmp/s762.jsonl"), is_subagent=False)
    u = TokenUsage(
        input_tokens=100_000,
        output_tokens=50_000,
        cache_read_input_tokens=500_000,
        models={"claude-fable-5-1"},
        assistant_msgs=762,
        user_msgs=5,
        tool_calls={"Edit": 10},
    )
    u.compaction_count = 1
    u.turn_stats = stats_762
    report = metrics.compute(meta, u, net_loc=400, task_type="feature")

    _, user_prompt = dynamics_prompt(report, derived_762, "metrics")
    assert "10~" in user_prompt
    assert "U1于第1轮" in user_prompt
    assert "第71-75轮死循环" in user_prompt
    assert "第382轮压缩" in user_prompt

    # 2. 模拟 LLM 在长会话中偷懒只输出了 3 个点（存在上百轮巨大跨度），验证 ground_dynamics_user_turns 自动增密填补空洞
    lazy_payload = {
        "convergence_type": "escaped",
        "trajectory": [
            {"turn": 1, "semantic_distance": 0.86, "vector": "positive"},
            {"turn": 382, "semantic_distance": 0.50, "vector": "positive"},
            {"turn": 762, "semantic_distance": 0.12, "vector": "positive"},
        ]
    }
    dense_payload = ground_dynamics_user_turns(lazy_payload, derived_762)
    assert dense_payload is not None
    dense_traj = dense_payload["trajectory"]
    # 密集点数量应当显著多于初始的 3 个
    assert len(dense_traj) >= 5
    for p in dense_traj:
        assert "potential_energy" in p
        assert "epistemic_debt" in p
        assert "regime" in p


def test_gui_phase_portrait_smoke():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("无显示环境，跳过 GUI 冒烟")

    root.withdraw()
    try:
        from tcer.gui.views import PhasePortraitWidget

        widget = PhasePortraitWidget(root)

        mock_dynamics = {
            "convergence_type": "escaped",
            "attractor_trapped": False,
            "damping_ratio": 0.88,
            "carnot_efficiency": 0.75,
            "barrier_crossed": True,
            "barrier_turn": 3,
            "trajectory": [
                {"turn": 1, "semantic_distance": 0.86, "vector": "positive", "event": "normal",
                 "potential_energy": -0.59, "epistemic_debt": 0.35, "regime": "gas"},
                {"turn": 2, "semantic_distance": 0.70, "vector": "positive", "event": "normal",
                 "potential_energy": -0.40, "epistemic_debt": 1.20, "regime": "liquid"},
                {"turn": 3, "semantic_distance": 0.45, "vector": "positive", "event": "breakthrough",
                 "potential_energy": -0.10, "epistemic_debt": 1.50, "regime": "liquid"},
                {"turn": 4, "semantic_distance": 0.12, "vector": "positive", "event": "normal",
                 "potential_energy": -1.05, "epistemic_debt": 0.80, "regime": "crystal"},
            ],
            "capabilities": {
                "intent_formalization": 90,
                "drift_sensitivity": 80,
                "feedback_mutual_info": 85,
                "epistemic_balance": 88,
            },
            "lyapunov_exponent": -0.32,
            "swarm_synergy": {
                "total_subagents": 2,
                "synergy_score": 85.0,
            },
        }
        mock_report = {
            "turns": 4,
            "cost_display": "$0.45",
        }

        # 1. 渲染时序流形
        widget.render(mock_dynamics, mock_report)
        root.update_idletasks()

        # 断言微型物理徽标文本非空
        assert "阻尼比" in widget.damping_badge.cget("text")
        assert "卡诺效率" in widget.carnot_badge.cget("text")
        assert "收敛稳定" in widget.lyapunov_badge.cget("text")
        assert "多智能体协同" in widget.swarm_badge.cget("text")
        # 2. 切换为相速度极限环模式并重新渲染
        widget._set_mode("phase_plane")
        root.update_idletasks()
        assert widget._view_mode == "phase_plane"

        # 2b. 切换回时序流形模式
        widget._set_mode("manifold")
        root.update_idletasks()
        assert widget._view_mode == "manifold"

        # 2c. 质点巡航探针与时间线步进测试 (Node Inspection & Scrubbing)
        assert len(widget._pts) > 0
        widget._select_node(1)
        assert widget._selected_node_idx == 1
        widget._step_node(1)
        assert widget._selected_node_idx == 2
        widget._step_node(-1)
        assert widget._selected_node_idx == 1
        # 边界环形循环测试（首尾循环永不卡死）
        widget._selected_node_idx = 0
        widget._step_node(-1)
        assert widget._selected_node_idx == len(widget._pts) - 1
        widget._step_node(1)
        assert widget._selected_node_idx == 0
        widget._select_node(1)
        widget._center_node()
        widget._deselect_node()
        assert widget._selected_node_idx is None

        # 2d. 复位测试 (Reset Zoom)
        widget._reset_zoom()
        assert widget._zoom_scale == 1.0
        assert widget._pan_x == 0.0
        assert widget._pan_y == 0.0
        # 3. 模拟鼠标悬停事件触发 Tooltip 计算
        class DummyEvent:
            x = 120
            y = 100
        widget._on_motion(DummyEvent())

        # 4. 交互缩放与平移测试 (Zoom & Pan)
        initial_scale = widget._zoom_scale
        assert initial_scale == 1.0
        assert widget.zoom_lbl.cget("text") == "100%"

        # 放大
        widget._zoom(1.18, cx=300, cy=240)
        assert widget._zoom_scale > 1.0
        assert "118%" in widget.zoom_lbl.cget("text")

        # 拖拽平移
        widget._drag_data = {"x": 300, "y": 240, "panned": False}
        class DragEvent:
            x = 320
            y = 250
        widget._on_drag(DragEvent())
        assert widget._drag_data["panned"] is True

        # 复位
        widget._reset_zoom()
        assert widget._zoom_scale == 1.0
        assert widget._pan_x == 0.0
        assert widget._pan_y == 0.0
        assert widget.zoom_lbl.cget("text") == "100%"

        # 5. 守护断言：狄拉克目标点严格位于左下角，X 轴底座标注未被修改
        assert widget._tgt_pos is not None
        assert widget._tgt_pos[0] < 120 and widget._tgt_pos[1] > 300
        canvas_texts = [widget.canvas.itemcget(item_id, "text")
                        for item_id in widget.canvas.find_all()
                        if widget.canvas.type(item_id) == "text"]
        assert "0.0 (契合真实意图)" in canvas_texts
        assert "1.0 (严重偏离意图)" in canvas_texts
    finally:
        root.destroy()
