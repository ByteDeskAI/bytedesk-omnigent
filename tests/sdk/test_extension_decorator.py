"""SDK ``@extension`` synthesis + Protocol-conformance tests (BDP-2508).

The HARD invariant (design doc Section 12.7): a class decorated with
``@extension`` (a) ``isinstance``-conforms to ``OmnigentExtension``, and (b) its
synthesised hooks return the SAME shape as the hand-written Protocol form.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from omnigent.extensions import (
    OmnigentExtension,
    extension_background_factories,
    extension_policy_modules,
    extension_tool_factories,
    extension_tool_interceptors,
)
from omnigent.sdk import (
    background,
    extension,
    harness,
    policy,
    provides,
    router,
    tool,
    tool_interceptor,
)


class _FakeTool:
    def __init__(self, clock=None):
        self.clock = clock


# Module-level service types: DI keys are real, importable types in practice
# (function-local classes can't be resolved under ``from __future__ import
# annotations`` / PEP 563, which stringizes every annotation).
class Clock:
    pass


class SystemClock(Clock):
    pass


# ── 12.7 layering invariant ────────────────────────────────────────────────
def test_isinstance_omnigent_extension():
    @extension(name="iso-ext")
    class IsoExt:
        @tool(name="my_tool")
        def my_tool(self):
            return _FakeTool()

    assert isinstance(IsoExt(), OmnigentExtension) is True


def test_tool_factories_shape_matches_handwritten():
    @extension(name="tool-ext")
    class ToolExt:
        @tool(name="my_tool")
        def my_tool(self):
            return _FakeTool()

    ext = ToolExt()
    factories = ext.tool_factories()
    # Same shape as the Protocol form: {name: factory(config) -> Tool}
    assert "my_tool" in factories
    built = factories["my_tool"]({})
    assert isinstance(built, _FakeTool)


def test_handwritten_equivalent_produces_same_keys():
    """SDK form and hand-written Protocol form yield identical tool-factory keys."""

    class Handwritten:
        name = "hw"

        def routers(self, auth_provider=None, permission_store=None):
            return []

        def tool_factories(self):
            return {"my_tool": lambda _c: _FakeTool()}

    @extension(name="sdk")
    class Sdk:
        @tool(name="my_tool")
        def my_tool(self):
            return _FakeTool()

    assert set(Handwritten().tool_factories()) == set(Sdk().tool_factories())


# ── routers (the REQUIRED hook) ─────────────────────────────────────────────
def test_routers_default_empty_when_no_router_members():
    @extension(name="no-router")
    class NoRouter:
        @tool(name="t")
        def t(self):
            return _FakeTool()

    assert NoRouter().routers() == []
    assert NoRouter().routers(auth_provider=object(), permission_store=object()) == []


def test_router_members_collected_into_routers():
    sentinel_a, sentinel_b = object(), object()

    @extension(name="has-routers")
    class HasRouters:
        @router(prefix="/a")
        def a(self):
            return sentinel_a

        @router(prefix="/b")
        def b(self):
            return [sentinel_b]

    out = HasRouters().routers()
    assert sentinel_a in out and sentinel_b in out
    assert len(out) == 2


def test_router_forwards_auth_when_method_accepts_it():
    captured = {}

    @extension(name="auth-router")
    class AuthRouter:
        @router()
        def r(self, auth_provider=None, permission_store=None):
            captured["auth"] = auth_provider
            captured["perm"] = permission_store
            return []

    ap, ps = object(), object()
    AuthRouter().routers(auth_provider=ap, permission_store=ps)
    assert captured == {"auth": ap, "perm": ps}


# ── policy synthesis ────────────────────────────────────────────────────────
def test_policy_synthesises_module_and_registry():
    @extension(name="policy-ext")
    class PolicyExt:
        @policy(
            name="Per-Agent Rate Limiter",
            description="Limit calls per agent per minute.",
            kind="factory",
            params_schema={"type": "object"},
        )
        def per_agent_rate_limit(self, calls_per_minute: float):
            def _policy(event, context):
                return None

            return _policy

    ext = PolicyExt()
    modules = ext.policy_modules()
    assert len(modules) == 1
    mod_name = modules[0]
    mod = sys.modules[mod_name]
    # The scan target: a POLICY_REGISTRY list-of-dicts with handler dotted-paths.
    registry = mod.POLICY_REGISTRY
    assert isinstance(registry, list) and len(registry) == 1
    entry = registry[0]
    assert entry["handler"] == f"{mod_name}.per_agent_rate_limit"
    assert entry["name"] == "Per-Agent Rate Limiter"
    assert entry["kind"] == "factory"
    assert entry["params_schema"] == {"type": "object"}
    # The handler dotted path resolves to a real, callable factory.
    handler = getattr(mod, "per_agent_rate_limit")
    built = handler(calls_per_minute=5)
    assert callable(built)


def test_policy_registry_loadable_by_real_load_registry():
    """The synthesised module is consumable by the real policy registry scanner."""
    from omnigent.policies import registry as preg

    @extension(name="loadable-policy")
    class LoadablePolicy:
        @policy(name="My Policy", kind="callable", description="d")
        def my_policy(self, event=None, context=None):
            return None

    mod_name = LoadablePolicy().policy_modules()[0]
    preg.load_registry(extra_modules=[mod_name])
    assert preg.is_registered_handler(f"{mod_name}.my_policy")


# ── harness synthesis ───────────────────────────────────────────────────────
def test_harness_descriptors_shape():
    @extension(name="harness-ext")
    class HarnessExt:
        @harness(name="my-harness", module_path="mypkg.my_harness", aliases=("mh",))
        def my_harness(self):
            ...

    descriptors = HarnessExt().harness_descriptors()
    assert "my-harness" in descriptors
    desc = descriptors["my-harness"]()  # {name: () -> HarnessDescriptor}
    from omnigent.runtime.harnesses.descriptors import HarnessDescriptor

    assert isinstance(desc, HarnessDescriptor)
    assert desc.name == "my-harness"
    assert desc.module_path == "mypkg.my_harness"
    assert desc.aliases == ("mh",)


# ── background synthesis ────────────────────────────────────────────────────
def test_background_tasks_factory_shape():
    ran = {}

    @extension(name="bg-ext")
    class BgExt:
        @background
        async def loop(self):
            ran["hit"] = True

    factories = BgExt().background_tasks()
    assert len(factories) == 1
    awaitable = factories[0]()  # background_tasks() -> [factory() -> Awaitable]
    asyncio.run(awaitable)
    assert ran == {"hit": True}


def test_background_callable_form():
    @extension(name="bg-call")
    class BgCall:
        @background()
        async def loop(self):
            return None

    assert len(BgCall().background_tasks()) == 1


# ── tool interceptor synthesis ──────────────────────────────────────────────
def test_tool_interceptors_shape():
    @extension(name="intercept-ext")
    class InterceptExt:
        @tool_interceptor(prefix="memory__")
        def handle(self, tool_name, arguments, *, caller_agent_id, caller_department):
            return ("handled", tool_name, caller_agent_id)

    table = InterceptExt().tool_interceptors()
    assert set(table) == {"memory__"}
    out = table["memory__"]("memory__read", {}, caller_agent_id="a1", caller_department="d")
    assert out == ("handled", "memory__read", "a1")


# ── unused hooks are behaviour-neutral no-ops (back-compat) ─────────────────
def test_unused_hooks_default_to_empty_noops():
    """Unused optional hooks exist (so isinstance passes) but contribute nothing.

    The @runtime_checkable Protocol probes every member, so a conforming class
    must expose all optional hooks — exactly as a complete hand-written
    extension does. The defaults return empty collections, behaviourally
    identical to the hook being absent.
    """

    @extension(name="minimal")
    class Minimal:
        @tool(name="only_tool")
        def only_tool(self):
            return _FakeTool()

    ext = Minimal()
    assert isinstance(ext, OmnigentExtension) is True
    assert ext.routers() == []
    # Optional hooks the author never used contribute nothing:
    assert ext.policy_modules() == []
    assert ext.background_tasks() == []
    assert ext.tool_interceptors() == {}
    assert ext.secret_backends() == []
    # The lifecycle hooks are safe no-ops (must never break boot):
    assert ext.pre_init(object()) is None
    assert ext.post_init(object()) is None
    assert ext.after_init(object()) is None
    # harness_descriptors is NOT a Protocol member (flows via PluggableRegistry),
    # so it stays absent unless the author used @harness:
    assert not hasattr(ext, "harness_descriptors")


def test_author_can_override_synthesised_hook():
    @extension(name="override")
    class Override:
        @tool(name="x")
        def x(self):
            return _FakeTool()

        def routers(self, auth_provider=None, permission_store=None):
            return ["custom"]

    assert Override().routers() == ["custom"]


# ── @provides DI injection into seam factories ──────────────────────────────
def test_provides_injects_into_tool_factory():
    @extension(name="di-ext")
    class DiExt:
        @provides(Clock)
        def clock(self) -> SystemClock:
            return SystemClock()

        @tool(name="echo")
        def echo_tool(self, clock: Clock):
            return _FakeTool(clock=clock)

    built = DiExt().tool_factories()["echo"]({})
    assert isinstance(built.clock, SystemClock)


def test_provides_infers_key_from_return_annotation():
    @extension(name="di-infer")
    class DiInfer:
        @provides()
        def clock(self) -> Clock:
            return Clock()

        @tool(name="echo")
        def echo_tool(self, clock: Clock):
            return _FakeTool(clock=clock)

    built = DiInfer().tool_factories()["echo"]({})
    assert isinstance(built.clock, Clock)


# ── aggregator round-trip (the kernel consumes synthesised hooks identically) ─
def test_kernel_aggregators_consume_synthesised_ext(monkeypatch):
    @extension(name="aggregated")
    class Aggregated:
        @tool(name="agg_tool")
        def agg_tool(self):
            return _FakeTool()

        @policy(name="Agg Policy", kind="callable")
        def agg_policy(self, event=None, context=None):
            return None

        @tool_interceptor(prefix="agg__")
        def agg_intercept(self, tool_name, arguments, **_kw):
            return None

        @background
        async def agg_bg(self):
            return None

    ext = Aggregated()
    monkeypatch.setattr("omnigent.extensions.discover_extensions", lambda: [ext])

    assert "agg_tool" in extension_tool_factories()
    pmods = extension_policy_modules()
    assert any(m.endswith(".aggregated") for m in pmods)
    assert "agg__" in extension_tool_interceptors()
    assert len(extension_background_factories()) == 1


def test_duplicate_marker_rejected():
    with pytest.raises(TypeError):

        @extension(name="dup")
        class Dup:
            @tool(name="a")
            @policy(name="b")
            def both(self):
                return _FakeTool()
