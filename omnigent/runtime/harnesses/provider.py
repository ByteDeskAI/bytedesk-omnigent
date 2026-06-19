"""Consolidated harness-identity registry — one descriptor per harness.

Part of the omnigent core-refactor spine (BDP-2327, Phase 1). Harness
identity is spread across **four** legacy sources today:

1. :data:`omnigent.runtime.harnesses._HARNESS_MODULES` — harness name →
   the Python module that exports ``create_app()`` (includes inline
   alias entries like ``"claude" -> claude_sdk_harness``).
2. :data:`omnigent.harness_aliases.HARNESS_ALIASES` — user-facing alias
   spelling → canonical id — plus :data:`~omnigent.harness_aliases.NATIVE_HARNESSES`,
   the set of canonical native-CLI harness ids.
3. :data:`omnigent.spec._omnigent_compat.OMNIGENT_HARNESSES` — the
   allowlist of harness ids accepted under ``executor.type: omnigent``
   (plus :data:`~omnigent.spec._omnigent_compat.OMNIGENT_HARNESS_ALIASES`).

:class:`HarnessProvider` folds those facets into a single descriptor
(``name``, ``aliases``, ``is_native``, ``module_path``, ``config_schema``)
and :data:`HARNESS_PROVIDERS` is built **at import time from the four
legacy sources**, so it agrees with them by construction rather than
duplicating their contents. :func:`resolve` offers a single lookup that
accepts a canonical id *or* any alias.

This is a strangler-fig sidecar: the four legacy sources stay
authoritative. :func:`resolve` is only consulted when
``OMNIGENT_USE_HARNESS_PROVIDER_REGISTRY`` is on; with the flag off the
registry is still built (cheap, import-time) but no live dispatch path
reads it, so behavior is unchanged. A contract test pins this registry to
the legacy sources so a future edit to either side fails loudly instead
of drifting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omnigent.harness_aliases import HARNESS_ALIASES, NATIVE_HARNESSES
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import (
    OMNIGENT_HARNESS_ALIASES,
    OMNIGENT_HARNESSES,
)


@dataclass(frozen=True)
class HarnessProvider:
    """A single harness's identity, folded from the four legacy sources.

    :param name: Canonical harness id, e.g. ``"claude-sdk"``. Matches the
        value of ``spec.executor.config.harness`` after canonicalization.
    :param aliases: User-facing alias spellings that canonicalize to
        :attr:`name`, e.g. ``("claude",)`` for ``"claude-sdk"``.
    :param is_native: Whether this is a native CLI harness (boots a
        vendor TUI in a terminal); mirrors membership in
        :data:`omnigent.harness_aliases.NATIVE_HARNESSES`.
    :param module_path: Fully-qualified module that exports
        ``create_app()`` for this harness, from
        :data:`~omnigent.runtime.harnesses._HARNESS_MODULES`; ``None``
        when the harness has no registered runner module (e.g.
        ``"open-responses"`` is accepted by the omnigent allowlist but
        resolved by a different executor path).
    :param config_schema: Reserved descriptor for a harness's
        ``executor.config`` schema. Carried for parity with the native
        ``config.schema`` work; ``None`` until a schema is wired.
    """

    name: str
    aliases: tuple[str, ...] = ()
    is_native: bool = False
    module_path: str | None = None
    config_schema: object | None = field(default=None)


def _build_registry() -> dict[str, HarnessProvider]:
    """Fold the four legacy harness-identity sources into descriptors.

    Canonical harness ids are the union of the omnigent allowlist
    (:data:`OMNIGENT_HARNESSES`) and the canonical (non-alias) keys of
    :data:`_HARNESS_MODULES`. For each, the alias list is reverse-derived
    from :data:`HARNESS_ALIASES` (plus any alias key in
    ``_HARNESS_MODULES`` that points at this canonical module), nativeness
    from :data:`NATIVE_HARNESSES`, and the module path from
    ``_HARNESS_MODULES``.

    :returns: Mapping of canonical harness id → :class:`HarnessProvider`.
    """
    # Alias → canonical, across BOTH alias sources, so a name appearing
    # only as an alias (e.g. "claude") is never mistaken for canonical.
    alias_to_canonical: dict[str, str] = dict(HARNESS_ALIASES)

    # _HARNESS_MODULES holds inline aliases (multiple names → same module).
    # The canonical name for a module is the one that is NOT a known alias
    # AND is not normalized away by HARNESS_ALIASES.
    module_canonical_names = {
        name for name in _HARNESS_MODULES if name not in alias_to_canonical
    }
    canonical_names = set(OMNIGENT_HARNESSES) | module_canonical_names

    # Reverse-map module aliases onto their canonical id so they are
    # surfaced as descriptor aliases too (e.g. _HARNESS_MODULES maps both
    # "openai-agents" and — historically — alias keys to one module).
    name_to_module = dict(_HARNESS_MODULES)
    for alias, module in name_to_module.items():
        if alias in alias_to_canonical:
            continue
        # A non-alias name whose module is shared by a canonical name is
        # itself canonical; nothing to fold. Handled by canonical_names.

    # Build reverse alias index: canonical id → its alias spellings.
    aliases_by_canonical: dict[str, set[str]] = {name: set() for name in canonical_names}
    for alias, canonical in alias_to_canonical.items():
        aliases_by_canonical.setdefault(canonical, set()).add(alias)
    # OMNIGENT_HARNESS_ALIASES are spec-accepted aliases; route each to its
    # canonical id via HARNESS_ALIASES when known (they are a subset).
    for alias in OMNIGENT_HARNESS_ALIASES:
        canonical = alias_to_canonical.get(alias)
        if canonical is not None:
            aliases_by_canonical.setdefault(canonical, set()).add(alias)

    registry: dict[str, HarnessProvider] = {}
    for name in sorted(canonical_names):
        registry[name] = HarnessProvider(
            name=name,
            aliases=tuple(sorted(aliases_by_canonical.get(name, set()))),
            is_native=name in NATIVE_HARNESSES,
            module_path=name_to_module.get(name),
        )
    return registry


# Built once at import from the four legacy sources. Keyed by canonical id.
HARNESS_PROVIDERS: dict[str, HarnessProvider] = _build_registry()

# Flat alias → canonical lookup, derived from the descriptors above.
_ALIAS_INDEX: dict[str, str] = {
    alias: provider.name
    for provider in HARNESS_PROVIDERS.values()
    for alias in provider.aliases
}


def resolve(harness: str | None) -> HarnessProvider | None:
    """Resolve a canonical id or alias to its :class:`HarnessProvider`.

    Used only when ``OMNIGENT_USE_HARNESS_PROVIDER_REGISTRY`` is on; the
    four legacy sources remain authoritative when it is off.

    :param harness: A canonical harness id or a user-facing alias, e.g.
        ``"claude-sdk"`` or ``"claude"``. ``None`` returns ``None``.
    :returns: The matching :class:`HarnessProvider`, or ``None`` when the
        name is neither a known canonical id nor a known alias.
    """
    if harness is None:
        return None
    if harness in HARNESS_PROVIDERS:
        return HARNESS_PROVIDERS[harness]
    canonical = _ALIAS_INDEX.get(harness)
    if canonical is not None:
        return HARNESS_PROVIDERS.get(canonical)
    return None


__all__ = ["HarnessProvider", "HARNESS_PROVIDERS", "resolve"]
