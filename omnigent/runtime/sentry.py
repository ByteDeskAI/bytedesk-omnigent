"""
Optional Sentry error + performance telemetry for the omnigent runtime.

This is the second observability plane alongside ``omnigent.runtime.telemetry``
(MLflow/OTel tracing). It is **additive and opt-in**: a no-op unless
``OMNIGENT_SENTRY_DSN`` is set, so OSS/dev installs are unaffected. It is
initialized once per process at each of the three omnigent entrypoints —
``server`` (FastAPI control plane), ``host`` (per-pod tunnel), and ``runner``
(subprocess) — so unhandled errors and performance transactions from all three
land in the self-hosted ByteDesk Observability project (a Sentry-compatible
ingest at ``observability.bytedesk.svc.cluster.local``).

**Content scrubbing (default on).** Agent prompts, responses, and tool payloads
must not leave the process. The dominant leak vector in an agent runtime is
stack-frame *local variables* (they hold ``messages`` / ``prompt`` / ``response``
objects), so ``include_local_variables=False`` is the primary control, backed by
``send_default_pii=False`` (which also disables the OpenAI/Anthropic
auto-integrations' prompt capture), ``max_request_body_size="never"``, and the
``before_send`` / ``before_send_transaction`` scrubbers below which drop request
bodies and per-span data. Operators can opt back into full capture with
``OMNIGENT_OTEL_CAPTURE_CONTENT=true`` (the same switch the OTel plane reads),
which keeps error type/stack/route/metadata either way.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

# Request-event keys that may carry user/agent content (bodies, query content,
# session cookies). Dropped from both error events and transactions.
_CONTENT_REQUEST_KEYS = ("data", "query_string", "cookies")

_DEFAULT_TRACES_SAMPLE_RATE = 0.1


def _env_bool(name: str) -> bool:
    """Return ``True`` when *name* is set to a truthy value (``true``/``1``/``yes``)."""
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes")


def _env_float(name: str, default: float) -> float:
    """Parse *name* as a float, returning *default* when unset or unparseable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def content_capture_enabled() -> bool:
    """
    Return whether prompt/response/message content may be retained on events.

    Reads ``OMNIGENT_OTEL_CAPTURE_CONTENT`` directly (not the telemetry module
    global) so the decision is correct regardless of whether ``telemetry.init``
    has run in this process — the host and runner entrypoints do not call it.
    """
    return _env_bool("OMNIGENT_OTEL_CAPTURE_CONTENT")


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """
    ``before_send`` hook: drop request bodies/content from an error event.

    Keeps the exception (type/value/stack — stack locals are already suppressed
    by ``include_local_variables=False``), tags, and the route/transaction name.

    :param event: The Sentry error event payload.
    :param hint: Sentry's hint dict (unused; the SDK passes it positionally).
    :returns: The mutated *event* (never dropped — errors always send).
    """
    request = event.get("request")
    if isinstance(request, dict):
        for key in _CONTENT_REQUEST_KEYS:
            request.pop(key, None)
    return event


def _scrub_transaction(
    event: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any]:
    """
    ``before_send_transaction`` hook: drop per-span ``data`` payloads.

    Span ``data`` can carry gen-ai request/response messages and HTTP bodies;
    ``op``, ``description``, and timing (the performance signal) are kept.

    :param event: The Sentry transaction event payload.
    :param hint: Sentry's hint dict (unused).
    :returns: The mutated *event*.
    """
    spans = event.get("spans")
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, dict):
                span.pop("data", None)
    request = event.get("request")
    if isinstance(request, dict):
        for key in _CONTENT_REQUEST_KEYS:
            request.pop(key, None)
    return event


def resolve_options(dsn: str) -> dict[str, Any]:
    """
    Build the ``sentry_sdk.init`` keyword options from the environment.

    Pure (no ``sentry_sdk`` import) so option resolution + scrubbing wiring are
    unit-testable without the SDK installed.

    :param dsn: The resolved, non-empty Sentry DSN.
    :returns: Keyword options for ``sentry_sdk.init``.
    """
    capture_content = content_capture_enabled()
    environment = (
        os.environ.get("OMNIGENT_SENTRY_ENVIRONMENT")
        or os.environ.get("SENTRY_ENVIRONMENT")
        or "production"
    ).strip()
    release = (
        os.environ.get("OMNIGENT_SENTRY_RELEASE")
        or os.environ.get("SENTRY_RELEASE")
        or ""
    ).strip() or None

    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "traces_sample_rate": _env_float(
            "OMNIGENT_SENTRY_TRACES_SAMPLE_RATE", _DEFAULT_TRACES_SAMPLE_RATE
        ),
        "send_default_pii": False,
        # Stack-frame locals hold the prompts/responses — the dominant content
        # leak in an agent runtime. Off unless content capture is explicitly on.
        "include_local_variables": capture_content,
        "max_request_body_size": "always" if capture_content else "never",
        "before_send": None if capture_content else _scrub_event,
        "before_send_transaction": None if capture_content else _scrub_transaction,
    }


def init_sentry(component: str) -> bool:
    """
    Initialize Sentry telemetry for one omnigent process. Safe + idempotent.

    No-op (returns ``False``) when ``OMNIGENT_SENTRY_DSN`` is unset/blank or when
    ``sentry-sdk`` is not installed — so the feature is opt-in and degrades
    quietly. The FastAPI/Starlette and HTTPX integrations auto-enable when those
    libraries are importable (the server gets request-context error capture for
    free).

    :param component: ``"server"`` / ``"host"`` / ``"runner"`` — attached as the
        ``omnigent.component`` tag so operators can filter by process.
    :returns: ``True`` when Sentry was initialized, ``False`` on the no-op path.
    """
    dsn = os.environ.get("OMNIGENT_SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        _logger.info(
            "sentry-sdk not installed; error telemetry disabled "
            "(install sentry-sdk to enable OMNIGENT_SENTRY_DSN reporting)."
        )
        return False

    options = resolve_options(dsn)
    sentry_sdk.init(**options)
    sentry_sdk.set_tag("omnigent.component", component)
    _logger.info(
        "omnigent Sentry telemetry initialized "
        "(component=%s, environment=%s, traces=%.2f, scrub_content=%s)",
        component,
        options["environment"],
        options["traces_sample_rate"],
        options["before_send"] is not None,
    )
    return True
