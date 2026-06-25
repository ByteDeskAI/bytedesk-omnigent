"""KERNEL — the extension contract and lifecycle protocols.

This is the heart of the microkernel: the *only* thing the kernel knows about an
extension is that it (a) has a ``name`` and (b) can ``register`` itself into the
host during boot. Everything else — tools, harnesses, policies, routes — is
contributed by the extension *from inside its own ``register``*, exactly like
ServiceStack's ``IPlugin.Register(IAppHost appHost)``.

Faithful to the real ``omnigent.extensions.OmnigentExtension`` Protocol: the
lifecycle methods are *optional* and probed with ``hasattr`` so an extension
implements only the stages it needs (the ServiceStack ``IPreInitPlugin`` /
``IPostInitPlugin`` staged-hook model).

Nothing domain-specific lives here. This file never changes when you add a
capability — that is what makes it the kernel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .host import Host


@runtime_checkable
class Extension(Protocol):
    """The single contract every extension conforms to — first- or third-party.

    The *required* surface is intentionally tiny: a ``name`` and a ``register``.
    ``register`` is where an extension mutates the host (the ServiceStack
    ``Register(appHost)`` analog) — the self-registration step.

    The other lifecycle stages — ``pre_init`` / ``post_init`` / ``after_init``
    (see :data:`LIFECYCLE_STAGES`) — are *optional* and therefore NOT part of
    this ``runtime_checkable`` contract (a Protocol checks every declared member,
    which would force every extension to implement all four). The host probes for
    them with ``hasattr`` and skips any an extension omits — the same pattern the
    real ``omnigent.extensions`` Protocol uses for its optional capability methods.
    """

    name: str

    def register(self, host: "Host") -> None:
        """Stage 2 — contribute capabilities into the host's seams. This is the
        self-registration step: ``host.tools.register(...)`` etc. (``IPlugin``)."""
        ...


#: The ordered lifecycle stages the host fires (``IPreInitPlugin`` → ``IPlugin``
#: → ``IPostInitPlugin`` → ``IAfterInitAppHost``). All but ``register`` are
#: optional and ``hasattr``-probed per extension. Adding a stage is a kernel
#: change (rare); adding a *capability* is just an extension, never this list.
LIFECYCLE_STAGES: tuple[str, ...] = ("pre_init", "register", "post_init", "after_init")
