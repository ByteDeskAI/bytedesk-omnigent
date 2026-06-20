"""ByteDesk policy builtins, listed in core BUILTIN_POLICY_MODULES (ADR-0143).

Also home to :class:`PolicyRegistryRaw`, the shared ``TypedDict`` that the
per-module ``POLICY_REGISTRY`` lists annotate themselves with. It pins the raw
entry shape the core registry scans (``omnigent.policies.registry.load_registry``)
so a missing ``handler`` or a bad ``kind`` is caught at author time instead of
silently skipped at startup.
"""

from typing import Any, NotRequired, TypedDict

from omnigent.policies.registry import PolicyHandlerKind

__all__ = ["PolicyHandlerKind", "PolicyRegistryRaw"]


class PolicyRegistryRaw(TypedDict):
    """Raw shape of one ``POLICY_REGISTRY`` entry before the registry ingests it.

    Mirrors what :func:`omnigent.policies.registry.load_registry` reads from each
    module's ``POLICY_REGISTRY`` list: ``handler`` and ``kind`` are required, the
    display ``name`` / ``description`` are auto-derived when absent, and
    ``params_schema`` is only meaningful for ``kind == "factory"``.

    :param handler: Full dotted import path to the policy callable or factory.
    :param kind: ``"callable"`` (direct) or ``"factory"`` (built with params).
    :param name: Optional short display name; auto-derived from the handler.
    :param description: Optional human-readable description.
    :param params_schema: Optional JSON Schema dict for factory params.
    """

    handler: str
    kind: PolicyHandlerKind
    name: NotRequired[str]
    description: NotRequired[str]
    # Opaque JSON Schema dict — a genuine foreign-shape boundary, mirrors the
    # core registry's ``params_schema: dict[str, Any] | None`` (registry.py).
    params_schema: NotRequired[dict[str, Any]]  # type: ignore[explicit-any]
