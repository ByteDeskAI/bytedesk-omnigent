"""Shared provider error taxonomy for pluggable seams (BDP-2345, sweep-3 item E).

Every seam built on :mod:`omnigent.pluggable` raises from this single hierarchy so
callers can catch ``ProviderError`` once instead of a per-seam exception zoo. The
four subclasses name the four distinct failure modes a seam hits:

- :class:`ProviderNotRegistered` — the requested provider name isn't registered
  (typo, missing extension, or an override env naming an unknown impl).
- :class:`ProviderUnconfigured` — the provider exists but lacks required
  configuration (credentials/URI/etc.) to operate.
- :class:`ProviderUnavailable` — the provider is configured but can't serve right
  now (remote unreachable, optional dependency absent at runtime).
- :class:`RegistryConflict` — two impls claim the same name in one seam.
"""

from __future__ import annotations

from collections.abc import Iterable


class ProviderError(Exception):
    """Base for every pluggable-seam provider failure."""


class ProviderNotRegistered(ProviderError):
    """No provider is registered under *name* for *seam*."""

    def __init__(
        self, seam: str, name: str, *, known: Iterable[str] | None = None
    ) -> None:
        self.seam = seam
        self.name = name
        self.known = sorted(known) if known is not None else []
        if self.known:
            msg = (
                f"no provider {name!r} registered for seam {seam!r}; "
                f"known: {', '.join(self.known)}"
            )
        else:
            msg = f"no provider {name!r} registered for seam {seam!r} (none registered)"
        super().__init__(msg)


class ProviderUnconfigured(ProviderError):
    """The provider for *seam*/*name* is missing required configuration."""

    def __init__(self, seam: str, name: str, detail: str = "") -> None:
        self.seam = seam
        self.name = name
        msg = f"provider {name!r} for seam {seam!r} is unconfigured"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


class ProviderUnavailable(ProviderError):
    """The provider for *seam*/*name* is configured but cannot serve right now."""

    def __init__(self, seam: str, name: str, detail: str = "") -> None:
        self.seam = seam
        self.name = name
        msg = f"provider {name!r} for seam {seam!r} is unavailable"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


class RegistryConflict(ProviderError):
    """Two providers claim the same *name* within *seam*."""

    def __init__(self, seam: str, name: str) -> None:
        self.seam = seam
        self.name = name
        super().__init__(
            f"provider {name!r} is already registered for seam {seam!r}"
        )


__all__ = [
    "ProviderError",
    "ProviderNotRegistered",
    "ProviderUnconfigured",
    "ProviderUnavailable",
    "RegistryConflict",
]
