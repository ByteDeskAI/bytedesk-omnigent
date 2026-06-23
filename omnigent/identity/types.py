"""Small value objects returned by the identity ports.

Tiny frozen dataclasses kept separate from the ports so both the ports and the
implementations can import them without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Credential:
    """An outbound credential a tool presents to an integration.

    :param header_name: The HTTP header to set (default ``Authorization``).
    :param header_value: The fully-formed header value (e.g. ``"Bearer ey…"``).
    :param expires_at: Unix epoch seconds when the credential lapses, or ``None``
        for a non-expiring (e.g. static API-key) credential.
    """

    header_value: str
    header_name: str = "Authorization"
    expires_at: float | None = None

    @property
    def header(self) -> dict[str, str]:
        """The credential as a one-entry header mapping."""
        return {self.header_name: self.header_value}


@dataclass(frozen=True)
class Decision:
    """An authorization decision.

    :param allowed: Whether the action is permitted.
    :param reason: Human-readable rationale (for audit/logging).
    """

    allowed: bool
    reason: str = ""
