"""Context-overflow detectors — one per provider, tried as a chain (BDP-2360, P6).

A provider rejects an over-long request with an HTTP 400 whose body shape is
provider-specific. ``llm_retry._detect_context_overflow`` used to inline four
parsers (OpenAI / two Anthropic shapes / Gemini) in sequence. This module turns
each into a registered :class:`OverflowDetector` and runs them as a chain:

- :class:`OverflowDetector` — the Protocol; :meth:`detect` returns
  :class:`OverflowTokens` when the body matches its provider's shape, else
  ``None``.
- :func:`overflow_detector_registry` — a
  :class:`~omnigent.pluggable.PluggableRegistry` with the built-in detectors
  registered in-module (no extension discovery at import — the runner hot path
  stays fastapi-free).
- :func:`detect_overflow` — try each registered detector in order; first match
  wins, matching the historical fall-through order exactly.

Behavior is byte-identical to the prior inlined parsers: same regexes, same
"unknown 400 → ``None``" conservatism, same token-count arithmetic (Anthropic's
``input + max_tokens`` sum).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from omnigent.pluggable import PluggableRegistry


@dataclass
class OverflowTokens:
    """Token counts parsed from a provider context-overflow error body.

    :param max_context_tokens: The model's context window size as reported by
        the provider, e.g. ``128000``.
    :param actual_tokens: The token count the provider measured for the
        rejected request, e.g. ``142000``.
    """

    max_context_tokens: int
    actual_tokens: int


@runtime_checkable
class OverflowDetector(Protocol):
    """Parse one provider's context-overflow 400 body into token counts."""

    provider: str

    def detect(self, body: str) -> OverflowTokens | None:
        """Return parsed counts if *body* matches this provider's shape, else ``None``."""
        ...


# ── Built-in detectors ────────────────────────────────────


class OpenAIOverflowDetector:
    """OpenAI: ``{"error": {"code": "context_length_exceeded", ...}}``."""

    provider = "openai"

    def detect(self, body: str) -> OverflowTokens | None:
        try:
            parsed = json.loads(body)
            error_obj = parsed.get("error", {})
            if error_obj.get("code") == "context_length_exceeded":
                msg = error_obj.get("message", "")
                max_m = re.search(r"maximum context length is (\d+) tokens", msg)
                act_m = re.search(r"you requested (\d+) tokens", msg)
                if max_m and act_m:
                    return OverflowTokens(
                        max_context_tokens=int(max_m.group(1)),
                        actual_tokens=int(act_m.group(1)),
                    )
        except (json.JSONDecodeError, AttributeError):
            pass
        return None


class AnthropicOverflowDetector:
    """Anthropic: ``"{input} + {max_tokens} > {limit}"`` or
    ``"prompt is too long: {actual} tokens > {limit} maximum"``."""

    provider = "anthropic"

    def detect(self, body: str) -> OverflowTokens | None:
        # "{input} + {max_tokens} > {limit}" — total request size is
        # input + max_tokens; capture both so actual_tokens reflects the
        # full request (not just the prompt).
        anthropic_sum = re.search(r"(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)", body)
        if anthropic_sum:
            return OverflowTokens(
                max_context_tokens=int(anthropic_sum.group(3)),
                actual_tokens=int(anthropic_sum.group(1)) + int(anthropic_sum.group(2)),
            )

        anthropic_long = re.search(
            r"prompt is too long:\s*(\d+)\s*tokens\s*>\s*(\d+)\s*maximum",
            body,
        )
        if anthropic_long:
            return OverflowTokens(
                max_context_tokens=int(anthropic_long.group(2)),
                actual_tokens=int(anthropic_long.group(1)),
            )
        return None


class GeminiOverflowDetector:
    """Gemini: ``"input token count ({actual}) exceeds ... ({limit})"``."""

    provider = "gemini"

    def detect(self, body: str) -> OverflowTokens | None:
        gemini_match = re.search(
            r"input token count \((\d+)\) exceeds the maximum number"
            r" of tokens allowed \((\d+)\)",
            body,
        )
        if gemini_match:
            return OverflowTokens(
                max_context_tokens=int(gemini_match.group(2)),
                actual_tokens=int(gemini_match.group(1)),
            )
        return None


# ── Registry + chain ──────────────────────────────────────

# Chain order matches the historical inlined fall-through: OpenAI, Anthropic,
# Gemini. The shapes are disjoint, but the order is preserved for parity.
_DETECTOR_CHAIN = ("openai", "anthropic", "gemini")


def overflow_detector_registry() -> PluggableRegistry[OverflowDetector]:
    """Build the ``overflow_detector`` seam registry with built-in detectors.

    OpenAI is the registered default (first in the historical chain); Anthropic
    and Gemini are registered alongside. Extensions may contribute detectors via
    an ``overflow_detector_providers`` hook at a server-side composition root —
    discovery is intentionally *not* run here so importing this module (used on
    the runner retry path) stays off the fastapi-heavy path.

    :returns: A registry keyed by provider name.
    """
    registry: PluggableRegistry[OverflowDetector] = PluggableRegistry(
        "overflow_detector", default=("openai", OpenAIOverflowDetector)
    )
    registry.register("anthropic", AnthropicOverflowDetector)
    registry.register("gemini", GeminiOverflowDetector)
    return registry


def detect_overflow(
    body: str,
    registry: PluggableRegistry[OverflowDetector] | None = None,
) -> OverflowTokens | None:
    """Try each registered detector in chain order; first match wins.

    Matches conservatively — only well-known error shapes produce a result.
    Unknown 400 errors return ``None`` so they propagate as ``PermanentLLMError``
    rather than entering a compact-retry loop.

    :param body: The raw HTTP response body string from the provider.
    :param registry: Optional registry override (for tests / swapping). Defaults
        to the built-in :func:`overflow_detector_registry`.
    :returns: Parsed token counts, or ``None`` if no detector matched.
    """
    reg = registry if registry is not None else overflow_detector_registry()
    for name in _DETECTOR_CHAIN:
        if name not in reg.names():
            continue
        result = reg.get(name).detect(body)
        if result is not None:
            return result
    # Any extension-contributed detectors not in the canonical chain run last.
    for name in reg.names():
        if name in _DETECTOR_CHAIN:
            continue
        result = reg.get(name).detect(body)
        if result is not None:
            return result
    return None


__all__ = [
    "AnthropicOverflowDetector",
    "GeminiOverflowDetector",
    "OpenAIOverflowDetector",
    "OverflowDetector",
    "OverflowTokens",
    "detect_overflow",
    "overflow_detector_registry",
]
