"""Consolidated harness-identity registry — one descriptor per harness.

The single source of truth is now the descriptor registry in
:mod:`omnigent.runtime.harnesses.descriptors` (BDP-2346): a
:class:`~omnigent.pluggable.PluggableRegistry` of :class:`HarnessDescriptor`
values, from which the harness-name → module-path mapping, the native
classification, and alias resolution are all projected.

This module is a thin compatibility surface over that registry. :class:`HarnessProvider`
is an alias for :class:`~omnigent.runtime.harnesses.descriptors.HarnessDescriptor`,
:data:`HARNESS_PROVIDERS` is the canonical-id → descriptor map, and :func:`resolve`
accepts a canonical id *or* any alias. Earlier this was a strangler-fig sidecar gated
by ``OMNIGENT_USE_HARNESS_PROVIDER_REGISTRY``; with the registry now the only path
(hard fork, no upstream), that flag and the legacy four-source fold are gone — the
registry IS the source of truth, not a parallel view of it.
"""

from __future__ import annotations

from omnigent.runtime.harnesses.descriptors import (
    HARNESS_DESCRIPTORS,
    HarnessDescriptor,
    resolve,
)

# Backwards-compatible name for the descriptor type.
HarnessProvider = HarnessDescriptor

# Canonical id → descriptor, straight from the registry projection.
HARNESS_PROVIDERS: dict[str, HarnessDescriptor] = HARNESS_DESCRIPTORS


__all__ = ["HarnessProvider", "HARNESS_PROVIDERS", "resolve"]
