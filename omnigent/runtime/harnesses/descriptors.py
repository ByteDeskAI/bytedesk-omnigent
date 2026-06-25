"""Harness identity registry — one descriptor per harness, on the pluggable seam.

This is the single source of truth for harness identity (BDP-2346). Each
harness is one :class:`HarnessDescriptor` value (``name``, ``aliases``,
``is_native``, ``module_path``, ``config_schema``), registered on a
:class:`~omnigent.pluggable.PluggableRegistry` keyed by canonical id. The four
historical identity facets are *projected from* this registry, not the other way
around:

- :func:`harness_modules` — canonical id / inline alias → ``create_app()`` module
  path. ``omnigent.runtime.harnesses._HARNESS_MODULES`` is this projection.
- :func:`native_harness_ids` — canonical native-CLI harness ids
  (``omnigent.harness_aliases.NATIVE_HARNESSES`` is the curated superset that also
  carries reversed alias spellings).
- :func:`resolve` — canonical id *or* any alias → descriptor.

Core dogfoods the harness seam (Section 9.2): its own first-party harnesses are
contributed through the module-level :func:`harness_descriptors` hook — the same
``{canonical_id: () -> descriptor}`` shape a third-party extension implements —
which :func:`_build_registry` consumes to populate the registry at import. There is
no hard-wired registration loop and no privileged "default set" the seam cannot
express; proving the seam hosts all first-party harnesses proves it hosts a
third-party one.

The ByteDesk ``hermes`` harness (``bytedesk_omnigent.harnesses.hermes_native_harness``)
is the cross-package case: it stays in the first-party set here so it is resolvable
at *import* time (``_HARNESS_MODULES`` and the omnigent allowlist materialize at
import, before any FastAPI-heavy extension discovery), and the ByteDesk extension
ALSO declares it through :meth:`BytedeskExtension.harness_descriptors` (BDP-2507) so
the cross-package contribution path is exercised end-to-end. That contribution is
idempotent — a no-op when hermes is already registered — so the two never conflict.
Importing ``bytedesk_omnigent`` from this module would pull FastAPI onto the harness
import hot path (BDP-2371), so the resolvable copy lives in core.

The registry is keyed by canonical id; :meth:`PluggableRegistry.discover_extensions`
is consulted at server startup with the ``harness_descriptors`` hook so any
extension can contribute *additional* harnesses error-isolated, exactly like the
artifact-store reference seam in :mod:`omnigent.stores.factory`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from omnigent.pluggable import PluggableRegistry


@dataclass(frozen=True)
class HarnessDescriptor:
    """A single harness's identity, folded into one value.

    :param name: Canonical harness id, e.g. ``"claude-sdk"``. Matches the
        value of ``spec.executor.config.harness`` after canonicalization.
    :param module_path: Fully-qualified module that exports ``create_app()``
        for this harness; ``None`` when the harness has no registered runner
        module (e.g. ``"open-responses"`` is accepted by the omnigent allowlist
        but resolved by a different executor path).
    :param aliases: User-facing alias spellings that canonicalize to
        :attr:`name`, e.g. ``("claude",)`` for ``"claude-sdk"``. These are also
        the inline-alias keys projected into ``_HARNESS_MODULES``.
    :param is_native: Whether this is a native CLI harness (boots a vendor TUI
        in a terminal); mirrors membership in
        :data:`omnigent.harness_aliases.NATIVE_HARNESSES`.
    :param config_schema: Reserved descriptor for a harness's ``executor.config``
        schema. Carried for parity with the native ``config.schema`` work;
        ``None`` until a schema is wired.
    """

    name: str
    module_path: str | None = None
    aliases: tuple[str, ...] = ()
    is_native: bool = False
    config_schema: object | None = field(default=None)


# ── The first-party harness set (dogfooded through the harness_descriptors hook) ──
#
# One descriptor per harness. ``module_path`` is the module exporting
# ``create_app() -> FastAPI``; ``aliases`` are the inline-alias spellings that
# historically appeared as extra ``_HARNESS_MODULES`` keys pointing at the same
# module; ``is_native`` marks native-CLI terminal harnesses.
#
# These are core's OWN harnesses, contributed through the same
# ``harness_descriptors()`` hook a third party would implement (Section 9.2
# dogfooding): :func:`harness_descriptors` projects this tuple into the
# ``{canonical_id: () -> descriptor}`` hook shape, and :func:`_build_registry`
# consumes that hook output rather than a hard-wired registration loop. If the
# seam can host all first-party harnesses it can host a third-party one.
_FIRST_PARTY_DESCRIPTORS: tuple[HarnessDescriptor, ...] = (
    # claude-sdk harness wrap. See omnigent/inner/claude_sdk_harness.py.
    # User-facing alias "claude" accepted in specs / omnigent dispatch.
    HarnessDescriptor(
        name="claude-sdk",
        module_path="omnigent.inner.claude_sdk_harness",
        aliases=("claude",),
    ),
    # Native Claude Code terminal bridge used by ``omnigent claude``.
    HarnessDescriptor(
        name="claude-native",
        module_path="omnigent.inner.claude_native_harness",
        is_native=True,
    ),
    # Native Codex TUI terminal bridge used by ``omnigent codex``.
    HarnessDescriptor(
        name="codex-native",
        module_path="omnigent.inner.codex_native_harness",
        is_native=True,
    ),
    # codex harness wrap. See omnigent/inner/codex_harness.py.
    HarnessDescriptor(
        name="codex",
        module_path="omnigent.inner.codex_harness",
    ),
    # pi harness wrap. See omnigent/inner/pi_harness.py.
    HarnessDescriptor(
        name="pi",
        module_path="omnigent.inner.pi_harness",
    ),
    # Native Pi TUI bridge used by ``omnigent pi``. User-facing alias "native-pi".
    HarnessDescriptor(
        name="pi-native",
        module_path="omnigent.inner.pi_native_harness",
        aliases=("native-pi",),
        is_native=True,
    ),
    # Native xAI Grok Build CLI bridge (ACP over ``grok agent stdio``) — the
    # "Grok" picker option. Subscription OAuth via ~/.grok/auth.json. See
    # omnigent/inner/grok_native_harness.py. User-facing alias "grok".
    # NOTE: is_native is False to mirror the legacy NATIVE_HARNESSES set, which
    # does not list "grok-native" (preserved behavior, BDP-2346).
    HarnessDescriptor(
        name="grok-native",
        module_path="omnigent.inner.grok_native_harness",
        aliases=("grok",),
    ),
    # openai-agents harness wrap. See omnigent/inner/openai_agents_sdk_harness.py.
    # Registry key is the omnigent-side spelling ("openai-agents", no "-sdk"
    # suffix) to match OmnigentExecutor's harness allowlist and the
    # executor.harness field used in omnigent YAML; the backing Python module
    # retains the "_sdk" suffix because the underlying SDK package is
    # "openai-agents" and the executor class is OpenAIAgentsSDKExecutor.
    # Alias "openai-agents-sdk" is the SDK package / runtime dispatch spelling.
    HarnessDescriptor(
        name="openai-agents",
        module_path="omnigent.inner.openai_agents_sdk_harness",
        aliases=("openai-agents-sdk",),
    ),
    # cursor harness wrap (Cursor's ``cursor-agent`` CLI, headless). See
    # omnigent/inner/cursor_harness.py.
    HarnessDescriptor(
        name="cursor",
        module_path="omnigent.inner.cursor_harness",
    ),
    # Google Antigravity SDK harness wrap. See omnigent/inner/antigravity_harness.py.
    # In-process SDK harness ("google-antigravity"), like openai-agents — omnigent
    # spawns no CLI binary or sandbox subprocess (the SDK itself launches a native
    # localharness binary; needs glibc >=~2.36). Drives Gemini 3.5 Flash by default
    # (also Claude / GPT-OSS), with Gemini API-key or Vertex AI auth. User-facing
    # aliases "agy" and "google-antigravity".
    HarnessDescriptor(
        name="antigravity",
        module_path="omnigent.inner.antigravity_harness",
        aliases=("agy", "google-antigravity"),
    ),
    # Supervisor harness wrap. See omnigent/inner/databricks_supervisor_harness.py.
    # Drives the Databricks Agent Bricks Supervisor API at
    # ``{workspace}/ai-gateway/mlflow/v1/responses``. Differs from the
    # SDK-wrapping harnesses above in that the inner executor has no third-party
    # SDK dependency — it talks HTTP / SSE directly to the Databricks gateway.
    HarnessDescriptor(
        name="databricks_supervisor",
        module_path="omnigent.inner.databricks_supervisor_harness",
    ),
    # ByteDesk Hermes Agent bridge (ACP over ``hermes acp``) — Kade Vector's
    # model-agnostic brain. Kept here so hermes is resolvable at *import* time
    # (``_HARNESS_MODULES["hermes"]`` and the omnigent allowlist are materialized
    # at import, before any FastAPI-heavy extension discovery runs). The ByteDesk
    # extension ALSO declares it through :meth:`BytedeskExtension.harness_descriptors`
    # (BDP-2507) so the cross-package contribution path is exercised end-to-end;
    # that contribution is idempotent — it is a no-op when hermes is already
    # registered here — so the two never conflict. Importing ``bytedesk_omnigent``
    # from this module would pull FastAPI onto the harness import hot path, which
    # the seam deliberately avoids (BDP-2371), so the resolvable copy lives here.
    # See bytedesk_omnigent/harnesses/hermes_native_harness.py.
    HarnessDescriptor(
        name="hermes",
        module_path="bytedesk_omnigent.harnesses.hermes_native_harness",
    ),
    # OpenAI Responses-API harness, accepted under executor.type: omnigent and
    # resolved by ``omnigent.inner.open_responses_sdk.OpenResponsesExecutor`` —
    # NOT through _HARNESS_MODULES. module_path is None so it appears in the
    # descriptor set / allowlist but is omitted from the module-path projection,
    # mirroring the legacy provider's handling of this name (BDP-2346).
    HarnessDescriptor(
        name="open-responses",
        module_path=None,
    ),
)


def harness_descriptors() -> dict[str, Callable[[], HarnessDescriptor]]:
    """Core's first-party harnesses, in the ``harness_descriptors`` hook shape.

    This is the SAME hook a third-party extension implements to contribute a
    harness (the ``harness_descriptors`` entry in
    :data:`omnigent.pluggable.manifest.SEAMS`): a mapping of canonical id to a
    zero-arg factory returning the :class:`HarnessDescriptor`. Core dogfoods the
    seam by registering its own built-in harnesses through this hook rather than a
    hard-wired registration loop (Section 9.2): :func:`_build_registry` consumes
    this mapping, so if the seam cannot host all first-party harnesses it cannot
    host a third-party one either.

    :returns: ``{canonical_id: () -> HarnessDescriptor}`` for every first-party
        harness, fresh per call.
    """
    return {d.name: (lambda d=d: d) for d in _FIRST_PARTY_DESCRIPTORS}


def _build_registry() -> PluggableRegistry[HarnessDescriptor]:
    """Build the harness-identity registry, keyed by canonical id.

    First-party descriptors are sourced from :func:`harness_descriptors` — the
    same ``{canonical_id: () -> descriptor}`` hook a third-party extension would
    implement — and registered one per canonical id; the first is the registry's
    default impl (it carries no special meaning beyond being a registered name).
    Third-party extensions contribute *additional* harnesses through the same
    ``harness_descriptors`` hook at server startup
    (:func:`omnigent.pluggable.manifest.discover_all_extensions`), error-isolated
    exactly like the artifact-store reference seam.

    :returns: The populated :class:`PluggableRegistry`.
    """
    (head_name, head_factory), *rest = harness_descriptors().items()
    registry: PluggableRegistry[HarnessDescriptor] = PluggableRegistry(
        "harness", default=(head_name, head_factory)
    )
    for name, factory in rest:
        registry.register(name, factory)
    # Third-party extension discovery is deferred to server startup (Wave-2
    # composition root): it loads FastAPI-heavy entry-point extensions; keep off
    # the import hot path. Hook: 'harness_descriptors'. The ByteDesk extension's
    # hermes contribution (BDP-2507) is idempotent — hermes is already registered
    # above from the first-party set, so the contribution skips it without
    # conflict.
    return registry


# Built once at import. The single source of truth for harness identity.
HARNESS_REGISTRY: PluggableRegistry[HarnessDescriptor] = _build_registry()

# Canonical id → descriptor, materialized from the registry.
HARNESS_DESCRIPTORS: dict[str, HarnessDescriptor] = {
    name: HARNESS_REGISTRY.get(name) for name in HARNESS_REGISTRY.names()
}

# Flat alias → canonical id, derived from every descriptor's alias list.
_ALIAS_INDEX: dict[str, str] = {
    alias: descriptor.name
    for descriptor in HARNESS_DESCRIPTORS.values()
    for alias in descriptor.aliases
}


def harness_modules() -> dict[str, str]:
    """Project the descriptor set to the harness-name → module-path mapping.

    Includes one entry per canonical id with a ``module_path`` plus one entry per
    inline alias pointing at the same module — the exact shape the legacy
    ``_HARNESS_MODULES`` literal carried. Descriptors with no ``module_path``
    (none today) are omitted, matching the historical dict.

    :returns: A fresh ``dict`` (callers, including the package ``__init__``, own a
        mutable copy so test fixtures can inject test-only harnesses).
    """
    modules: dict[str, str] = {}
    for descriptor in HARNESS_DESCRIPTORS.values():
        if descriptor.module_path is None:
            continue
        modules[descriptor.name] = descriptor.module_path
        for alias in descriptor.aliases:
            modules[alias] = descriptor.module_path
    return modules


def native_harness_ids() -> frozenset[str]:
    """The canonical ids of native-CLI harnesses, from the descriptor set."""
    return frozenset(
        name for name, d in HARNESS_DESCRIPTORS.items() if d.is_native
    )


def resolve(harness: str | None) -> HarnessDescriptor | None:
    """Resolve a canonical id or alias to its :class:`HarnessDescriptor`.

    :param harness: A canonical harness id or a user-facing alias, e.g.
        ``"claude-sdk"`` or ``"claude"``. ``None`` returns ``None``.
    :returns: The matching :class:`HarnessDescriptor`, or ``None`` when the name
        is neither a known canonical id nor a known alias.
    """
    if harness is None:
        return None
    if harness in HARNESS_DESCRIPTORS:
        return HARNESS_DESCRIPTORS[harness]
    canonical = _ALIAS_INDEX.get(harness)
    if canonical is not None:
        return HARNESS_DESCRIPTORS.get(canonical)
    return None


__all__ = [
    "HarnessDescriptor",
    "HARNESS_REGISTRY",
    "HARNESS_DESCRIPTORS",
    "harness_descriptors",
    "harness_modules",
    "native_harness_ids",
    "resolve",
]
