"""SDK — the ``@extension`` class decorator (BDP-2508).

Turns a plain class into a class whose instances satisfy the kernel's
:class:`omnigent.kernel.extensions.OmnigentExtension` Protocol, by scanning for the
member-decorator markers (:mod:`omnigent.sdk.contrib`) and *synthesising* the
matching Protocol hook methods. The synthesised hooks return the **same shape**
a hand-written Protocol implementation would (the Section 12.7 invariant):

  * ``@tool``  → ``tool_factories() -> {name: factory(config) -> Tool}``
  * ``@policy`` → ``policy_modules() -> [synthetic_module]`` whose module carries
    a ``POLICY_REGISTRY`` list-of-dicts + the policy callables (so the existing
    :func:`omnigent.policies.registry.load_registry` scan + dotted-path handler
    resolution work unchanged).
  * ``@harness`` → ``harness_descriptors() -> {name: () -> HarnessDescriptor}``
  * ``@background`` → ``background_tasks() -> [factory() -> Awaitable]``
  * ``@router`` → ``routers(auth_provider=..., permission_store=...) -> [APIRouter]``
  * ``@tool_interceptor`` → ``tool_interceptors() -> {prefix: handler}``
  * ``@provides`` → DI registration on the extension's per-instance container.

Only hooks the author actually used are synthesised — and only when the class
does not already define that method by hand (so an author may override any one
hook). A hook that *isn't* synthesised is simply absent, which the kernel's
``hasattr``-probe aggregators skip (back-compatible by construction).

Heavy / domain imports (``HarnessDescriptor``) are deferred inside the
synthesised methods so importing :mod:`omnigent.sdk` stays kernel-light.
"""

from __future__ import annotations

import sys
import types as _pytypes
from collections.abc import Callable
from inspect import Parameter, signature
from typing import Any, get_type_hints

from .contrib import (
    _SEAM_BACKGROUND,
    _SEAM_HARNESS,
    _SEAM_INTERCEPTOR,
    _SEAM_POLICY,
    _SEAM_ROUTER,
    _SEAM_SERVICE,
    _SEAM_TOOL,
    CONTRIB_ATTR,
)
from .di import Container

#: Synthetic policy modules live under this dotted prefix in ``sys.modules``.
_SYNTH_POLICY_PKG = "omnigent._sdk_policies"

#: Optional ``OmnigentExtension`` Protocol members and the empty/no-op value a
#: not-contributed hook returns. The ``@runtime_checkable`` Protocol probes for
#: *every* declared member, so a class must expose all of them to satisfy
#: ``isinstance`` (the Section 12.7 invariant) — exactly as a complete
#: hand-written extension (e.g. ``BytedeskExtension``) does. A no-op default
#: returning the empty collection is behaviourally identical to the hook being
#: absent (the kernel aggregators merge ``[]`` / ``{}`` to nothing), so this adds
#: no behaviour — it only makes the structural check pass.
_DICT_HOOKS = (
    "tool_factories",
    "tool_interceptors",
    "assertion_verifiers",
    "outbound_credential_providers",
    "authorization_providers",
)
_LIST_HOOKS = (
    "policy_modules",
    "secret_backends",
    "default_mcp_servers",
    "background_tasks",
    "config_descriptors",
    "principal_resolvers",
)
_LIFECYCLE_HOOKS = ("pre_init", "post_init", "after_init")


def _collect_contribs(cls: type) -> list[tuple[str, dict[str, Any]]]:
    """Return ``[(attr_name, meta), ...]`` for every marked member of *cls*."""
    contribs: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for attr_name in dir(cls):
        if attr_name in seen:
            continue
        member = getattr(cls, attr_name, None)
        meta = getattr(member, CONTRIB_ATTR, None)
        if meta is not None:
            contribs.append((attr_name, meta))
            seen.add(attr_name)
    return contribs


def _ensure_container(self: Any) -> Container:
    """Lazily build + memoise the extension's per-instance SDK DI container.

    Walks the instance's ``@provides`` members and registers each as a factory
    whose own params are method-injected. Built once per instance, on first use
    of any synthesised hook that needs injection.
    """
    container = getattr(self, "_omnigent_sdk_container", None)
    if container is not None:
        return container
    container = Container()
    for attr_name, meta in _contribs_of(self):
        if meta["seam"] != _SEAM_SERVICE:
            continue
        bound = getattr(self, attr_name)
        key = meta.get("service_key")
        if key is None:
            key = _return_annotation(bound)
        if key is None:
            raise TypeError(
                f"@provides on {type(self).__name__}.{attr_name} needs a key or a "
                f"'-> ReturnType' annotation to use as the DI key"
            )
        container.register_factory(
            key,
            lambda c, b=bound: c.call(b),
            lifetime=meta["lifetime"],
        )
    object.__setattr__(self, "_omnigent_sdk_container", container)
    return container


def _contribs_of(self: Any) -> list[tuple[str, dict[str, Any]]]:
    """The contribs collected at decoration time, stashed on the class."""
    return getattr(type(self), "_omnigent_sdk_contribs", [])


def _return_annotation(fn: Callable) -> Any | None:
    """Best-effort read of *fn*'s ``-> ReturnType`` annotation as a DI key.

    Prefers :func:`typing.get_type_hints` (resolves string annotations against
    the function's module globals) but falls back to the raw ``__annotations__``
    value when resolution fails — e.g. a *function-local* class used as the
    return type, which ``get_type_hints`` cannot see. The raw value is already
    the class object for a non-stringized annotation, so it is a valid key.
    """
    try:
        resolved = get_type_hints(fn).get("return")
        if resolved is not None:
            return resolved
    except Exception:  # noqa: BLE001 — annotation eval is best-effort
        pass
    raw = getattr(fn, "__annotations__", {}).get("return")
    # A still-stringized raw annotation is not a usable key; ignore it.
    return raw if not isinstance(raw, str) else None


def _inject_call(self: Any, bound: Callable) -> Any:
    """Call *bound* with its annotated params injected from the SDK container."""
    return _ensure_container(self).call(bound)


# ── synthesised hook builders ──────────────────────────────────────────────
def _make_tool_factories() -> Callable[[Any], dict]:
    def tool_factories(self) -> dict[str, Callable[[object], object]]:
        out: dict[str, Callable[[object], object]] = {}
        for attr_name, meta in _contribs_of(self):
            if meta["seam"] != _SEAM_TOOL:
                continue
            bound = getattr(self, attr_name)
            # Factory shape the kernel expects: factory(config) -> Tool.
            # The per-tool config is ignored unless the method declares it; the
            # method's annotated params are method-injected from the container.
            out[meta["name"]] = lambda _config=None, b=bound, s=self: _inject_call(s, b)
        return out

    return tool_factories


def _make_background_tasks() -> Callable[[Any], list]:
    def background_tasks(self):
        out: list[Callable[[], Any]] = []
        for attr_name, meta in _contribs_of(self):
            if meta["seam"] != _SEAM_BACKGROUND:
                continue
            bound = getattr(self, attr_name)
            # background_tasks() returns factories; calling one yields the awaitable.
            out.append(lambda b=bound: b())
        return out

    return background_tasks


def _make_tool_interceptors() -> Callable[[Any], dict]:
    def tool_interceptors(self) -> dict[str, Callable[..., object]]:
        out: dict[str, Callable[..., object]] = {}
        for attr_name, meta in _contribs_of(self):
            if meta["seam"] != _SEAM_INTERCEPTOR:
                continue
            out[meta["prefix"]] = getattr(self, attr_name)
        return out

    return tool_interceptors


def _make_harness_descriptors() -> Callable[[Any], dict]:
    def harness_descriptors(self) -> dict[str, Callable[[], object]]:
        from omnigent.runtime.harnesses.descriptors import HarnessDescriptor

        out: dict[str, Callable[[], object]] = {}
        for _attr_name, meta in _contribs_of(self):
            if meta["seam"] != _SEAM_HARNESS:
                continue
            desc = HarnessDescriptor(
                name=meta["name"],
                module_path=meta.get("module_path"),
                aliases=meta.get("aliases", ()),
                is_native=meta.get("is_native", False),
                config_schema=meta.get("config_schema"),
            )
            out[desc.name] = lambda d=desc: d
        return out

    return harness_descriptors


def _make_routers() -> Callable[..., list]:
    def routers(self, auth_provider=None, permission_store=None) -> list:
        out: list = []
        for attr_name, meta in _contribs_of(self):
            if meta["seam"] != _SEAM_ROUTER:
                continue
            bound = getattr(self, attr_name)
            result = _call_router_factory(
                self,
                bound,
                auth_provider=auth_provider,
                permission_store=permission_store,
            )
            if result is None:
                continue
            if isinstance(result, list):
                out.extend(result)
            else:
                out.append(result)
        return out

    return routers


def _call_router_factory(
    self: Any,
    bound: Callable,
    *,
    auth_provider: Any,
    permission_store: Any,
) -> Any:
    """Invoke a ``@router`` method with SDK DI plus server-provided args."""
    kwargs = _ensure_container(self)._build_kwargs(bound)
    sig = signature(bound)
    params = sig.parameters
    if _accepts_named(params, "auth_provider"):
        kwargs["auth_provider"] = auth_provider
    if _accepts_named(params, "permission_store"):
        kwargs["permission_store"] = permission_store
    return bound(**kwargs)


def _accepts_named(params: dict[str, Parameter], name: str) -> bool:
    """Return whether a callable signature accepts a named keyword."""
    if name in params:
        return True
    return any(param.kind is Parameter.VAR_KEYWORD for param in params.values())


def _make_policy_modules(ext_name: str) -> Callable[[Any], list]:
    def policy_modules(self) -> list[str]:
        mod_name = _build_policy_module(self, ext_name)
        return [mod_name] if mod_name else []

    return policy_modules


def _build_policy_module(self: Any, ext_name: str) -> str | None:
    """Synthesise (once) a real module carrying ``POLICY_REGISTRY`` + callables.

    Registered in ``sys.modules`` under ``omnigent._sdk_policies.<ext_name>`` so
    that both the ``load_registry`` scan (``importlib.import_module`` +
    ``getattr(mod, "POLICY_REGISTRY")``) and dotted-path handler resolution
    (``getattr(import_module(module), attr)``) work against it unchanged.
    """
    safe = _safe_mod_segment(ext_name)
    mod_name = f"{_SYNTH_POLICY_PKG}.{safe}"
    existing = sys.modules.get(mod_name)
    registry: list[dict[str, Any]] = []
    policy_members = [
        (attr_name, meta)
        for attr_name, meta in _contribs_of(self)
        if meta["seam"] == _SEAM_POLICY
    ]
    if not policy_members:
        return None
    if existing is None:
        _ensure_synth_pkg()
        existing = _pytypes.ModuleType(mod_name)
        existing.__dict__["__package__"] = _SYNTH_POLICY_PKG
        sys.modules[mod_name] = existing
    for attr_name, meta in policy_members:
        bound = getattr(self, attr_name)
        existing.__dict__[attr_name] = bound  # the resolvable handler callable
        registry.append(
            {
                "handler": f"{mod_name}.{attr_name}",
                "kind": meta.get("kind", "factory"),
                "name": meta["name"],
                "description": meta.get("description", ""),
                "params_schema": meta.get("params_schema"),
            }
        )
    existing.__dict__["POLICY_REGISTRY"] = registry
    return mod_name


def _ensure_synth_pkg() -> None:
    """Make ``omnigent._sdk_policies`` importable as a namespace-ish package."""
    if _SYNTH_POLICY_PKG in sys.modules:
        return
    pkg = _pytypes.ModuleType(_SYNTH_POLICY_PKG)
    pkg.__dict__["__path__"] = []  # mark as a package so submodule import works
    sys.modules[_SYNTH_POLICY_PKG] = pkg


def _safe_mod_segment(name: str) -> str:
    """Turn an arbitrary extension name into a valid module-path segment."""
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name) or "ext"


# ── the class decorator ─────────────────────────────────────────────────────
def extension(name: str, *, requires: tuple[str, ...] = ()) -> Callable[[type], type]:
    """Class decorator: make a class's instances conform to ``OmnigentExtension``.

    It sets ``name`` (and a ``requires`` hint), collects the member-decorator
    markers once at decoration time, and synthesises the matching Protocol hook
    methods — but only for hooks the author actually used, and only when the
    class hasn't already defined that hook by hand.

    Discovery is **not** the SDK's job: the author still declares the
    entry-point in ``pyproject.toml`` (the irreducible self-registration hook),
    and the kernel's existing ``discover_extensions`` finds the class. The SDK
    only compiles the class down to the Protocol contract.
    """

    def deco(cls: type) -> type:
        cls.name = name  # type: ignore[attr-defined]
        cls.requires = requires  # type: ignore[attr-defined]

        contribs = _collect_contribs(cls)
        cls._omnigent_sdk_contribs = contribs  # type: ignore[attr-defined]

        seams = {meta["seam"] for _a, meta in contribs}

        def _set_if_absent(attr: str, fn: Callable) -> None:
            if attr not in cls.__dict__:
                setattr(cls, attr, fn)

        if _SEAM_TOOL in seams:
            _set_if_absent("tool_factories", _make_tool_factories())
        if _SEAM_POLICY in seams:
            _set_if_absent("policy_modules", _make_policy_modules(name))
        if _SEAM_HARNESS in seams:
            _set_if_absent("harness_descriptors", _make_harness_descriptors())
        if _SEAM_BACKGROUND in seams:
            _set_if_absent("background_tasks", _make_background_tasks())
        if _SEAM_INTERCEPTOR in seams:
            _set_if_absent("tool_interceptors", _make_tool_interceptors())
        # ``routers`` is REQUIRED by the Protocol. Synthesise it whenever the
        # author didn't write one — returning the @router outputs, or [] if none.
        _set_if_absent("routers", _make_routers())

        # Fill the remaining optional Protocol members with empty/no-op defaults
        # so the @runtime_checkable structural check passes (Section 12.7) — a
        # behaviour-neutral default (empty collection / no-op lifecycle hook),
        # identical to what a complete hand-written extension exposes.
        for hook in _DICT_HOOKS:
            _set_if_absent(hook, lambda _self: {})
        for hook in _LIST_HOOKS:
            _set_if_absent(hook, lambda _self: [])
        for hook in _LIFECYCLE_HOOKS:
            _set_if_absent(hook, lambda _self, _host: None)

        return cls

    return deco


__all__ = ["extension"]
