"""Structure + invariant tests for the fork/upstream rebase strategy doc.

Pins the load-bearing parts of ``docs/architecture/fork-rebase-strategy.md``
(BDP-2323 Phase −1, BDP-2325) so a future edit can't silently drop the
landing-order invariant, the two shared-file rules, the rebase sync points, or
the ADR-0145 authority reference. The doc is the policy the abstraction roadmap
is sequenced against, so the wording that other phases rely on is asserted here.
"""

from __future__ import annotations

from pathlib import Path

_DOC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "architecture"
    / "fork-rebase-strategy.md"
)


def _text() -> str:
    return _DOC.read_text(encoding="utf-8")


def test_doc_exists_and_nonempty() -> None:
    assert _DOC.is_file(), f"missing strategy doc at {_DOC}"
    assert _text().strip(), "strategy doc is empty"


def test_references_adr_0145_authority() -> None:
    assert "ADR-0145" in _text(), "doc must cite ADR-0145 as the roadmap authority"


def test_names_both_remotes() -> None:
    text = _text()
    # The fork (origin) and the tracked upstream must both be named so the
    # rebase direction is unambiguous.
    assert "origin" in text and "upstream" in text
    assert "ByteDeskAI/bytedesk-omnigent" in text
    assert "omnigent-ai/omnigent" in text


def test_states_the_two_shared_file_rules() -> None:
    text = _text()
    # Rule 1 (minimize changed lines) and Rule 2 (additive append).
    assert "minimize changed lines" in text.lower()
    assert "additive-append" in text.lower() or "additive append" in text.lower()
    # The append marker convention is what makes the fork delta greppable.
    assert "bytedesk(" in text


def test_classifies_every_phase_boundary() -> None:
    text = _text()
    # Each phase row declares one of the two boundaries.
    assert "bytedesk_omnigent`-only" in text
    assert "shared" in text.lower()
    # The two shared phases are explicitly the spec-field and server-wiring ones.
    assert "AgentSpec" in text
    assert "install_extensions" in text


def test_pins_the_landing_order_invariant() -> None:
    text = _text().lower()
    # The one ordering invariant: shared phases (6, 7) land last, after a sync.
    assert "land last" in text or "lands last" in text or "land in any order" in text
    assert "phases (6, 7)" in text or "phase 6/7" in text or "phases 6 and 7" in text


def test_defines_three_rebase_sync_points() -> None:
    text = _text()
    assert "sync point" in text.lower()
    assert "git rebase" in text and "upstream/main" in text
    # The fork-delta audit grep is the concrete mechanism, not just prose.
    assert "git grep" in text


def test_enforces_dual_db_conventions() -> None:
    text = _text()
    # JSON-in-Text, never native JSONB; soft FKs; ABC+impl+converter triad.
    assert "JSON-in-`Text`" in text or "JSON-in-Text" in text
    assert "JSONB" in text  # named as the banned form
    assert "soft FK" in text
    assert "sql_X_to_entity" in text
