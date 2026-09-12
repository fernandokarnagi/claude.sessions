"""
The rate table and what it refuses to price.

The rule under test throughout: only a metered Anthropic model produces a
dollar figure. Everything else reports its class and leaves the money blank,
because a made-up rate on a subscription or local model is worse than no
number at all.
"""

import json

import pytest

from server import prices


@pytest.fixture(autouse=True)
def no_overrides(tmp_path, monkeypatch):
    """Point the override file at an empty scratch path, per test."""
    monkeypatch.setattr(prices, "PRICES_FILE", str(tmp_path / ".prices.json"))
    monkeypatch.setattr(prices, "_cache", {"at": 0.0, "mtime": None, "table": None})
    return tmp_path / ".prices.json"


ONE_M = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_creation": 0}


def test_a_listed_model_is_priced_from_the_table():
    assert prices.cost("claude-opus-5", ONE_M)["usd"] == 5.0
    assert prices.cost("claude-sonnet-5", ONE_M)["usd"] == 2.0


def test_a_dated_snapshot_id_resolves_to_its_family_row():
    """Claude Code writes `claude-haiku-4-5-20251001` into the transcript."""
    assert prices.classify("claude-haiku-4-5-20251001") == prices.METERED
    assert prices.cost("claude-haiku-4-5-20251001", ONE_M)["usd"] == 1.0


def test_the_longest_matching_prefix_wins():
    """`claude-opus-4-8` must not be served by a shorter `claude-opus-4` row."""
    prices.RATES["claude-opus-4"] = prices._row(99.0, 99.0)
    try:
        assert prices.cost("claude-opus-4-8", ONE_M)["usd"] == 5.0
    finally:
        prices.RATES.pop("claude-opus-4")


def test_cache_buckets_are_charged_at_their_own_rates():
    tokens = {"input": 0, "output": 0,
              "cache_read": 1_000_000, "cache_creation": 1_000_000}
    c = prices.cost("claude-opus-5", tokens)
    assert c["per_bucket"]["cache_read"] == 0.5      # 0.1x input
    assert c["per_bucket"]["cache_creation"] == 6.25  # 1.25x input


def test_a_cloud_model_is_subscription_and_shows_no_dollars():
    c = prices.cost("glm-5.3-flash:cloud", ONE_M)
    assert (c["billing"], c["priced"], c["usd"]) == (prices.SUBSCRIPTION, False, 0.0)


def test_a_free_tier_model_is_subscription():
    assert prices.classify("tencent/hy3:free") == prices.SUBSCRIPTION


def test_a_local_model_is_never_priced():
    for name in ("qwen3.6:35b-mlx", "ggml-org/Qwen3.6-35B-A3B-GGUF:Q8_0",
                 "gemma4:12b", "Qwen3.8-27B-UD-Q4_K_XL"):
        assert prices.classify(name) == prices.LOCAL, name
        assert prices.cost(name, ONE_M)["priced"] is False


def test_an_unrecognised_model_stays_unknown():
    """No rate, no marker, no launcher — so the fleet says it doesn't know."""
    c = prices.cost("some-model-nobody-registered", ONE_M)
    assert (c["billing"], c["priced"], c["usd"]) == (prices.UNKNOWN, False, 0.0)


def test_no_model_at_all_is_unknown():
    assert prices.classify(None) == prices.UNKNOWN
    assert prices.cost(None, ONE_M)["priced"] is False


def test_the_launch_registry_classifies_a_bare_resolved_id(monkeypatch):
    """A transcript records `glm-5.2`; the launcher knows it ran `:cloud`."""
    monkeypatch.setattr(prices.models, "canonical",
                        lambda m: "glm-5.2:cloud" if m == "glm-5.2" else None)
    assert prices.classify("glm-5.2") == prices.SUBSCRIPTION


def test_an_override_file_changes_a_rate(no_overrides):
    no_overrides.write_text(json.dumps(
        {"claude-opus-5": {"input": 1.0, "output": 1.0,
                           "cache_read": 0.0, "cache_write": 0.0}}))
    assert prices.cost("claude-opus-5", ONE_M)["usd"] == 1.0


def test_an_override_file_can_name_the_billing_class(no_overrides):
    """The escape hatch for a model whose id carries no marker."""
    assert prices.classify("kimi-k2.7-code") == prices.UNKNOWN
    no_overrides.write_text(json.dumps(
        {"kimi-k2.7-code": {"billing": "subscription"}}))
    assert prices.classify("kimi-k2.7-code") == prices.SUBSCRIPTION


def test_a_corrupt_override_file_is_ignored(no_overrides):
    no_overrides.write_text("{not json")
    assert prices.cost("claude-opus-5", ONE_M)["usd"] == 5.0
