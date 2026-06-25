"""omnigent_demo — a runnable, stdlib-only prototype of the three-tier
microkernel + SDK + DI architecture proposed in
``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``.

Tiers:
    kernel/      KERNEL      — boot + plugin-hosting + DI. Domain-free.
    core/        CORE        — kernel + first-party extensions (via the SDK).
    extensions/  EXTENSIONS  — third-party, same SDK + same contract.
    sdk/         FACADE      — the developer API that hides all of the above.

See ``run_demo.py`` for an end-to-end boot.
"""

from __future__ import annotations

from .core import default_extensions
from .sdk import Host


def bootstrap(*, discover: bool = True, disable: tuple[str, ...] = ()) -> Host:
    """Assemble a host = kernel + core + discovered third-party extensions.

    This is the *composition root* — the one place that knows the concrete set
    of tiers. Everything else depends only on interfaces and the SDK.
    """
    builder = Host.build()
    if disable:  # apply BEFORE adding, so disabled extensions are never added
        builder.disable(*disable)
    for ext in default_extensions():  # CORE
        builder.with_extension(ext)
    if discover:  # third-party EXTENSIONS that declared themselves
        builder.discover()
    return builder.boot()


__all__ = ["bootstrap", "Host"]
