"""
Harness package — per-conversation subprocesses that implement a
subset of the Omnigent REST API.

See ``designs/SERVER_HARNESS_CONTRACT.md`` for the full contract.
The harness IS an HTTP service speaking the same Pydantic models AP
serves to external clients (re-use ``omnigent.server.schemas`` —
there is no separate protocol module).

This package contains:

- ``_HARNESS_MODULES``: harness-name → ``create_app()`` module-path
  mapping consumed across the runner/dispatch path. It is **projected
  from** the single source of truth — the descriptor registry in
  :mod:`omnigent.runtime.harnesses.descriptors` — rather than a
  hand-written literal, so harness identity lives in exactly one place
  (BDP-2346). It stays a plain mutable ``dict`` because the test suite
  injects fixture entries at test time via direct mutation.
- ``process_manager``: ``HarnessProcessManager`` — owns
  per-conversation subprocess lifecycle.
- ``_runner``: shared ``python -m`` entrypoint that any registered
  harness's ``create_app()`` is served through.

The package directory is intentionally small. Behavior lives in the
sibling modules; this ``__init__.py`` is just the registry projection.
"""

from __future__ import annotations

from omnigent.runtime.harnesses.descriptors import harness_modules

# Harness-name → fully-qualified module path. Each module must export
# ``create_app() -> FastAPI``; the runner imports the module, calls the
# factory, and serves the result over a Unix socket.
#
# Projected from the descriptor registry (descriptors.py), which is the
# single source of truth for harness identity — including the ByteDesk
# ``hermes`` harness, wired into the default descriptor set in core (hard
# fork) so there is no hardcoded cross-package string literal here. The
# value is a fresh mutable dict; the test suite injects fixture entries
# at test time via direct mutation.
_HARNESS_MODULES: dict[str, str] = harness_modules()

__all__ = ["_HARNESS_MODULES"]
