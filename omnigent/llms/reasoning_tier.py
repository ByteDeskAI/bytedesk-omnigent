"""Reasoning-tier providers — effort→native-knob mapping per provider (BDP-2360, P5).

Each LLM provider exposes reasoning effort through a *different* native knob:
Anthropic takes a ``thinking.budget_tokens`` integer, OpenAI takes a native
``effort`` string passed straight through. The mapping used to be an inlined
``match`` on effort inside the Anthropic adapter (``_effort_to_budget``).

This module turns that into the canonical pluggable seam (``reasoning_tier``):

- :class:`ReasoningTierProvider` — the Protocol each provider implements; it
  declares its own effort→native-knob map via :meth:`native_knob`.
- :func:`reasoning_tier_registry` — a :class:`~omnigent.kernel.pluggable.PluggableRegistry`
  with the Anthropic + OpenAI defaults registered in-module (no extension
  discovery at import; the runner hot path stays fastapi-free).

Behavior is byte-identical to the historical ``_effort_to_budget`` for Anthropic
(same budget caps, same effort validation) and to the OpenAI Responses path's
effort passthrough.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnigent.kernel.pluggable import PluggableRegistry
from omnigent.reasoning_effort import (
    ANTHROPIC_EFFORTS,
    OPENAI_EFFORTS,
    validate_effort_or_llm_error,
)

# ── Provider seam ─────────────────────────────────────────


@runtime_checkable
class ReasoningTierProvider(Protocol):
    """A provider's reasoning-effort → native-knob mapping.

    Each provider declares which effort strings it supports and how an effort
    maps to the native API knob the provider expects (an int budget for
    Anthropic, an effort string for OpenAI).
    """

    provider: str

    def native_knob(self, effort: str, max_tokens: int) -> object:
        """Map a reasoning *effort* to this provider's native knob value.

        :param effort: Reasoning effort string (``"low"``/``"medium"``/…).
            Validated against the provider's supported set.
        :param max_tokens: The request's ``max_tokens`` setting, used by
            providers (Anthropic) whose budget is capped by it.
        :returns: The native knob value — an ``int`` budget for Anthropic, a
            validated effort ``str`` for OpenAI.
        """
        ...


# ── Built-in providers ────────────────────────────────────


class AnthropicReasoningTier:
    """Anthropic: effort → ``thinking.budget_tokens`` integer.

    Byte-identical to the historical ``_effort_to_budget``: the budget is
    capped both by a per-tier ceiling and by the request ``max_tokens``.
    """

    provider = "anthropic"

    def native_knob(self, effort: str, max_tokens: int) -> int:
        """Map *effort* to a thinking budget (token count)."""
        effort = validate_effort_or_llm_error(effort, "Anthropic", ANTHROPIC_EFFORTS)
        match effort:
            case "low":
                return min(1024, max_tokens)
            case "medium":
                return min(4096, max_tokens)
            case "high":
                return min(8192, max_tokens)
            case "xhigh" | "max":
                return max_tokens
            case _:
                raise ValueError(f"Unsupported Anthropic reasoning effort: {effort}")


class OpenAIReasoningTier:
    """OpenAI: effort → native effort string (validated passthrough).

    OpenAI's Responses API takes the effort string directly; the native knob
    is the validated effort itself, so ``max_tokens`` is unused.
    """

    provider = "openai"

    def native_knob(self, effort: str, max_tokens: int) -> str:
        """Validate *effort* and return it unchanged (native effort knob)."""
        validated = validate_effort_or_llm_error(effort, "OpenAI", OPENAI_EFFORTS)
        if validated is None:
            raise ValueError(f"Unsupported OpenAI reasoning effort: {effort!r}")
        return validated


# ── Registry ──────────────────────────────────────────────


def reasoning_tier_registry() -> PluggableRegistry[ReasoningTierProvider]:
    """Build the ``reasoning_tier`` seam registry with built-in providers.

    Anthropic is the registered default (it is the only provider whose adapter
    previously inlined the mapping); OpenAI is registered alongside. Extensions
    may contribute providers via a ``reasoning_tier_providers`` hook at a
    server-side composition root — discovery is intentionally *not* run here so
    importing this module stays off the fastapi-heavy path.

    :returns: A registry keyed by provider name.
    """
    registry: PluggableRegistry[ReasoningTierProvider] = PluggableRegistry(
        "reasoning_tier", default=("anthropic", AnthropicReasoningTier)
    )
    registry.register("openai", OpenAIReasoningTier)
    return registry


__all__ = [
    "AnthropicReasoningTier",
    "OpenAIReasoningTier",
    "ReasoningTierProvider",
    "reasoning_tier_registry",
]
