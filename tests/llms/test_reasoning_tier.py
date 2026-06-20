"""Reasoning-tier provider seam tests (BDP-2360, P5).

Proves the ``reasoning_tier`` PluggableRegistry: the Anthropic provider is
byte-identical to the historical ``_effort_to_budget`` (same caps, same
``max_tokens`` clamp, same effort validation), the OpenAI provider is a
validated effort passthrough, and the seam is swappable with a fake.
"""

from __future__ import annotations

import pytest

from omnigent.llms.adapters.anthropic import _effort_to_budget
from omnigent.llms.errors import PermanentLLMError
from omnigent.llms.reasoning_tier import (
    AnthropicReasoningTier,
    OpenAIReasoningTier,
    ReasoningTierProvider,
    reasoning_tier_registry,
)
from omnigent.pluggable import PluggableRegistry

# ── Anthropic provider: byte-identical to old _effort_to_budget ──


@pytest.mark.parametrize(
    ("effort", "max_tokens", "expected"),
    [
        ("low", 16384, 1024),
        ("medium", 16384, 4096),
        ("high", 16384, 8192),
        ("low", 512, 512),  # clamped by max_tokens
        ("medium", 2048, 2048),  # clamped by max_tokens
        ("high", 4096, 4096),  # clamped by max_tokens
        ("xhigh", 16384, 16384),  # whole budget
        ("max", 16384, 16384),  # whole budget
    ],
)
def test_anthropic_native_knob_matches_legacy(
    effort: str, max_tokens: int, expected: int
) -> None:
    provider = AnthropicReasoningTier()
    assert provider.native_knob(effort, max_tokens) == expected
    # And the adapter wrapper delegates to exactly the same value.
    assert _effort_to_budget(effort, max_tokens) == expected


def test_anthropic_rejects_unsupported_effort() -> None:
    provider = AnthropicReasoningTier()
    with pytest.raises(PermanentLLMError):
        provider.native_knob("minimal", 16384)  # not in ANTHROPIC_EFFORTS


# ── OpenAI provider: validated effort passthrough ──


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh"])
def test_openai_native_knob_passthrough(effort: str) -> None:
    provider = OpenAIReasoningTier()
    assert provider.native_knob(effort, 16384) == effort


def test_openai_rejects_unsupported_effort() -> None:
    provider = OpenAIReasoningTier()
    with pytest.raises(PermanentLLMError):
        provider.native_knob("max", 16384)  # not in OPENAI_EFFORTS


# ── Registry: defaults + swappability ──


def test_registry_registers_both_defaults() -> None:
    registry = reasoning_tier_registry()
    assert set(registry.names()) == {"anthropic", "openai"}
    assert isinstance(registry.get("anthropic"), AnthropicReasoningTier)
    assert isinstance(registry.get("openai"), OpenAIReasoningTier)


def test_default_resolves_to_anthropic() -> None:
    assert isinstance(reasoning_tier_registry().resolve_default(), AnthropicReasoningTier)


def test_providers_satisfy_protocol() -> None:
    assert isinstance(AnthropicReasoningTier(), ReasoningTierProvider)
    assert isinstance(OpenAIReasoningTier(), ReasoningTierProvider)


def test_seam_swappable_with_fake() -> None:
    class FakeTier:
        provider = "fake"

        def native_knob(self, effort: str, max_tokens: int) -> object:
            return f"{effort}:{max_tokens}"

    registry: PluggableRegistry[ReasoningTierProvider] = PluggableRegistry(
        "reasoning_tier", default=("fake", FakeTier)
    )
    assert registry.resolve_default().native_knob("low", 100) == "low:100"
