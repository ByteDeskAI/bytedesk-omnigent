"""SDK — the developer-facing facade over the kernel.

This module is the *public API surface*. An extension author imports from here
and never touches ``PluggableRegistry``, entry-point strings, lifecycle-stage
protocols, or the ``register(host)`` wiring. They write declarative decorators;
the SDK *compiles them down to the same kernel ``Extension`` contract*.

Design patterns hidden behind this facade:
  * **Microkernel / Plugin** — the whole tiering (kernel/core/extensions).
  * **Registry** — ``PluggableRegistry`` per seam (hidden; you just ``@tool``).
  * **Facade** — this module: one clean surface over many kernel moving parts.
  * **Builder** — ``Host.build()...build_app()`` fluent composition root.
  * **Template Method** — the fixed boot sequence in ``Host.boot``.
  * **Strategy** — named factories per seam, switchable via ``OMNIGENT_USE_<SEAM>``.

Invariant: a class decorated with ``@extension`` satisfies
``isinstance(obj, kernel.Extension)`` — the SDK is a *thin facade*, not a second
source of truth. It generates a Protocol-conformant ``register`` for you.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_type_hints

from ..kernel import Host, Lifetime
from ..kernel.discovery import register_entry_point

# Attribute the member decorators stamp onto a function so @extension can find them.
_CONTRIB_ATTR = "__omnigent_contrib__"


def _mark(seam: str, **meta: Any):
    """Stamp a method with the seam it contributes to + metadata."""

    def deco(fn: Callable) -> Callable:
        setattr(fn, _CONTRIB_ATTR, {"seam": seam, "name": meta.get("name", fn.__name__), **meta})
        return fn

    return deco


# ── member decorators: declare *what* a method contributes, not *how* ──
def tool(name: str | None = None):
    """Mark a method as a tool factory → registered into the ``tools`` seam."""
    return _mark("tools", name=name)


def harness(name: str | None = None):
    """Mark a method as a harness factory → ``harnesses`` seam."""
    return _mark("harnesses", name=name)


def policy(name: str | None = None):
    """Mark a method as a policy factory → ``policies`` seam."""
    return _mark("policies", name=name)


def background(fn: Callable | None = None):
    """Mark a method as a background task → ``background`` seam.

    Usable bare (``@background``) or called (``@background()``)."""
    if fn is not None:
        return _mark("background")(fn)
    return _mark("background")


def router(prefix: str = ""):
    """Mark a method as a router factory → ``routers`` seam."""
    return _mark("routers", prefix=prefix)


def provides(key: Any | None = None, *, lifetime: Lifetime = Lifetime.SINGLETON):
    """Mark a method as a *service provider* → registered into the DI container.

    The method body is the factory; its own params are injected (so services can
    depend on other services). If *key* is omitted, the method's return type
    annotation is used as the key — letting other code depend on the interface::

        @provides(ArtifactStore)            # explicit interface key
        def store(self) -> S3ArtifactStore: ...

        @provides()                          # key inferred from -> annotation
        def clock(self) -> Clock: ...
    """
    return _mark("__service__", service_key=key, lifetime=lifetime)


def extension(name: str, *, requires: tuple[str, ...] = (), entry_point: bool = True):
    """Class decorator: turn a plain class into a kernel ``Extension``.

    What it does for you, so you never write ``register(host)`` by hand:
      * sets ``name`` and a ``requires`` dependency hint;
      * scans the class for ``@tool`` / ``@harness`` / ``@policy`` /
        ``@background`` / ``@router`` members and synthesises a
        ``register(host)`` that wires each into the correct host seam;
      * (optionally) self-registers under the entry-point group so the host
        *discovers* the extension without anyone importing it by name.

    The generated class still satisfies ``isinstance(obj, kernel.Extension)``.
    """

    def deco(cls: type) -> type:
        cls.name = name
        cls.requires = requires

        # Collect decorated members once, at decoration time.
        contribs: list[tuple[str, dict]] = []
        for attr_name in dir(cls):
            member = getattr(cls, attr_name, None)
            meta = getattr(member, _CONTRIB_ATTR, None)
            if meta is not None:
                contribs.append((attr_name, meta))

        def register(self, host: Host) -> None:
            # Optional dependency check — fail fast & legibly (assert_plugin).
            for dep in getattr(self, "requires", ()):  # noqa: B009
                host.assert_plugin(dep)

            # Services first, so seam factories in this same extension can inject
            # them. Cross-extension deps resolve lazily (seams build on demand).
            for attr_name, meta in contribs:
                if meta["seam"] != "__service__":
                    continue
                bound = getattr(self, attr_name)
                key = meta["service_key"] or get_type_hints(bound).get("return")
                if key is None:
                    raise TypeError(
                        f"@provides on {cls.__name__}.{attr_name} needs a key or "
                        f"a '-> ReturnType' annotation to use as the DI key"
                    )
                host.container.register_factory(
                    key,
                    lambda c, b=bound: c.call(b),  # method-injected factory
                    lifetime=meta["lifetime"],
                )

            # Then capability seams. The stored factory is DI-wrapped: when the
            # kernel registry resolves it, the container injects the factory's
            # annotated params. Author writes `def build(self, store: Store)`.
            for attr_name, meta in contribs:
                if meta["seam"] == "__service__":
                    continue
                bound = getattr(self, attr_name)
                seam = host.seams[meta["seam"]]
                seam.register(meta["name"], lambda b=bound: host.container.call(b))

        # Only attach a generated register if the author didn't write one.
        if "register" not in cls.__dict__:
            cls.register = register

        if entry_point:
            register_entry_point(name, cls)  # self-registration

        return cls

    return deco


class HostBuilder:
    """Fluent composition root — ``Host.build().with_extension(...).boot()``."""

    def __init__(self) -> None:
        self._host = Host()
        self._discover = False

    def with_extension(self, ext: Any) -> "HostBuilder":
        self._host.add(ext)
        return self

    def disable(self, *names: str) -> "HostBuilder":
        self._host._disabled.update(names)  # EnableFeatures analog
        return self

    def discover(self) -> "HostBuilder":
        """Pull in every extension that declared itself (entry-points + env)."""
        self._discover = True
        return self

    def boot(self) -> Host:
        if self._discover:
            from ..kernel import discover_extensions

            for ext in discover_extensions():
                if self._host.get_plugin(ext.name) is None:
                    self._host.add(ext)
        return self._host.boot()
