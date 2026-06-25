"""KERNEL — the Host: lifecycle engine, seam registries, service container.

The ``Host`` is the ServiceStack ``AppHost`` analog. It owns:

  * ``plugins`` — the ordered list of extensions (load order is explicit & observable).
  * a set of named :class:`PluggableRegistry` *seams* extensions register into.
  * a typed *service registry* (``{type|name: instance}``) for inter-extension lookup
    — the ``appHost.GetPlugin<T>()`` / ``Resolve<T>()`` analog.
  * config-driven enable/disable — the ``Config.EnableFeatures`` analog.

``boot()`` fires the four lifecycle stages **in order, across all extensions**:
``pre_init`` → ``register`` → ``post_init`` → ``after_init``. A stage is skipped
for any extension that doesn't implement it (``hasattr`` probe) and an extension
disabled by config never runs at all.

This is the Template-Method pattern: the *sequence* of boot is fixed in the
kernel; each extension fills in the steps it cares about.
"""

from __future__ import annotations

import logging
from typing import Any

from .di import Container, Lifetime
from .protocol import LIFECYCLE_STAGES, Extension
from .registry import PluggableRegistry

logger = logging.getLogger("omnigent_demo.host")

#: The seams the kernel ships. Each maps to a PluggableRegistry on the host.
#: Adding a seam is a deliberate kernel decision; contributing *into* a seam is
#: just an extension. (Real omnigent's ``SEAMS`` tuple, condensed for the demo.)
KERNEL_SEAMS: tuple[str, ...] = (
    "tools",
    "harnesses",
    "policies",
    "routers",
    "background",
    "secret_backends",
)


class Host:
    """The microkernel application host."""

    def __init__(self, *, disabled: set[str] | None = None) -> None:
        self.plugins: list[Extension] = []
        self.seams: dict[str, PluggableRegistry[Any]] = {
            name: PluggableRegistry(name) for name in KERNEL_SEAMS
        }
        #: The DI container — single source of truth for services. ``provide`` /
        #: ``resolve`` below are thin sugar over it so extensions can use either
        #: the simple key/value form or full constructor auto-wiring.
        self.container = Container()
        self.container.register_instance(Host, self)  # the host injects itself
        self._disabled = disabled or set()  # Config.EnableFeatures analog
        self._booted = False

    # ── convenience seam accessors (so extensions write host.tools.register(...)) ──
    @property
    def tools(self) -> PluggableRegistry[Any]:
        return self.seams["tools"]

    @property
    def harnesses(self) -> PluggableRegistry[Any]:
        return self.seams["harnesses"]

    @property
    def policies(self) -> PluggableRegistry[Any]:
        return self.seams["policies"]

    @property
    def routers(self) -> PluggableRegistry[Any]:
        return self.seams["routers"]

    @property
    def background(self) -> PluggableRegistry[Any]:
        return self.seams["background"]

    # ── plugin management (appHost.Plugins.Add) ──
    def add(self, ext: Extension) -> "Host":
        """Append *ext* to the load order. Returns self for chaining."""
        if not isinstance(ext, Extension):  # runtime_checkable Protocol
            raise TypeError(f"{ext!r} does not satisfy the Extension contract")
        if ext.name in self._disabled:
            logger.info("extension %r disabled by config — skipped", ext.name)
            return self
        self.plugins.append(ext)
        return self

    # ── typed service container (appHost.Register<T> / GetPlugin<T>) ──
    def provide(self, key: Any, instance: Any) -> None:
        """Publish a ready-made singleton service (sugar over the DI container)."""
        self.container.register_instance(key, instance)

    def provide_type(self, cls: type, *, key: Any = None, lifetime: Lifetime = Lifetime.SINGLETON) -> None:
        """Register a class for constructor auto-wiring under *key* (default *cls*)."""
        self.container.register_type(cls, key=key, lifetime=lifetime)

    def resolve(self, key: Any, default: Any = None) -> Any:
        """Resolve a service from the DI container (``None``/default if absent)."""
        return self.container.try_resolve(key, default)

    def inject(self, fn) -> Any:
        """Call *fn* with its annotated params injected from the container."""
        return self.container.call(fn)

    def get_plugin(self, name: str) -> Extension | None:
        """Look up a loaded extension by name (``appHost.GetPlugin<T>()``)."""
        return next((p for p in self.plugins if p.name == name), None)

    def assert_plugin(self, name: str) -> Extension:
        """Like :meth:`get_plugin` but raises if absent (``appHost.AssertPlugin<T>()``)."""
        plugin = self.get_plugin(name)
        if plugin is None:
            raise LookupError(
                f"required extension {name!r} not loaded "
                f"(loaded: {[p.name for p in self.plugins]})"
            )
        return plugin

    # ── the lifecycle engine (Template Method) ──
    def boot(self) -> "Host":
        """Fire every lifecycle stage across all extensions, in order."""
        if self._booted:
            raise RuntimeError("host already booted")
        for stage in LIFECYCLE_STAGES:
            for ext in self.plugins:
                hook = getattr(ext, stage, None)
                if hook is None or not callable(hook):
                    continue  # extension opts out of this stage
                logger.debug("stage=%s ext=%s", stage, ext.name)
                hook(self)
            logger.info("lifecycle stage %r complete", stage)
        self._booted = True
        return self

    # ── introspection (capability_manifest analog) ──
    def manifest(self) -> dict[str, Any]:
        """A snapshot of everything that got wired — for ``/v1/capabilities``."""
        return {
            "plugins": [p.name for p in self.plugins],
            "seams": {name: reg.names() for name, reg in self.seams.items()},
            "services": [
                k.__name__ if isinstance(k, type) else str(k)
                for k in self.container._registrations  # noqa: SLF001 — demo introspection
            ],
        }
