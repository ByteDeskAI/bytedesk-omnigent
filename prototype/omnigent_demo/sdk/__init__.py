"""SDK — the public, semver-stable developer API for building extensions.

Everything an extension author needs, and nothing they don't. The kernel below
(registries, lifecycle protocols, the DI container, discovery) is an
implementation detail this facade hides. Write::

    from omnigent_demo.sdk import extension, tool, provides, inject, Host

    @extension(name="my-feature")
    class MyFeature:
        @provides()
        def clock(self) -> Clock:
            return SystemClock()

        @tool(name="echo")
        def echo_tool(self, clock: Clock):     # ← clock injected by the container
            return EchoTool(clock)

    host = Host.build().with_extension(MyFeature()).boot()

Stability contract: these names are the public surface. Kernel internals can be
refactored freely as long as this surface keeps compiling extensions.
"""

from __future__ import annotations

from ..kernel import Host as _KernelHost
from ..kernel import Lifetime
from .decorators import (
    HostBuilder,
    background,
    extension,
    harness,
    policy,
    provides,
    router,
    tool,
)


class Host(_KernelHost):
    """The kernel Host, plus the fluent ``Host.build()`` entry point."""

    @staticmethod
    def build() -> HostBuilder:
        """Start a fluent composition root (Builder pattern)."""
        return HostBuilder()


def inject(host: Host, fn):
    """Resolve *fn*'s annotated params from *host*'s container and call it."""
    return host.inject(fn)


__all__ = [
    "Host",
    "HostBuilder",
    "Lifetime",
    "extension",
    "tool",
    "harness",
    "policy",
    "background",
    "router",
    "provides",
    "inject",
]
