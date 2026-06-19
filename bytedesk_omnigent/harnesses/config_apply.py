"""Idempotent SOUL.md projection for the Hermes agent.

The Hermes Agent reads its identity/persona from ``<hermes_home>/SOUL.md``.
Omnigent owns the canonical persona as the agent spec's ``system_prompt``,
which can change when a fresh/updated spec is pulled. ``apply_spec_to_hermes``
reconciles ``SOUL.md`` to the current system prompt, hashing the prompt so a
re-apply with an unchanged prompt is a no-op (no rewrite, no churn).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)

_SOUL_FILE = "SOUL.md"
_APPLIED_VERSION_FILE = ".applied-version"


def _default_hermes_home() -> Path:
    return Path.home() / ".hermes"


def _hash_prompt(system_prompt: str) -> str:
    """Return a stable hex digest of *system_prompt* for change detection."""
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def apply_spec_to_hermes(
    system_prompt: str,
    *,
    hermes_home: Path | None = None,
) -> bool:
    """Project *system_prompt* onto ``<hermes_home>/SOUL.md`` idempotently.

    Hashes *system_prompt* and compares it to the digest recorded in
    ``<hermes_home>/.applied-version``. If they differ (or no record exists),
    ``SOUL.md`` is (re)written and the recorded hash updated.

    :param system_prompt: The agent spec's system prompt (Hermes persona).
    :param hermes_home: Hermes home directory; defaults to ``~/.hermes``.
    :returns: ``True`` when ``SOUL.md`` was (re)written, ``False`` on a no-op
        because the prompt already matched the recorded hash.
    """
    home = hermes_home if hermes_home is not None else _default_hermes_home()
    digest = _hash_prompt(system_prompt)

    applied_path = home / _APPLIED_VERSION_FILE
    try:
        recorded = applied_path.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""

    if recorded == digest:
        return False

    home.mkdir(parents=True, exist_ok=True)
    (home / _SOUL_FILE).write_text(system_prompt, encoding="utf-8")
    applied_path.write_text(digest, encoding="utf-8")
    return True
