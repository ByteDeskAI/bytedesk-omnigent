"""Overflow-detector seam tests (BDP-2360, P6).

Proves the ``overflow_detector`` PluggableRegistry + chain: each provider's
detector parses its own 400 body shape, the chain tries each in order, unknown
bodies return ``None``, and the seam is swappable. Token-count arithmetic and
regexes are byte-identical to the prior inlined parsers, including the
``llm_retry._detect_context_overflow`` wrapper.
"""

from __future__ import annotations

import json

import pytest

from omnigent.pluggable import PluggableRegistry
from omnigent.runtime.llm_retry import _detect_context_overflow
from omnigent.runtime.overflow_detect import (
    AnthropicOverflowDetector,
    GeminiOverflowDetector,
    OpenAIOverflowDetector,
    OverflowDetector,
    OverflowTokens,
    detect_overflow,
    overflow_detector_registry,
)

_OPENAI_BODY = json.dumps(
    {
        "error": {
            "code": "context_length_exceeded",
            "message": (
                "This model's maximum context length is 128000 tokens. "
                "However, you requested 142000 tokens."
            ),
        }
    }
)
_ANTHROPIC_SUM_BODY = "input length and max_tokens exceed: 130000 + 4096 > 128000"
_ANTHROPIC_LONG_BODY = "prompt is too long: 142000 tokens > 128000 maximum"
_GEMINI_BODY = (
    "input token count (142000) exceeds the maximum number of tokens allowed (128000)"
)


# ── Per-provider detectors ──


def test_openai_detector() -> None:
    got = OpenAIOverflowDetector().detect(_OPENAI_BODY)
    assert got == OverflowTokens(max_context_tokens=128000, actual_tokens=142000)


def test_anthropic_detector_sum_form() -> None:
    got = AnthropicOverflowDetector().detect(_ANTHROPIC_SUM_BODY)
    # actual = input + max_tokens = 130000 + 4096
    assert got == OverflowTokens(max_context_tokens=128000, actual_tokens=134096)


def test_anthropic_detector_long_form() -> None:
    got = AnthropicOverflowDetector().detect(_ANTHROPIC_LONG_BODY)
    assert got == OverflowTokens(max_context_tokens=128000, actual_tokens=142000)


def test_gemini_detector() -> None:
    got = GeminiOverflowDetector().detect(_GEMINI_BODY)
    assert got == OverflowTokens(max_context_tokens=128000, actual_tokens=142000)


def test_detector_returns_none_on_foreign_body() -> None:
    assert OpenAIOverflowDetector().detect(_GEMINI_BODY) is None
    assert GeminiOverflowDetector().detect(_OPENAI_BODY) is None


# ── Chain ──


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_OPENAI_BODY, OverflowTokens(128000, 142000)),
        (_ANTHROPIC_SUM_BODY, OverflowTokens(128000, 134096)),
        (_ANTHROPIC_LONG_BODY, OverflowTokens(128000, 142000)),
        (_GEMINI_BODY, OverflowTokens(128000, 142000)),
    ],
)
def test_chain_detects_each_provider(body: str, expected: OverflowTokens) -> None:
    assert detect_overflow(body) == expected


def test_chain_returns_none_for_unknown_400() -> None:
    assert detect_overflow('{"error": {"code": "invalid_request_error"}}') is None
    assert detect_overflow("totally unrelated error text") is None
    assert detect_overflow("") is None


def test_wrapper_matches_chain() -> None:
    # llm_retry._detect_context_overflow delegates byte-identically.
    for body in (_OPENAI_BODY, _ANTHROPIC_SUM_BODY, _ANTHROPIC_LONG_BODY, _GEMINI_BODY):
        assert _detect_context_overflow(body) == detect_overflow(body)
    assert _detect_context_overflow("nope") is None


# ── Registry + swappability ──


def test_registry_registers_all_detectors() -> None:
    registry = overflow_detector_registry()
    assert set(registry.names()) == {"openai", "anthropic", "gemini"}


def test_detectors_satisfy_protocol() -> None:
    assert isinstance(OpenAIOverflowDetector(), OverflowDetector)
    assert isinstance(AnthropicOverflowDetector(), OverflowDetector)
    assert isinstance(GeminiOverflowDetector(), OverflowDetector)


def test_chain_runs_extension_detector_with_fake_registry() -> None:
    class FakeDetector:
        provider = "fake"

        def detect(self, body: str) -> OverflowTokens | None:
            if body == "fake-overflow":
                return OverflowTokens(max_context_tokens=1, actual_tokens=2)
            return None

    registry: PluggableRegistry[OverflowDetector] = PluggableRegistry(
        "overflow_detector", default=("fake", FakeDetector)
    )
    assert detect_overflow("fake-overflow", registry=registry) == OverflowTokens(1, 2)
    assert detect_overflow("nope", registry=registry) is None
