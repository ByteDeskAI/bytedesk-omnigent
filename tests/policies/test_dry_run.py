"""Tests for the dry-run preview gate (BDP-2277 F6, ADR-0142)."""
from __future__ import annotations

from omnigent.policies.builtins.dry_run import dry_run_preview


def _tool_call(name: str, arguments: dict | None = None) -> dict:
    return {"type": "tool_call", "data": {"name": name, "arguments": arguments or {}}}


def test_asks_with_concrete_preview_for_matched_tool() -> None:
    evaluate = dry_run_preview(["billing\\.charge"])
    result = evaluate(_tool_call("billing.charge", {"amount": 500, "customer": "c1"}))
    assert result["result"] == "ASK"
    # The preview surfaces the exact tool + its arguments.
    assert "billing.charge" in result["reason"]
    assert "500" in result["reason"]
    assert "c1" in result["reason"]


def test_allows_non_matching_tool() -> None:
    evaluate = dry_run_preview(["billing\\.charge"])
    assert evaluate(_tool_call("read.file"))["result"] == "ALLOW"


def test_allows_non_tool_call_events() -> None:
    evaluate = dry_run_preview(["billing\\.charge"])
    assert evaluate({"type": "llm_call"})["result"] == "ALLOW"


def test_truncates_an_oversized_preview() -> None:
    evaluate = dry_run_preview(["wipe\\.all"], max_preview_chars=30)
    result = evaluate(_tool_call("wipe.all", {"blob": "y" * 200}))
    assert result["result"] == "ASK"
    assert "truncated" in result["reason"]
