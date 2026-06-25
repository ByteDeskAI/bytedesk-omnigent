"""SDK — public type re-exports (BDP-2508, Section 12.1).

A stable, kernel-light surface of the types an extension author annotates
against (``Tool``, ``ToolContext``, ``HarnessDescriptor``, ``OmnigentExtension``,
``PolicyRegistryEntry``). Heavy domain modules are imported **lazily** via
``__getattr__`` (PEP 562) so ``import omnigent.sdk.types`` does not drag the
FastAPI / runtime stack onto the import path — only the attribute actually
accessed is imported. Annotation-only use (``from omnigent.sdk.types import Tool``
under ``TYPE_CHECKING``) costs nothing.

Names are part of the semver-stable SDK surface (Section 12.8); the underlying
kernel modules they resolve to are implementation detail and may move.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

#: name -> "module:attr" the lazy ``__getattr__`` resolves on first access.
_LAZY: dict[str, str] = {
    "OmnigentExtension": "omnigent.extensions:OmnigentExtension",
    "Tool": "omnigent.tools.base:Tool",
    "ToolContext": "omnigent.tools.base:ToolContext",
    "HarnessDescriptor": "omnigent.runtime.harnesses.descriptors:HarnessDescriptor",
    "PolicyRegistryEntry": "omnigent.policies.registry:PolicyRegistryEntry",
}

if TYPE_CHECKING:  # static import for type checkers only — no runtime cost
    from omnigent.extensions import OmnigentExtension as OmnigentExtension


def __getattr__(name: str) -> Any:  # PEP 562 module-level lazy attribute
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod_name, _, attr = target.partition(":")
    try:
        mod = importlib.import_module(mod_name)
        value = getattr(mod, attr)
    except (ImportError, AttributeError) as exc:  # optional dep absent
        raise AttributeError(
            f"{name!r} could not be resolved from {target!r}: {exc}"
        ) from exc
    globals()[name] = value  # memoise
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "OmnigentExtension",
    "Tool",
    "ToolContext",
    "HarnessDescriptor",
    "PolicyRegistryEntry",
]
