"""SDK — member decorators (BDP-2508).

These decorators *declare what a method contributes*, not *how* — they stamp a
small metadata marker (:data:`CONTRIB_ATTR`) onto the method. The
:func:`omnigent.sdk.extension.extension` class decorator scans for those markers
at decoration time and synthesises the matching :class:`OmnigentExtension`
Protocol hook returns:

  ===================  ====================================================
  decorator            synthesised kernel hook
  ===================  ====================================================
  :func:`tool`         ``tool_factories()`` → ``{name: factory(config)}``
  :func:`policy`       ``policy_modules()`` + a synthesised ``POLICY_REGISTRY``
  :func:`harness`      ``harness_descriptors()`` → ``{name: () -> descriptor}``
  :func:`background`   ``background_tasks()`` → ``[factory() -> awaitable]``
  :func:`router`       ``routers()`` → ``[APIRouter, ...]``
  :func:`tool_interceptor`  ``tool_interceptors()`` → ``{prefix: handler}``
  :func:`provides`     a DI registration on the extension's SDK container
  ===================  ====================================================

The SDK introduces **no** parallel discovery, lifecycle, or registry: a
decorated class still satisfies ``isinstance(obj, OmnigentExtension)`` and feeds
the kernel's existing ``discover_extensions`` / ``install_extensions`` /
``PluggableRegistry.discover_extensions`` calls identically to a hand-written
Protocol implementation (Section 12.7).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .di import Lifetime

#: Attribute the member decorators stamp onto a method so ``@extension`` can find
#: it. Public-but-underscored: extension authors never read it; tooling may.
CONTRIB_ATTR = "__omnigent_contrib__"

#: The seam-kind markers used internally. Authors use the named decorators.
_SEAM_TOOL = "tool"
_SEAM_POLICY = "policy"
_SEAM_HARNESS = "harness"
_SEAM_BACKGROUND = "background"
_SEAM_ROUTER = "router"
_SEAM_INTERCEPTOR = "tool_interceptor"
_SEAM_SERVICE = "__service__"


def _mark(seam: str, **meta: Any) -> Callable[[Callable], Callable]:
    """Stamp a method with the seam it contributes to + metadata.

    Re-decorating (multiple markers on one method) is rejected so a method maps
    to exactly one seam — the synthesis step assumes a single contribution.
    """

    def deco(fn: Callable) -> Callable:
        if getattr(fn, CONTRIB_ATTR, None) is not None:
            raise TypeError(
                f"{getattr(fn, '__name__', fn)!r} already carries an SDK "
                f"contribution marker; a method maps to exactly one seam"
            )
        info: dict[str, Any] = {"seam": seam, "name": meta.get("name") or fn.__name__}
        info.update(meta)
        setattr(fn, CONTRIB_ATTR, info)
        return fn

    return deco


def tool(name: str | None = None) -> Callable[[Callable], Callable]:
    """Mark a method as a tool factory → contributed to ``tool_factories()``.

    The method body builds and returns the :class:`~omnigent.tools.base.Tool`.
    Its own annotated params are method-injected from the extension's SDK
    container (so a tool can depend on a ``@provides`` service). The synthesised
    ``tool_factories()`` returns ``{name: factory(config) -> Tool}`` — the
    kernel passes the per-tool config positionally as the factory argument.
    """
    return _mark(_SEAM_TOOL, name=name)


def harness(
    name: str | None = None,
    *,
    module_path: str | None = None,
    aliases: tuple[str, ...] = (),
    is_native: bool = False,
    config_schema: Any | None = None,
) -> Callable[[Callable], Callable]:
    """Mark a method as a harness descriptor → contributed to ``harness_descriptors()``.

    Hides :class:`~omnigent.runtime.harnesses.descriptors.HarnessDescriptor`
    construction and the ``{name: () -> descriptor}`` factory shape the
    ``harness`` :class:`~omnigent.pluggable.PluggableRegistry` seam expects. The
    descriptor fields are taken from this decorator's args; the method body need
    not return anything (it may simply ``...``).
    """
    return _mark(
        _SEAM_HARNESS,
        name=name,
        module_path=module_path,
        aliases=aliases,
        is_native=is_native,
        config_schema=config_schema,
    )


def policy(
    name: str | None = None,
    *,
    description: str = "",
    kind: str = "factory",
    params_schema: dict[str, Any] | None = None,
) -> Callable[[Callable], Callable]:
    """Mark a method as a policy → contributed to ``policy_modules()`` + ``POLICY_REGISTRY``.

    ``@extension`` synthesises a module (registered in ``sys.modules``) carrying
    a ``POLICY_REGISTRY`` list-of-dicts and the policy callables, then returns
    that module's dotted name from ``policy_modules()`` — so the existing
    :func:`omnigent.policies.registry.load_registry` scan and dotted-path
    handler resolution work unchanged.

    ``kind`` is ``"factory"`` (the method is called with ``factory_params`` to
    build the policy callable) or ``"callable"`` (the method *is* the policy).
    """
    return _mark(
        _SEAM_POLICY,
        name=name,
        description=description,
        kind=kind,
        params_schema=params_schema,
    )


def background(fn: Callable | None = None) -> Callable:
    """Mark a method as a background-task factory → contributed to ``background_tasks()``.

    Usable bare (``@background``) or called (``@background()``). The synthesised
    ``background_tasks()`` returns ``[factory() -> Awaitable[None]]`` — each
    factory invokes the decorated coroutine method to produce the awaitable the
    server lifespan starts and cancels.
    """
    if fn is not None:
        return _mark(_SEAM_BACKGROUND)(fn)
    return _mark(_SEAM_BACKGROUND)


def router(prefix: str = "") -> Callable[[Callable], Callable]:
    """Mark a method as a router factory → contributed to ``routers()``.

    The method returns a :class:`fastapi.APIRouter` (or a list of them). The
    synthesised ``routers(auth_provider=..., permission_store=...)`` collects
    every ``@router`` method's output into one flat list. The method may declare
    ``auth_provider`` / ``permission_store`` params and they are forwarded.
    """
    return _mark(_SEAM_ROUTER, prefix=prefix)


def tool_interceptor(prefix: str) -> Callable[[Callable], Callable]:
    """Mark a method as a tool-call interceptor → contributed to ``tool_interceptors()``.

    Synthesises ``{prefix: handler}`` — core consults the prefix table before
    runner dispatch (closing the ``memory_tool_intercept`` seam violation,
    Section 12.6). The handler keeps the method's bound signature, e.g.
    ``handler(tool_name, arguments, *, caller_agent_id, caller_department)``.
    """
    return _mark(_SEAM_INTERCEPTOR, prefix=prefix)


def provides(
    key: Any | None = None, *, lifetime: Lifetime = Lifetime.SINGLETON
) -> Callable[[Callable], Callable]:
    """Mark a method as a *service provider* → registered into the SDK DI container.

    The method body is the factory; its own annotated params are injected (so a
    service can depend on another service). If *key* is omitted, the method's
    return-type annotation is used as the key — letting seam factories in the
    same extension depend on the interface::

        @provides(ArtifactStore)            # explicit interface key
        def store(self) -> S3ArtifactStore: ...

        @provides()                          # key inferred from -> annotation
        def clock(self) -> Clock: ...
    """
    return _mark(_SEAM_SERVICE, service_key=key, lifetime=lifetime)


__all__ = [
    "CONTRIB_ATTR",
    "tool",
    "harness",
    "policy",
    "background",
    "router",
    "tool_interceptor",
    "provides",
]
