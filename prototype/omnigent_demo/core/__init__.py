"""CORE — the kernel extended by the curated first-party extensions.

``core`` is *not* privileged code. It is exactly the kernel plus this bundle of
extensions, each registering through the same SDK contract a third party uses.
``default_extensions()`` is the only thing the composition root needs.

Dependency order is expressed declaratively (``requires=`` on each extension);
the list below is the conventional load order, and the kernel's ``assert_plugin``
turns a missing/out-of-order dependency into a clear boot error rather than a
mysterious ``None``.
"""

from __future__ import annotations

from .harnesses_ext import HarnessesExtension
from .stores_ext import StoresExtension
from .tools_ext import ToolsExtension


def default_extensions() -> list:
    """The first-party extensions every install gets by default."""
    return [StoresExtension(), ToolsExtension(), HarnessesExtension()]


__all__ = ["StoresExtension", "ToolsExtension", "HarnessesExtension", "default_extensions"]
