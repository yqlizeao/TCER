"""Tests for per-model pricing resolution (data/model_pricing.json + pricing.py)."""
from tcer import metrics, pricing
from tcer.models import TokenUsage


def test_table_loaded():
    assert pricing.model_count() >= 150
    assert "claude-opus-4-8" in pricing._load()["models"]


def test_exact_resolve():
    r = pricing.resolve("claude-opus-4-8")
    assert r == {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}


def test_suffix_prefix_resolve():
    # Claude Code appends a [1m] / dated suffix to the base id.
    assert pricing.resolve("claude-opus-4-8[1m]") == pricing.resolve("claude-opus-4-8")


def test_unknown_falls_back_to_default():
    assert pricing.resolve("totally-made-up-model") == pricing.default_pricing()
    assert pricing.default_pricing() == {
        "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75,
    }


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
