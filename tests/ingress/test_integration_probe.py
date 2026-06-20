"""Tests for deterministic webhook smoke-test probe compilation."""

from __future__ import annotations

import hashlib
import hmac
import json

from bytedesk_omnigent.integration_probe import compile_webhook_probe


def test_compile_webhook_probe_signs_canonical_body_and_curl() -> None:
    """A generated probe is deterministic and exercises the ingress contract.

    Operators need a copy/paste smoke test before activating a third-party
    webhook binding. The compiler must produce the exact body, HMAC signature,
    event header, and expected status hints without contacting the provider.
    """
    probe = compile_webhook_probe(
        source="github",
        match_key="issues.opened",
        secret="whsec_test",
        payload={"issue": {"number": 42}, "action": "opened"},
        base_url="https://omnigent.example.com/v1",
    )

    assert probe.url == "https://omnigent.example.com/v1/ingress/github"
    assert probe.body == '{"action":"opened","issue":{"number":42}}'
    expected_signature = hmac.new(
        b"whsec_test", probe.body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert probe.headers == {
        "content-type": "application/json",
        "x-omnigent-event": "issues.opened",
        "x-omnigent-signature": expected_signature,
    }
    assert probe.expected_statuses == {
        202: "binding exists and parked signal was delivered",
        401: "signature/header mismatch or wrong secret",
        404: "source, binding, or pending wait is not configured",
        409: "event replayed after the signal was already resolved",
        410: "parked wait expired before delivery",
    }
    assert (
        "curl -fsS -X POST 'https://omnigent.example.com/v1/ingress/github'"
        in probe.curl_command
    )
    assert "-H 'x-omnigent-event: issues.opened'" in probe.curl_command
    assert f"-H 'x-omnigent-signature: {expected_signature}'" in probe.curl_command
    assert "--data '{\"action\":\"opened\",\"issue\":{\"number\":42}}'" in probe.curl_command


def test_compile_webhook_probe_accepts_raw_body_for_provider_native_replay() -> None:
    """Provider payload captures can be replayed byte-for-byte when needed."""
    raw_body = '{"z":2, "a":1}'
    probe = compile_webhook_probe(
        source="teamcity",
        match_key="build.finished",
        secret="teamcity-secret",
        raw_body=raw_body,
        base_url="http://localhost:8000/v1/",
    )

    assert probe.url == "http://localhost:8000/v1/ingress/teamcity"
    assert probe.body == raw_body
    assert json.loads(probe.body) == {"z": 2, "a": 1}
