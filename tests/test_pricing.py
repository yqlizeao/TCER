"""Tests for per-model pricing resolution (data/model_pricing.json + pricing.py)."""
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


def test_thinking_suffix_maps_to_base_opus():
    """Claude Code / proxies append ``-thinking``; must not fall back to default."""
    base = pricing.table_key("claude-opus-4-6")
    assert base == "claude-opus-4-6-20260206"
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
