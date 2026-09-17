"""Offline tests for scripts/smoke_openrouter_execution.py — MA3.

These never call OpenRouter or touch the network: only the pure model-
selection logic and the "no API key -> refuse before doing anything" guard
are exercised here. The live end-to-end path is deliberately not covered by
this suite — it is the operator's job to run the script itself against a
real OPENROUTER_API_KEY (see the script's own module docstring).
"""

from decimal import Decimal

from app.providers.base import ModelDescriptor
from scripts.smoke_openrouter_execution import main, select_free_model


def _descriptor(provider_model_id, cost_input, cost_output):
    return ModelDescriptor(
        provider_model_id=provider_model_id,
        name=provider_model_id,
        cost_input_per_mtok=cost_input,
        cost_output_per_mtok=cost_output,
    )


def test_select_free_model_prefers_named_free_tier_over_untagged_free_model():
    stealth_free = _descriptor("stealth/union-alpha", Decimal(0), Decimal(0))
    named_free = _descriptor("meta-llama/llama-3.2-3b-instruct:free", Decimal(0), Decimal(0))

    selected = select_free_model([stealth_free, named_free])

    assert selected is named_free


def test_select_free_model_falls_back_to_untagged_free_model_when_no_named_tier_exists():
    stealth_free = _descriptor("stealth/union-alpha", Decimal(0), Decimal(0))

    selected = select_free_model([stealth_free])

    assert selected is stealth_free


def test_select_free_model_returns_none_when_no_candidate_is_currently_free():
    paid = _descriptor("anthropic/claude-3.5-sonnet", Decimal("3.00"), Decimal("15.00"))
    unknown = _descriptor("some-org/unpriced-model", None, None)

    assert select_free_model([paid, unknown]) is None


def test_select_free_model_never_trusts_free_naming_over_real_pricing():
    """A model whose id happens to end in ":free" but is not actually
    priced at zero must never be selected — pricing metadata
    (classify_pricing), not the name, is authoritative."""
    misleadingly_named = _descriptor("some-org/not-actually-free:free", Decimal("1.00"), Decimal("2.00"))

    assert select_free_model([misleadingly_named]) is None


def test_main_refuses_and_returns_nonzero_when_api_key_is_missing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert main() == 1
