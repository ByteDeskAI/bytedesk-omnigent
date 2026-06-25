"""omnigent.sdk — the public, semver-stable extension-author facade (BDP-2508).

Everything a third-party extension author needs, and nothing they don't. The
kernel below (``omnigent.kernel.extensions`` Protocol, ``omnigent.kernel.pluggable``
registries, ``omnigent.server.app.create_app``) is the implementation this
facade hides. Write::

    from omnigent.sdk import extension, tool, policy, background, provides, Host

    @extension(name="my-extension")
    class MyExtension:
        @provides()
        def clock(self) -> Clock:
            return SystemClock()

        @tool(name="echo")
        def echo_tool(self, clock: Clock):       # ← clock injected by the container
            return EchoTool(clock)

        @policy(name="Per-Agent Rate Limiter", kind="factory")
        def per_agent_rate_limit(self, calls_per_minute: float):
            def _policy(event, context): ...
            return _policy

        @background
        async def my_maintenance_loop(self): ...

    # still a kernel Protocol object — no parallel mechanism:
    from omnigent.kernel.extensions import OmnigentExtension
    assert isinstance(MyExtension(), OmnigentExtension)

The entry-point string in ``pyproject.toml`` remains the irreducible
self-registration hook (Section 12.3); everything else is hidden here.

**Layering invariant (Section 12.7):** a class decorated with ``@extension``
conforms to :class:`omnigent.kernel.extensions.OmnigentExtension`, and its synthesised
hooks return the *same shape* as the hand-written Protocol form. The SDK adds no
parallel discovery, plugin list, or lifecycle — it compiles down to the kernel.

**Stability (Section 12.8):** every name re-exported here is part of the
semver-stable public surface. Kernel internals may churn between minors as long
as this surface keeps compiling extensions.
"""

from __future__ import annotations

from .contrib import (
    background,
    harness,
    policy,
    provides,
    router,
    tool,
    tool_interceptor,
)
from .di import Container, DIResolutionError, Lifetime
from .extension import extension
from .host import Host

__all__ = [
    # class decorator
    "extension",
    # member decorators
    "tool",
    "harness",
    "policy",
    "background",
    "router",
    "tool_interceptor",
    "provides",
    # host builder
    "Host",
    # DI container
    "Container",
    "Lifetime",
    "DIResolutionError",
]
