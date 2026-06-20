"""LLM call retry logic with exponential backoff.

Classifies adapter exceptions as retryable or permanent, computes
backoff delays, and provides a retry loop that emits SSE events.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import httpx

from omnigent.llms.errors import (
    ContextWindowExceededError,
    LLMErrorDetail,
    PermanentLLMError,
    RetryableLLMError,
)
from omnigent.runtime.overflow_detect import OverflowTokens, detect_overflow
from omnigent.spec.types import RetryPolicy

_logger = logging.getLogger(__name__)

T = TypeVar("T")

# Backwards-compat alias: ``_OverflowTokens`` was the local dataclass before
# the overflow parsers were lifted into the pluggable ``overflow_detector``
# seam (BDP-2360, P6). Kept so any importer of the old name keeps working.
_OverflowTokens = OverflowTokens


def _detect_context_overflow(body: str) -> OverflowTokens | None:
    """
    Parse provider-specific context-overflow error messages and
    extract token counts.

    Thin wrapper over the ``overflow_detector`` pluggable seam
    (:func:`omnigent.runtime.overflow_detect.detect_overflow`, BDP-2360):
    the OpenAI / Anthropic / Gemini parsers that used to be inlined here are
    now registered detectors tried as a chain. Behavior is byte-identical —
    same regexes, same conservative "unknown 400 → ``None``" handling.

    :param body: The raw HTTP response body string from the provider.
    :returns: Parsed token counts, or ``None`` if the body does not
        match any known overflow pattern.
    """
    return detect_overflow(body)


def classify_llm_error(
    exc: Exception,
    retryable_status_codes: Sequence[int],
) -> RetryableLLMError | PermanentLLMError:
    """
    Classify an adapter exception as retryable or permanent.

    :param exc: The exception raised by the LLM adapter. Typically
        ``httpx.TimeoutException`` or ``httpx.HTTPStatusError``.
    :param retryable_status_codes: HTTP status codes configured as
        retryable, e.g. ``[429, 500, 502, 503]``.
    :returns: A :class:`RetryableLLMError` or
        :class:`PermanentLLMError`.
    """
    if isinstance(exc, httpx.TimeoutException):
        return RetryableLLMError(
            f"LLM request timed out: {exc}",
            code="timeout",
            detail=LLMErrorDetail(),
        )

    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_http_error(exc, retryable_status_codes)

    # Transport-level connection failures: tunnel disconnects raise
    # bare ``ConnectionError`` (an ``OSError`` subclass), httpx
    # network errors surface as ``httpx.NetworkError``.  Both are
    # transient — the runner reconnects with backoff and a retry
    # will find it back online.
    if isinstance(exc, (ConnectionError, httpx.NetworkError)):
        return RetryableLLMError(
            f"LLM call failed (transient): {exc}",
            code="connection_error",
            detail=LLMErrorDetail(),
        )

    # Anything else (programming errors, unexpected exceptions) is
    # treated as permanent so it surfaces immediately.
    return PermanentLLMError(
        f"LLM call failed: {exc}",
        code="unknown_error",
        detail=LLMErrorDetail(),
    )


def _classify_http_error(
    exc: httpx.HTTPStatusError,
    retryable_status_codes: Sequence[int],
) -> RetryableLLMError | PermanentLLMError:
    """
    Classify an HTTP status error as retryable or permanent.

    HTTP 400 is checked for context-window overflow before the
    generic retryable/permanent split. This allows the executor's
    retry logic to surface ``ContextWindowExceededError`` so the
    workflow can compact and retry.

    :param exc: The HTTP status error from httpx.
    :param retryable_status_codes: Status codes that trigger retry.
    :returns: A :class:`RetryableLLMError`,
        :class:`ContextWindowExceededError`, or
        :class:`PermanentLLMError`.
    """
    status = exc.response.status_code
    body = _safe_response_text(exc.response)
    detail = LLMErrorDetail(status_code=status, response_body=body)
    code = str(status)
    message = f"LLM returned HTTP {status}: {body}"

    # HTTP 400 may be a context-window overflow — check before
    # the generic split so the workflow can compact-retry.
    if status == 400:
        overflow = _detect_context_overflow(body)
        if overflow is not None:
            return ContextWindowExceededError(
                f"Context window exceeded: "
                f"{overflow.actual_tokens} tokens "
                f"> {overflow.max_context_tokens} max",
                code="context_length_exceeded",
                detail=detail,
                max_context_tokens=overflow.max_context_tokens,
                actual_tokens=overflow.actual_tokens,
            )

    if status in retryable_status_codes:
        return RetryableLLMError(message, code=code, detail=detail)
    return PermanentLLMError(message, code=code, detail=detail)


def compute_backoff_delay(
    attempt_index: int,
    backoff_base_s: float,
    backoff_max_s: float,
) -> float:
    """
    Compute the backoff delay with jitter for a retry attempt.

    Standalone helper kept for backwards-compat with tests and a
    small number of remaining callers. New code should construct
    a :class:`RetryPolicy` and call its ``compute_backoff_delay``.

    :param attempt_index: Zero-based retry index (0 = first retry),
        e.g. ``0``.
    :param backoff_base_s: Exponential backoff base in seconds, e.g.
        ``2.0``.
    :param backoff_max_s: Maximum delay cap in seconds, e.g. ``30.0``.
    :returns: Delay in seconds with jitter applied, e.g. ``1.47``.
    """
    # Delegates to the registered ``backoff_policy`` Strategy (BDP-2361, P9)
    # so the exp-full-jitter curve lives in one place. ``attempt_index`` is
    # already the 0-based exponent this curve uses; jitter is always on, no
    # server retry-after hint on this path (matching the prior inline math).
    from omnigent.runtime.backoff import default_backoff_policy

    return default_backoff_policy().compute_delay(
        attempt_index, backoff_base_s, backoff_max_s
    )


def _safe_response_text(response: httpx.Response) -> str:
    """
    Safely extract response body text, truncating if very long.

    :param response: The httpx response object.
    :returns: Response body text, truncated to 1000 chars.
    """
    try:
        text = response.text
    except Exception:
        return "<unreadable response body>"
    if len(text) > 1000:
        return text[:1000] + "..."
    return text


def detail_to_dict(
    detail: LLMErrorDetail | None,
) -> dict[str, Any] | None:
    """
    Convert an :class:`LLMErrorDetail` to a JSON-serializable dict.

    :param detail: The error detail, or ``None``.
    :returns: Dict with non-None fields, or ``None``.
    """
    if detail is None:
        return None
    result: dict[str, Any] = {}
    if detail.provider is not None:
        result["provider"] = detail.provider
    if detail.status_code is not None:
        result["status_code"] = detail.status_code
    if detail.response_body is not None:
        result["response_body"] = detail.response_body
    # Empty dict → None to keep SSE JSON payload clean.
    return result or None


def execute_with_retry(
    call_fn: Callable[[], T],
    retry_policy: RetryPolicy,
    on_retry: Callable[[dict[str, Any]], None],
) -> T:
    """
    Execute ``call_fn`` with retry on transient failures.

    Called *inside* a ``@step`` boundary so retries don't cause
    duplicate checkpoints. Emits ``response.retry`` SSE events
    via ``on_retry`` before each backoff sleep. Total tries:
    ``1 + retry_policy.max_retries``.

    :param call_fn: Zero-argument callable that performs the LLM
        call. Raises httpx exceptions on failure.
    :param retry_policy: Retry policy from the agent's LLM config.
    :param on_retry: Callback to emit a ``response.retry`` SSE event.
        Called with the event dict before sleeping.
    :returns: The successful result from ``call_fn``.
    :raises PermanentLLMError: On non-retryable errors.
    :raises RetryableLLMError: When all retry attempts are exhausted.
    """
    last_error: RetryableLLMError | None = None
    total_tries = retry_policy.max_retries + 1

    for attempt in range(total_tries):
        try:
            return call_fn()
        except RetryableLLMError as exc:
            # Pre-classified retryable (mirrors the async variant —
            # see its docstring for the rationale around skipping
            # ``classify_llm_error`` on this path).
            last_error = exc
            if attempt + 1 < total_tries:
                _emit_retry_and_sleep(attempt, retry_policy, exc, on_retry)
        except PermanentLLMError:
            raise
        except Exception as exc:
            classified = classify_llm_error(exc, retry_policy.retryable_status_codes)
            if isinstance(classified, PermanentLLMError):
                raise classified from exc

            last_error = classified
            if attempt + 1 < total_tries:
                _emit_retry_and_sleep(attempt, retry_policy, classified, on_retry)

    # All retries exhausted.
    assert last_error is not None
    raise last_error


def _emit_retry_and_sleep(
    attempt: int,
    retry_policy: RetryPolicy,
    error: RetryableLLMError,
    on_retry: Callable[[dict[str, Any]], None],
) -> None:
    """
    Emit a retry SSE event and sleep for the backoff delay.

    :param attempt: Current zero-based attempt index, e.g. ``0``
        for the first attempt that just failed.
    :param retry_policy: Retry policy with backoff parameters.
    :param error: The classified retryable error.
    :param on_retry: Callback to emit the ``response.retry``
        SSE event dict.
    """
    delay = retry_policy.compute_backoff_delay(retry_index=attempt + 1)
    total_tries = retry_policy.max_retries + 1
    event: dict[str, Any] = {
        "type": "response.retry",
        "source": "llm",
        "attempt": attempt + 2,  # 1-based count of upcoming attempt
        "max_attempts": total_tries,
        "delay_seconds": round(delay, 2),
        "error": {
            "code": error.code,
            "message": str(error),
            "detail": detail_to_dict(error.detail),
        },
    }
    on_retry(event)
    _logger.info(
        "LLM retry %d/%d after %.1fs: %s",
        attempt + 2,
        total_tries,
        delay,
        error.code,
    )
    time.sleep(delay)
