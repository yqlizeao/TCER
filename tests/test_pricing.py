"""Tests for per-model pricing resolution (data/model_pricing.json + pricing.py)."""
import json
import pytest

from tcer.core import metrics, pricing
from tcer.core.models import TokenUsage


def test_table_loaded():
    assert pricing.model_count() >= 150
    assert "claude-opus-4-8" in pricing._load()["models"]


def test_exact_resolve():
    r = pricing.resolve("claude-opus-4-8")
    assert r == {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}


def test_doubao_seed_2_1():
    # v3.16.4: 豆包 Seed 2.1 Pro/Turbo（火山官方 list 价，CNY 按 ~7.14 折算）
    assert pricing.resolve("doubao-seed-2-1-pro") == {
        "input": 0.84, "output": 4.2, "cache_read": 0.17, "cache_write": 0.0,
    }
    assert pricing.resolve("doubao-seed-2-1-turbo") == {
        "input": 0.42, "output": 2.1, "cache_read": 0.08, "cache_write": 0.0,
    }



def test_suffix_prefix_resolve():
    # Claude Code appends a [1m] / dated suffix to the base id.
    assert pricing.resolve("claude-opus-4-8[1m]") == pricing.resolve("claude-opus-4-8")


def test_irregular_provider_names_collapse():
    """Providers wrap/damage the id in various ways; all must resolve to the
    one canonical table key so the model-comparison tab doesn't show duplicates."""
    glm52 = pricing.resolve("glm-5.2")
    # missing dash
    assert pricing.normalize("glm5.2") == "glm-5.2"
    assert pricing.resolve("glm5.2") == glm52
    # vendor path prefix
    assert pricing.normalize("z-ai/glm-5.2") == "glm-5.2"
    assert pricing.resolve("z-ai/glm-5.2") == glm52
    assert pricing.normalize("z-ai/glm-5.1") == "glm-5.1"
    # deep vendor path + version dot spelled as 'p' (fireworks: glm-5p2)
    assert pricing.normalize("accounts/fireworks/models/glm-5p2") == "glm-5.2"
    assert pricing.resolve("accounts/fireworks/models/glm-5p2") == glm52
    # -fp8 quantization suffix still routes via forward prefix
    assert pricing.normalize("glm-5.2-fp8") == "glm-5.2"
    assert pricing.resolve("glm-5.2-fp8") == glm52
    # upper-case
    assert pricing.resolve("GLM-5.2") == glm52


def test_gpt_dash_version_collapses():
    """Providers render ``gpt-5.6`` with a dash (``gpt-5-6``); the version dot
    must be restored so the id binds to the GPT-5.6 entry, not forward-prefix
    onto the shorter ``gpt-5`` key (wrong label AND wrong price)."""
    sol = pricing.resolve("gpt-5.6-sol")
    assert pricing.normalize("gpt-5-6-sol") == "gpt-5.6-sol"
    assert pricing.resolve("gpt-5-6-sol") == sol
    # all three GPT-5.6 tiers + an effort suffix
    assert pricing.normalize("gpt-5-6") == "gpt-5.6"
    assert pricing.normalize("gpt-5-6-luna") == "gpt-5.6-luna"
    assert pricing.normalize("gpt-5-6-terra") == "gpt-5.6-terra"
    assert pricing.normalize("gpt-5-6-high") == "gpt-5.6-high"
    # the same dash damage hits the older codex line too
    assert pricing.normalize("gpt-5-1-codex") == "gpt-5.1-codex"
    assert pricing.normalize("gpt-5-3-codex") == "gpt-5.3-codex"
    # regression guard: must NOT collapse onto the shorter gpt-5 key
    assert pricing.table_key("gpt-5-6-sol") != "gpt-5"
    assert pricing.resolve("gpt-5-6-sol") != pricing.resolve("gpt-5")


def test_grok_dash_version_with_thinking_suffix():
    """omp emits ``grok-4-5-thinking`` — dash-spelled ``grok-4.5`` plus a
    ``-thinking`` mode suffix. The raw id forward-prefixes onto the shorter
    ``grok-4`` key (wrong label AND wrong price); the suffix-stripped candidate
    must normalize onto ``grok-4.5`` first. Covers the bare id, the bare
    dash-spelled id, and the ``provider/modelId`` form omp writes in
    ``model_change`` events."""
    g45 = pricing.resolve("grok-4.5")
    assert pricing.table_key("grok-4-5-thinking") == "grok-4.5"
    assert pricing.normalize("grok-4-5-thinking") == "grok-4.5"
    assert pricing.resolve("grok-4-5-thinking") == g45
    # provider/ prefix form (omp model_change events)
    assert pricing.normalize("granola/grok-4-5-thinking") == "grok-4.5"
    assert pricing.resolve("granola/grok-4-5-thinking") == g45
    # bare dash-spelled id without the thinking suffix
    assert pricing.normalize("grok-4-5") == "grok-4.5"
    # regression guard: must NOT bind onto the shorter grok-4 key
    assert pricing.table_key("grok-4-5-thinking") != "grok-4"
    assert pricing.resolve("grok-4-5-thinking") != pricing.resolve("grok-4")


def test_table_key_distinguishes_default():
    assert pricing.table_key("glm-5.2") == "glm-5.2"
    assert pricing.table_key("glm5.2") == "glm-5.2"  # normalized, not default
    assert pricing.table_key("totally-made-up-model") is None
    assert pricing.table_key(None) is None
    assert pricing.table_key("") is None


def test_note_for_multitrack_annotations():
    """``_note`` carries the non-primary price tracks (promo/peak/batch/tiered)
    a single rate set can't express; surfaced by the GUI price tooltip."""
    # 促销/下线路由条目：当前生效价为主价，备注含促销期或下线路由说明
    assert "2026-09-11 起 V4 Flash 退役" in pricing.note_for("deepseek-v4-flash")
    assert "促销价" in pricing.note_for("gpt-5.6-sol")
    assert "介绍价至 2026-12-31" in pricing.note_for("gemini-3.8-flash")
    # 别名条目跟随主条目（resolve 路径不同，note 同样命中）
    assert "legacy 别名" in pricing.note_for("deepseek-chat")
    # 无备注条目 / 未知模型 / 空值 → None
    assert pricing.note_for("claude-opus-4-8") is None
    assert pricing.note_for("totally-made-up-model") is None
    assert pricing.note_for(None) is None
    assert pricing.note_for("") is None


def test_thinking_suffix_maps_to_base_opus():
    """Claude Code / proxies append ``-thinking``; must not fall back to default."""
    base = pricing.table_key("claude-opus-4-6")
    assert base == "claude-opus-4-6"
    assert pricing.table_key("claude-opus-4-6-thinking") == base
    # Real table key that ends in -thinking must still exact-match itself.
    if "kimi-k2-thinking" in pricing._load()["models"]:
        assert pricing.table_key("kimi-k2-thinking") == "kimi-k2-thinking"
    # Effort tiers are real SKUs — must NOT be stripped to a shorter key.
    assert pricing.table_key("gpt-5.2-high") == "gpt-5.2-high"


def test_unmatched_models_lists_default_fallback_only():
    ids = [
        "claude-opus-4-8",
        "totally-made-up-model",
        "another-unknown-v2",
        "",
        "<synthetic>",
        "totally-made-up-model",  # dedupe
    ]
    got = pricing.unmatched_models(ids)
    assert got == ["another-unknown-v2", "totally-made-up-model"]
    assert pricing.is_table_priced("claude-opus-4-8")
    assert not pricing.is_table_priced("totally-made-up-model")


def test_unmatched_pricing_models_from_usage():
    u = TokenUsage()
    u.bucket("claude-opus-4-8").add(10, 0, 0, 5)
    u.bucket("mystery-lab-model").add(20, 0, 0, 5)
    u.bucket("").add(1, 0, 0, 0)
    assert metrics.unmatched_pricing_models(u) == ["mystery-lab-model"]


def test_unknown_falls_back_to_default():
    assert pricing.resolve("totally-made-up-model") == pricing.default_pricing()
    assert pricing.default_pricing() == {
        "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75,
    }


def test_cost_zero_tokens():
    """Zero tokens should produce zero cost."""
    u = TokenUsage()
    assert metrics.cost_usd(u) == 0.0


def test_cost_single_token_precision():
    """Single token cost should be calculated with full precision."""
    u = TokenUsage(input_tokens=1)
    # $3/MTok for input = $3 per 1,000,000 tokens
    expected = 1 * 3.0 / 1_000_000  # 3e-06
    assert metrics.cost_usd(u) == pytest.approx(expected)

    u2 = TokenUsage(output_tokens=1)
    # $15/MTok for output
    expected2 = 1 * 15.0 / 1_000_000  # 1.5e-05
    assert metrics.cost_usd(u2) == pytest.approx(expected2)


def test_cost_uses_session_model():
    u = TokenUsage(
        input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        output_tokens=1_000_000,
        models={"claude-opus-4-8[1m]"},
    )
    # 5 + 6.25 + 0.5 + 25
    assert metrics.cost_usd(u) == 36.75


def test_cost_mixed_models_falls_back_to_default():
    u = TokenUsage(input_tokens=1_000_000, models={"claude-opus-4-8", "gpt-5"})
    assert metrics.cost_usd(u) == 3.0  # default input rate, no single model


def test_cost_explicit_model_overrides():
    u = TokenUsage(output_tokens=1_000_000)
    assert metrics.cost_usd(u, model="gpt-5") == 10.0  # GPT-5 output = $10/MTok


def _bucketed():
    """A mixed-model session: 1M output on Opus 4.8 + 1M output on GLM-5.2."""
    u = TokenUsage()
    u.models.update({"claude-opus-4-8[1m]", "glm-5.2"})
    u.bucket("claude-opus-4-8[1m]").add(0, 0, 0, 1_000_000)
    u.bucket("glm-5.2").add(0, 0, 0, 1_000_000)
    u.output_tokens = 2_000_000  # scalar total stays consistent with buckets
    return u


def test_mixed_session_priced_per_model():
    u = _bucketed()
    # Opus 4.8 output $25/MTok + GLM-5.2 output $4.4/MTok = 29.4 (NOT 2*default 15=30)
    assert metrics.cost_usd(u) == 25.0 + 4.4


def test_cost_by_model_breakdown():
    u = _bucketed()
    cbm = metrics.cost_by_model(u)
    assert cbm["claude-opus-4-8[1m]"] == 25.0
    assert cbm["glm-5.2"] == 4.4
    assert metrics.cost_usd(u) == sum(cbm.values())


def test_unknown_model_bucket_uses_default():
    u = TokenUsage(output_tokens=1_000_000)
    u.bucket("").add(0, 0, 0, 1_000_000)  # no model recorded -> default $15/MTok
    assert metrics.cost_usd(u) == 15.0


def test_per_model_survives_merge():
    a, b = _bucketed(), _bucketed()
    m = a.merge(b)
    # buckets doubled; cost doubles and stays per-model accurate
    assert m.per_model["glm-5.2"].output_tokens == 2_000_000
    assert metrics.cost_usd(m) == 2 * (25.0 + 4.4)


def test_cache_write_1h_premium():
    """1h 缓存写 = 5m 率 × 1.6（Anthropic 1h 2×input vs 5m 1.25×input）。"""
    from tcer.core.metrics import CACHE_1H_PREMIUM, cost_usd
    from tcer.core.models import TokenUsage

    base = TokenUsage(cache_creation_input_tokens=1_000_000,
                      models={"claude-opus-4-8"})
    hot = TokenUsage(cache_creation_input_tokens=1_000_000,
                     cache_write_1h_tokens=1_000_000,
                     models={"claude-opus-4-8"})
    c_base = cost_usd(base)
    c_hot = cost_usd(hot)
    assert c_hot > c_base
    assert abs(c_hot - c_base * (1 + CACHE_1H_PREMIUM)) < 1e-9


def test_cache_write_1h_flows_through_scan_and_merge(tmp_path):
    """reader 解析 cache_creation.ephemeral_1h 分档并进 per_model 桶与 merge。"""
    import json

    from tcer.core import reader
    from tcer.core.metrics import cost_by_model, cost_usd

    line = {
        "type": "assistant",
        "timestamp": "2026-07-01T10:00:00Z",
        "message": {
            "role": "assistant", "id": "m1", "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 200,
                                   "ephemeral_1h_input_tokens": 800},
            },
        },
    }
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    u = reader.aggregate_usage(p)
    assert u.cache_write_1h_tokens == 800
    mu = u.per_model[next(iter(u.per_model))]
    assert mu.cache_write_1h_tokens == 800
    # merge 保持子集
    m = u.merge(u)
    assert m.cache_write_1h_tokens == 1600
    assert m.per_model[next(iter(m.per_model))].cache_write_1h_tokens == 1600
    # 逐模型成本加总 == 总成本（审计一致性）
    assert abs(sum(cost_by_model(m).values()) - cost_usd(m)) < 1e-9


def test_newly_added_models_and_current_time_pricing():
    """Verify models added/updated in 2026-09 pricing audit."""
    # GPT-6 Astra
    assert pricing.is_table_priced("gpt-6-astra")
    astra = pricing.resolve("gpt-6-astra")
    assert astra == {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5}
    assert pricing.label("gpt-6-astra") == "GPT-6 Astra"

    # DeepSeek V4.1 Flash
    assert pricing.is_table_priced("deepseek-flash")
    assert pricing.resolve("deepseek-flash") == {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0.0}

    # 当前时间（2026-09-21）实际生效价格
    # GPT-5.6 Sol 促销期价格生效中
    assert pricing.resolve("gpt-5.6-sol") == {"input": 4.0, "output": 20.0, "cache_read": 0.4, "cache_write": 5.0}
    # Gemini 3.8 Flash 介绍期价格生效中
    assert pricing.resolve("gemini-3.8-flash") == {"input": 0.75, "output": 3.75, "cache_read": 0.075, "cache_write": 0.0}
    # DeepSeek V4 Pro 路由生效价
    assert pricing.resolve("deepseek-v4-pro") == {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0.0}
    # o3-mini 降价
    assert pricing.resolve("o3-mini") == {"input": 0.55, "output": 2.2, "cache_read": 0.275, "cache_write": 0.0}



def test_expanded_models_and_consistent_notes():
    """Verify 2026 cutting-edge frontier models (Claude Opus 5.5, Sonnet 5.5,
    GPT-6 Sol, Gemini 3.8 Pro, Grok 5, DeepSeek V4.5, Qwen 4 Coder, etc.)
    and standardized note format."""
    # 1. 2026-09 Released Claude Opus 5.5 & Claude 5.5 Family
    assert pricing.is_table_priced("claude-opus-5-5")
    op55 = pricing.resolve("claude-opus-5-5")
    assert op55 == {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
    assert pricing.label("claude-opus-5-5") == "Claude Opus 5.5"
    assert pricing.table_key("claude-opus-5-5") == "claude-opus-5-5"
    # Regression guard: must NOT collapse onto older claude-opus-5
    assert pricing.normalize("claude-opus-5-5") == "claude-opus-5-5"
    assert pricing.resolve("claude-opus-5-5") != pricing.resolve("claude-opus-5")
    assert pricing.is_table_priced("claude-sonnet-5-5")
    assert pricing.resolve("claude-sonnet-5-5") == {"input": 1.5, "output": 7.5, "cache_read": 0.15, "cache_write": 1.875}
    assert pricing.is_table_priced("claude-haiku-5-5")

    # 2. OpenAI GPT-6 Cutting-Edge Series
    assert pricing.is_table_priced("gpt-6-sol")
    assert pricing.resolve("gpt-6-sol") == {"input": 6.0, "output": 30.0, "cache_read": 0.6, "cache_write": 7.5}
    assert pricing.is_table_priced("gpt-6-codex")
    assert pricing.is_table_priced("gpt-6-luna")

    # 3. Google Gemini 3.8 Pro & 3.9
    assert pricing.is_table_priced("gemini-3.8-pro")
    assert pricing.resolve("gemini-3.8-pro") == {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0}
    assert pricing.is_table_priced("gemini-3.9-pro")

    # 4. xAI Grok 5 & Grok 4.6 Build
    assert pricing.is_table_priced("grok-5")
    assert pricing.resolve("grok-5") == {"input": 5.0, "output": 20.0, "cache_read": 1.0, "cache_write": 0.0}
    assert pricing.is_table_priced("grok-4.6-build")

    # 5. DeepSeek 2026 Frontier (V4.5 & R1)
    assert pricing.is_table_priced("deepseek-v4.5")
    assert pricing.resolve("deepseek-v4.5") == {"input": 0.35, "output": 1.4, "cache_read": 0.007, "cache_write": 0.0}
    assert pricing.is_table_priced("deepseek-r1")

    # 6. Qwen 4 Coder & Max
    assert pricing.is_table_priced("qwen4-coder")
    assert pricing.resolve("qwen4-coder") == {"input": 0.3, "output": 0.9, "cache_read": 0.03, "cache_write": 0.0}
    assert pricing.is_table_priced("qwen4-max")

    # 7. Meta LLaMA 4 Series
    assert pricing.is_table_priced("llama-4-scion")
    assert pricing.is_table_priced("llama-4-maverick")

    # 8. Note 格式一致性：所有 note 使用全角分号隔开，不使用半角分号，不含有未经整理的碎句
    raw_models = pricing._load()["models"]
    for mid, entry in raw_models.items():
        note = entry.get("_note")
        if note is not None:
            assert isinstance(note, str)
            assert len(note.strip()) > 0
            # 统一要求：不得使用半角分号
            assert ";" not in note, f"Model {mid} note contains half-width semicolon: {note}"
            # 统一要求：括号应为全角括号
            assert "(" not in note and ")" not in note, f"Model {mid} note contains half-width parens: {note}"


def test_pricing_vendor_and_description():
    """Verify pricing.vendor and pricing.description lookups."""
    assert pricing.vendor("claude-opus-5-5") == "Anthropic"
    assert pricing.vendor("gpt-6-astra") == "OpenAI"
    assert pricing.vendor("gemini-3.8-flash") == "Google"
    assert pricing.vendor("grok-5") == "xAI"
    assert pricing.vendor("deepseek-r1") == "DeepSeek"
    assert pricing.vendor("qwen4-coder") == "Alibaba"
    assert pricing.vendor("glm-5.5") == "Zhipu AI"
    assert pricing.vendor("kimi-k3.5") == "Moonshot AI"
    assert pricing.vendor("minimax-m3.5") == "MiniMax"
    assert pricing.vendor("llama-4-scion") == "Meta"
    assert pricing.vendor("codestral-latest") == "Mistral AI"
    assert pricing.vendor(None) == ""
    assert pricing.vendor("") == ""

    # description alias
    desc = pricing.description("claude-opus-5-5")
    assert desc is not None
    assert "Anthropic 2026 前沿旗舰智能体模型" in desc


def test_model_price_tip_display():
    """Verify GUI price tooltip renders cleanly with vendor and standardized descriptions."""
    from types import SimpleNamespace
    from tcer.gui.views import _model_price_tip

    mc = SimpleNamespace(model_id="claude-3-7-sonnet", display_name="Claude 3.7 Sonnet")
    tip = _model_price_tip(mc)
    assert "[Anthropic] Claude 3.7 Sonnet · 官方标价（$/百万 Token）" in tip
    assert "输入　　　$3/百万" in tip
    assert "输出　　　$15/百万" in tip
    assert "缓存创建　$3.75/百万" in tip
    assert "缓存命中　$0.3/百万" in tip
    assert "ℹ️ 描述：Anthropic 混合推理模型" in tip

    # 未知模型应展示默认配置价警示
    unknown_mc = SimpleNamespace(model_id="unknown-test-model", display_name="Unknown Model")
    unknown_tip = _model_price_tip(unknown_mc)
    assert "默认配置价（未在价表中）" in unknown_tip
    assert "⚠️ 该模型未在价表中" in unknown_tip


def test_claude_2_1_283_drift_fixes(tmp_path):
    """Verify fixes for Claude Code 2.1.283 format drift:
    1. Deep head sampling (head_n=60) extracts effort past early attachments.
    2. Top-level type: 'mode' events record plan mode transitions.
    """
    from tcer.core import reader

    # 构造模拟 2.1.283 头部：先出现连续 25 个 attachment / snapshot，再出现 effort=high 的 assistant
    lines = [
        {"type": "mode", "mode": "normal", "sessionId": "s-test-283"},
        {"type": "permission-mode", "permissionMode": "bypassPermissions", "sessionId": "s-test-283"},
    ]
    for i in range(25):
        lines.append({"type": "attachment", "attachment": {"type": "environment", "index": i}})
    lines.append({
        "type": "assistant",
        "effort": "high",
        "perTurnEffort": "high",
        "version": "2.1.283",
        "sessionId": "s-test-283",
        "message": {"role": "assistant", "id": "m1", "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
    })
    # 模拟进入 plan 模式的顶层 mode 事件
    lines.append({"type": "mode", "mode": "plan", "sessionId": "s-test-283"})

    p = tmp_path / "session_283.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    meta = reader._read_session_meta_uncached(p)
    assert meta.cli_version == "2.1.283"
    assert meta.reasoning_effort == "high"
    assert meta.permission_profile == "bypassPermissions"

    usage, loc = reader.scan_session(p)
    assert usage.plan_mode_count == 1
    assert "claude-opus-5-5" in usage.models
    assert "claude-opus-5-5" in usage.per_model
