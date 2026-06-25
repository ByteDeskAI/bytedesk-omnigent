# Omnigent SDK Reference

Complete API reference for the shipped **`omnigent.sdk`** facade (BDP-2508, part of
the BDP-2503 microkernel + extension-author SDK refactor).

`omnigent.sdk` is the public, **semver-stable** surface an extension author writes
against. Everything below the facade — the `omnigent.extensions` Protocol, the
`omnigent.pluggable` registries, `omnigent.server.app.create_app` — is the
implementation it hides. The SDK does **not** introduce a parallel discovery,
plugin list, lifecycle, or registry: every decorator *compiles down* to the same
kernel `OmnigentExtension` Protocol hook a hand-written extension would expose.

> **Design source of truth:** `docs/EXTENSION_FRAMEWORK_ANALYSIS.md`, Section 12
> (12.1 module layout, 12.3 `@extension`, 12.4 `Host` builder, 12.5 `@harness`,
> 12.6 `@tool_interceptor`, **12.7 layering invariant**, **12.8 versioning &
> stability**).

---

## Table of contents

- [Quick start](#quick-start)
- [Module layout (Section 12.1)](#module-layout-section-121)
- [The layering invariant (Section 12.7)](#the-layering-invariant-section-127)
- [Class decorator: `@extension`](#class-decorator-extension)
- [Member decorators](#member-decorators)
  - [`@tool`](#tool)
  - [`@policy`](#policy)
  - [`@harness`](#harness)
  - [`@background`](#background)
  - [`@router`](#router)
  - [`@tool_interceptor`](#tool_interceptor)
  - [`@provides`](#provides)
- [The DI container](#the-di-container)
  - [`Container`](#container)
  - [`Lifetime`](#lifetime)
  - [`DIResolutionError`](#diresolutionerror)
- [The `Host` builder](#the-host-builder)
- [Public type re-exports (`omnigent.sdk.types`)](#public-type-re-exports-omnigentsdktypes)
- [Kernel symbols the SDK compiles to (`omnigent.extensions`)](#kernel-symbols-the-sdk-compiles-to-omnigentextensions)
- [Versioning and stability contract (Section 12.8)](#versioning-and-stability-contract-section-128)
- [Symbol index](#symbol-index)

---

## Quick start

```python
# mypackage/extension.py
from omnigent.sdk import extension, tool, policy, background, provides, Host

class Clock: ...
class SystemClock(Clock): ...
class EchoTool:
    def __init__(self, clock: Clock): self.clock = clock

@extension(name="my-extension")
class MyExtension:
    @provides()
    def clock(self) -> Clock:                 # registered into the SDK DI container
        return SystemClock()

    @tool(name="echo")
    def echo_tool(self, clock: Clock):        # clock is method-injected from the container
        return EchoTool(clock)

    @policy(name="Per-Agent Rate Limiter", kind="factory")
    def per_agent_rate_limit(self, calls_per_minute: float):
        def _policy(event, context): ...
        return _policy

    @background
    async def my_maintenance_loop(self): ...
```

```toml
# pyproject.toml — the ONLY non-Python declaration still required (Section 12.3).
# The entry-point string is the irreducible self-registration hook the kernel's
# discover_extensions() reads; the SDK hides everything else.
[project.entry-points."omnigent.extensions"]
my-extension = "mypackage.extension:MyExtension"
```

A class decorated with `@extension` is **still a kernel Protocol object**:

```python
from omnigent.extensions import OmnigentExtension
assert isinstance(MyExtension(), OmnigentExtension)   # the Section 12.7 invariant
```

---

## Module layout (Section 12.1)

| Module | Public surface |
|---|---|
| `omnigent.sdk` | Re-exports the main entry points (everything in the symbol index). Import from here. |
| `omnigent.sdk.extension` | The `@extension` class decorator. |
| `omnigent.sdk.contrib` | The member decorators: `@tool`, `@harness`, `@policy`, `@background`, `@router`, `@tool_interceptor`, `@provides`; plus `CONTRIB_ATTR`. |
| `omnigent.sdk.host` | The `Host` fluent builder. |
| `omnigent.sdk.di` | The DI `Container`, `Lifetime`, `DIResolutionError`. |
| `omnigent.sdk.types` | Lazy public type re-exports (`Tool`, `ToolContext`, `HarnessDescriptor`, `OmnigentExtension`, `PolicyRegistryEntry`). |

`import omnigent.sdk` is **kernel-light**: it pulls in only the decorators, the DI
container, and the builder. Heavy/domain imports (FastAPI, `HarnessDescriptor`,
`create_app`) are deferred inside the synthesised hooks and `Host.build_app()`, so
importing the SDK does not drag the runtime/FastAPI stack onto the import path
(rule 4 — keep the kernel domain-free).

---

## The layering invariant (Section 12.7)

The SDK satisfies exactly one invariant: **it compiles down to the same kernel
Protocol contract.** Two things must always hold:

1. **`isinstance` conformance.** A `@extension`-decorated class's instances satisfy
   `isinstance(obj, omnigent.extensions.OmnigentExtension)`. `OmnigentExtension` is
   a `@runtime_checkable` Protocol, so `@extension` fills *every* declared Protocol
   member — synthesising the ones the author contributed, and adding
   behaviour-neutral empty/no-op defaults for the rest (an empty `{}`/`[]` hook is
   behaviourally identical to the hook being absent, because the kernel aggregators
   merge `[]`/`{}` to nothing).

2. **Same-shape hooks.** Each synthesised hook returns the *same shape* a
   hand-written Protocol implementation returns, so the kernel's existing
   `discover_extensions()` / `install_extensions()` /
   `PluggableRegistry.discover_extensions()` consume it identically.

```python
from omnigent.sdk import extension, tool
from omnigent.extensions import OmnigentExtension

class MyTool: ...

@extension(name="test")
class TestExt:
    @tool(name="my_tool")
    def my_tool(self):
        return MyTool()

ext = TestExt()
assert isinstance(ext, OmnigentExtension)            # (1) conformance
factories = ext.tool_factories()                     # (2) same shape as hand-written
assert "my_tool" in factories
assert isinstance(factories["my_tool"]({}), MyTool)  # factory(config) -> Tool
```

There is **no** parallel discovery mechanism, plugin list, or lifecycle. The SDK is
a compiler from the ergonomic decorator form to the kernel Protocol form.

---

## Class decorator: `@extension`

```python
def extension(name: str, *, requires: tuple[str, ...] = ()) -> Callable[[type], type]
```

*Module:* `omnigent.sdk.extension` · re-exported as `omnigent.sdk.extension`.

Turns a plain class into a class whose **instances** satisfy
`omnigent.extensions.OmnigentExtension`. It scans the class for member-decorator
markers (the `@tool`/`@policy`/… markers stamped under
`omnigent.sdk.contrib.CONTRIB_ATTR`) once at decoration time and **synthesises**
the matching Protocol hook methods.

**Parameters**

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | — (required) | The extension name. Set as `cls.name`; it is what `discover_extensions()` dedups on, what `OMNIGENT_DISABLED_EXTENSIONS` / `Host.disable(...)` match, and the key in synthesised policy module paths. |
| `requires` | `tuple[str, ...]` | `()` | Dependency hint, stored as `cls.requires`. Advisory metadata; not enforced by the kernel today. |

**Returns:** the same class object (decorated in place), with `name`, `requires`,
the collected contribs (`cls._omnigent_sdk_contribs`), and the synthesised Protocol
hooks attached.

**Kernel hook/seam it compiles to.** It produces a class conforming to the
`omnigent.extensions.OmnigentExtension` Protocol. Per the markers present, it
synthesises (only when the class hasn't already hand-written that method — an
author may override any single hook):

| Author used | Synthesised kernel hook | Returned shape |
|---|---|---|
| `@tool` | `tool_factories()` | `{name: factory(config) -> Tool}` |
| `@policy` | `policy_modules()` (+ a synthetic module carrying `POLICY_REGISTRY`) | `[dotted_module_name]` |
| `@harness` | `harness_descriptors()` | `{name: () -> HarnessDescriptor}` |
| `@background` | `background_tasks()` | `[factory() -> Awaitable]` |
| `@router` | `routers(auth_provider=..., permission_store=...)` | `[APIRouter, ...]` |
| `@tool_interceptor` | `tool_interceptors()` | `{prefix: handler}` |
| `@provides` | (no Protocol hook) — DI registration on the extension's per-instance `Container` | — |

`routers()` is **always** synthesised when absent (it is a *required* Protocol
member), returning `[]` if there are no `@router` methods. Every other optional
Protocol member (`secret_backends`, `default_mcp_servers`, `config_descriptors`,
`principal_resolvers`, `assertion_verifiers`, `outbound_credential_providers`,
`authorization_providers`, and the `pre_init`/`post_init`/`after_init` lifecycle
hooks) is filled with a behaviour-neutral default (`{}`, `[]`, or a no-op
`def hook(self, host): None`) so the `@runtime_checkable` structural check passes.

> **Discovery is not the SDK's job.** The author still declares the entry-point in
> `pyproject.toml` (the irreducible self-registration hook). The kernel's existing
> `discover_extensions` finds the class; the SDK only compiles the class down to
> the Protocol contract.

**Example**

```python
from omnigent.sdk import extension, tool

class Greeter: ...

@extension(name="greeter", requires=("omnigent.core",))
class GreeterExt:
    @tool(name="greet")
    def greet(self):
        return Greeter()

ext = GreeterExt()
assert ext.name == "greeter"
assert ext.requires == ("omnigent.core",)
assert "greet" in ext.tool_factories()
```

---

## Member decorators

All member decorators live in `omnigent.sdk.contrib` and are re-exported from
`omnigent.sdk`. Each stamps a small metadata marker (under the attribute named by
`CONTRIB_ATTR == "__omnigent_contrib__"`) onto the method; the `@extension` class
decorator reads those markers and synthesises the matching Protocol hook.

A method maps to **exactly one** seam — stacking two member decorators on one
method raises `TypeError` at decoration time.

> **`CONTRIB_ATTR`** (`omnigent.sdk.contrib.CONTRIB_ATTR`, value
> `"__omnigent_contrib__"`) is the marker attribute name. Public-but-underscored:
> extension authors never read it; tooling (linters, introspection) may.

### `@tool`

```python
def tool(name: str | None = None) -> Callable[[Callable], Callable]
```

Mark a method as a **tool factory**.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str \| None` | `None` | The tool's registration name. If `None`, the method's own name is used. |

**Method contract.** The decorated method **builds and returns the `Tool`**. Its own
annotated parameters are method-injected from the extension's SDK DI container, so a
tool can depend on a `@provides` service.

**Return shape / kernel seam.** Contributes to the synthesised `tool_factories()`,
which returns `{name: factory(config) -> Tool}`. The kernel passes the per-tool
config as the factory's first positional argument; the synthesised factory ignores
it unless the method declares a matching parameter, and method-injects the rest from
the container. Aggregated by `omnigent.extensions.extension_tool_factories()` into
the core builtin tool registry.

```python
from omnigent.sdk import extension, tool, provides

class Clock: ...
class SystemClock(Clock): ...
class EchoTool:
    def __init__(self, clock: Clock): self.clock = clock

@extension(name="echo-ext")
class EchoExt:
    @provides()
    def clock(self) -> Clock:
        return SystemClock()

    @tool(name="echo")
    def echo(self, clock: Clock):     # clock injected from the @provides registration
        return EchoTool(clock)

built = EchoExt().tool_factories()["echo"]({})   # factory(config) -> Tool
assert isinstance(built.clock, SystemClock)
```

### `@policy`

```python
def policy(
    name: str | None = None,
    *,
    description: str = "",
    kind: str = "factory",
    params_schema: dict[str, Any] | None = None,
) -> Callable[[Callable], Callable]
```

Mark a method as a **policy**.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str \| None` | `None` | Policy registry display name. Falls back to the method name. |
| `description` | `str` | `""` | Human-readable description recorded in the registry entry. |
| `kind` | `str` | `"factory"` | `"factory"` — the method is called with `factory_params` to build the policy callable; `"callable"` — the method *is* the policy callable. |
| `params_schema` | `dict \| None` | `None` | JSON-schema for the factory params, recorded on the registry entry. |

**Return shape / kernel seam.** Contributes to the synthesised `policy_modules()`.
At first call, `@extension` synthesises a **real module** registered in
`sys.modules` under `omnigent._sdk_policies.<extension-name>`, carrying a
`POLICY_REGISTRY` list-of-dicts and the policy callables. `policy_modules()` then
returns that module's dotted name. The existing
`omnigent.policies.registry.load_registry` scan (`importlib.import_module` +
`getattr(mod, "POLICY_REGISTRY")`) and the dotted-path handler resolution work
against it **unchanged**. Aggregated by
`omnigent.extensions.extension_policy_modules()`. Each registry entry is a
`PolicyRegistryEntry`-shaped dict (`handler`, `kind`, `name`, `description`,
`params_schema`).

```python
from omnigent.sdk import extension, policy

@extension(name="ratelimit-ext")
class RateLimitExt:
    @policy(
        name="Per-Agent Rate Limiter",
        description="Limit calls per agent per minute.",
        kind="factory",
        params_schema={"type": "object",
                       "properties": {"calls_per_minute": {"type": "number"}},
                       "required": ["calls_per_minute"]},
    )
    def per_agent_rate_limit(self, calls_per_minute: float):
        def _policy(event, context): ...
        return _policy

mods = RateLimitExt().policy_modules()
assert mods and mods[0].startswith("omnigent._sdk_policies.")
```

### `@harness`

```python
def harness(
    name: str | None = None,
    *,
    module_path: str | None = None,
    aliases: tuple[str, ...] = (),
    is_native: bool = False,
    config_schema: Any | None = None,
) -> Callable[[Callable], Callable]
```

Mark a method as a **harness descriptor**.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str \| None` | `None` | Harness name (the descriptor key). Falls back to the method name. |
| `module_path` | `str \| None` | `None` | Import path of the harness implementation module. |
| `aliases` | `tuple[str, ...]` | `()` | Alternate names the harness resolves under. |
| `is_native` | `bool` | `False` | Whether the harness is a native (in-process) descriptor. |
| `config_schema` | `Any \| None` | `None` | Optional config schema for the harness. |

**Method contract.** The method body need not return anything — it may be `...`; the
descriptor fields come entirely from the decorator arguments.

**Return shape / kernel seam.** Contributes to the synthesised
`harness_descriptors()`, returning `{name: () -> HarnessDescriptor}`. The
`HarnessDescriptor` (`omnigent.runtime.harnesses.descriptors.HarnessDescriptor`) is
imported **lazily inside** the synthesised hook (keeps the SDK import kernel-light)
and constructed from the decorator args. This is the shape the `harness`
`PluggableRegistry` seam expects.

```python
from omnigent.sdk import extension, harness

@extension(name="my-harness-ext")
class MyHarnessExt:
    @harness(name="my-harness", module_path="mypackage.my_harness")
    def my_harness(self): ...

descriptors = MyHarnessExt().harness_descriptors()
assert "my-harness" in descriptors
descriptor = descriptors["my-harness"]()      # () -> HarnessDescriptor
assert descriptor.name == "my-harness"
```

### `@background`

```python
def background(fn: Callable | None = None) -> Callable
```

Mark a method as a **background-task factory**. Usable bare (`@background`) or called
(`@background()`).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `fn` | `Callable \| None` | `None` | The decorated coroutine method when used bare; `None` when used as `@background()`. Handles both spellings. |

**Method contract.** The decorated method is the (usually `async`) task coroutine.

**Return shape / kernel seam.** Contributes to the synthesised `background_tasks()`,
returning `[factory() -> Awaitable[None]]`. Each factory invokes the decorated
coroutine to produce the awaitable the **server lifespan** starts as a task and
cancels on shutdown. Aggregated by
`omnigent.extensions.extension_background_factories()`.

```python
import asyncio
from omnigent.sdk import extension, background

@extension(name="maint-ext")
class MaintExt:
    @background
    async def my_maintenance_loop(self):
        while True:
            await asyncio.sleep(300)
            ...

factories = MaintExt().background_tasks()
assert len(factories) == 1
coro = factories[0]()                  # factory() -> Awaitable
assert asyncio.iscoroutine(coro)
coro.close()                            # don't actually run the infinite loop here
```

### `@router`

```python
def router(prefix: str = "") -> Callable[[Callable], Callable]
```

Mark a method as a **router factory**.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `prefix` | `str` | `""` | Stored on the marker for the author's own use; the kernel mounts every extension router under `/v1` regardless. |

**Method contract.** The method returns a `fastapi.APIRouter` (or a list of them, or
`None` for "no routes"). It may declare `auth_provider` / `permission_store`
parameters; the synthesised hook forwards them only if the method accepts them
(back-compat with the kernel's install-time `TypeError` retry).

**Return shape / kernel seam.** Contributes to the synthesised
`routers(auth_provider=None, permission_store=None)`, which collects every
`@router` method's output into one flat `list[APIRouter]`. `routers()` is the one
**required** Protocol member, so `@extension` always synthesises it (returning `[]`
when there are no `@router` methods). `omnigent.extensions.install_extensions`
mounts the returned routers under `/v1`.

```python
from fastapi import APIRouter
from omnigent.sdk import extension, router

@extension(name="health-ext")
class HealthExt:
    @router(prefix="/health")
    def health_router(self, auth_provider=None, permission_store=None):
        r = APIRouter()
        @r.get("/ping")
        def ping(): return {"ok": True}
        return r

routers = HealthExt().routers()
assert len(routers) == 1
```

### `@tool_interceptor`

```python
def tool_interceptor(prefix: str) -> Callable[[Callable], Callable]
```

Mark a method as a **tool-call interceptor** (closes the `memory_tool_intercept`
seam violation, Section 12.6 / ADR-0143 §5).

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `prefix` | `str` | — (required) | The tool-name prefix this handler claims (e.g. `"memory__"`). |

**Method contract.** The method keeps its bound signature, e.g.
`handler(tool_name, arguments, *, caller_agent_id, caller_department)`. It returns a
result, or `None` to fall through to normal runner dispatch.

**Return shape / kernel seam.** Contributes to the synthesised `tool_interceptors()`,
returning `{prefix: handler}`. Core consults the prefix table **before** runner
dispatch. Aggregated by `omnigent.extensions.extension_tool_interceptors()` (which
isolates a misbehaving contributor — one extension whose `tool_interceptors()` raises
is logged and skipped so the rest still register).

```python
from omnigent.sdk import extension, tool_interceptor

@extension(name="memory-ext")
class MemoryExt:
    @tool_interceptor(prefix="memory__")
    def memory_tool_handler(self, tool_name, arguments, *,
                            caller_agent_id, caller_department):
        from mypackage.memory_intercept import execute_memory_tool
        return execute_memory_tool(tool_name, arguments,
                                   caller_agent_id=caller_agent_id,
                                   caller_department=caller_department)

table = MemoryExt().tool_interceptors()
assert "memory__" in table
```

### `@provides`

```python
def provides(
    key: Any | None = None, *, lifetime: Lifetime = Lifetime.SINGLETON
) -> Callable[[Callable], Callable]
```

Mark a method as a **service provider** — registered into the extension's per-instance
SDK DI `Container`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `key` | `Any \| None` | `None` | The DI key. If omitted, the method's `-> ReturnType` annotation is used as the key. A `@provides` with neither a key nor a return annotation raises `TypeError` when the container is first built. |
| `lifetime` | `Lifetime` | `Lifetime.SINGLETON` | The service lifetime (see [`Lifetime`](#lifetime)). |

**Method contract.** The method body **is** the factory; its own annotated parameters
are injected (so a service can depend on another `@provides` service). Registering
under an interface/Protocol `key` while returning a concrete type lets other seam
factories in the same extension depend on the *capability* (Dependency Inversion).

**Kernel hook/seam it compiles to.** `@provides` contributes **no** Protocol hook —
it is SDK-internal wiring. Each `@provides` member is registered as a factory on the
extension's lazily-built per-instance `Container` (`self._omnigent_sdk_container`).
That container is what `@tool` / `@harness` (and other) factories are method-injected
from.

```python
from omnigent.sdk import extension, tool, provides, Lifetime

class ArtifactStore: ...
class S3ArtifactStore(ArtifactStore): ...
class UploadTool:
    def __init__(self, store: ArtifactStore): self.store = store

@extension(name="upload-ext")
class UploadExt:
    @provides(ArtifactStore, lifetime=Lifetime.SINGLETON)   # explicit interface key
    def store(self) -> S3ArtifactStore:
        return S3ArtifactStore()

    @tool(name="upload")
    def upload(self, store: ArtifactStore):    # resolves the S3 impl via the interface
        return UploadTool(store)

tool_obj = UploadExt().tool_factories()["upload"]({})
assert isinstance(tool_obj.store, S3ArtifactStore)
```

---

## The DI container

*Module:* `omnigent.sdk.di` · re-exported from `omnigent.sdk`.

The **extension-author-facing** dependency-injection container. It is intentionally
separate from the internal `omnigent.server.container` core container (gated behind
`OMNIGENT_USE_DI_CONTAINER`, serving the composition root). The SDK container is a
small, stdlib-only, general-purpose resolver an extension uses to wire its **own**
services and have its seam factories (`@tool`, `@harness`, …) method-injected.

Resolution **keys** are *types* (the idiomatic DI form) or plain strings (for
config-ish values).

### `Container`

```python
class Container:
    def __init__(self, parent: "Container | None" = None) -> None
```

A hierarchical DI container. `parent` links it to an enclosing scope (set by
`create_scope()`); a root container has `parent=None`.

#### Registration methods

```python
def register_instance(self, key: Any, instance: Any) -> "Container"
```
Register a ready-made singleton (the most common case). Returns `self` for chaining.

```python
def register_factory(
    self, key: Any,
    factory: Callable[["Container"], Any],
    *, lifetime: Lifetime = Lifetime.SINGLETON,
) -> "Container"
```
Register a factory `factory(container) -> instance` under `key`. Returns `self`.

```python
def register_type(
    self, cls: type, *, key: Any | None = None,
    lifetime: Lifetime = Lifetime.SINGLETON,
) -> "Container"
```
Register `cls`, **auto-wiring its constructor** on resolve (reads `__init__` type
annotations and resolves each recursively). `key` defaults to `cls` but may be a
Protocol/ABC so callers depend on the interface, not the implementation. Returns
`self`.

#### Resolution methods

```python
def resolve(self, key: Any) -> Any
```
Return an instance for `key`, honouring its lifetime. Raises `DIResolutionError` if
`key` is unregistered or a resolution cycle is detected. Singletons live on the
container that *registered* them (so a child scope shares its parent's singletons).

```python
def try_resolve(self, key: Any, default: Any = None) -> Any
```
Like `resolve`, but returns `default` if `key` is unregistered (catches
`DIResolutionError`).

```python
def call(self, fn: Callable[..., T]) -> T
```
Invoke `fn`, injecting its annotated parameters from the container. `self` and
default-bearing params that can't be resolved are left alone. This is the mechanism
behind `@tool` / `@harness` / `@provides` **method injection**. A required (no
default) param whose annotation is unregistered raises `DIResolutionError`.

#### Scopes

```python
def create_scope(self) -> "Container"
```
Return a child container: **shares singletons** with the parent, **isolates**
scoped/transient instances. The FastAPI request-scope analog.

**Kernel hook/seam.** `Container` is **not** a kernel Protocol hook — it is the
DI primitive the `@provides` / `@tool` / `@harness` synthesis uses under the hood,
and a general resolver the author may also use directly. It is ported from the
runnable prototype (`prototype/.../kernel/di.py`).

```python
from omnigent.sdk import Container, Lifetime, DIResolutionError

class Clock: ...
class SystemClock(Clock):
    def now(self): return "tick"
class Report:
    def __init__(self, clock: Clock): self.clock = clock

c = Container()
c.register_type(SystemClock, key=Clock, lifetime=Lifetime.SINGLETON)
c.register_type(Report)                       # __init__(clock: Clock) auto-wired

report = c.resolve(Report)
assert isinstance(report.clock, SystemClock)

# singletons are shared into child scopes; scoped/transient are isolated
scope = c.create_scope()
assert scope.resolve(Clock) is c.resolve(Clock)

# method injection — the @tool/@provides mechanism
def build(clock: Clock): return f"built with {clock.now()}"
assert c.call(build) == "built with tick"

# missing registrations
assert c.try_resolve(str, default="fallback") == "fallback"
try:
    c.resolve(str)
except DIResolutionError as exc:
    pass   # "no registration for str"
```

### `Lifetime`

```python
class Lifetime(Enum):
    SINGLETON = "singleton"   # one instance per (owning) container
    TRANSIENT = "transient"   # a fresh instance on every resolve
    SCOPED    = "scoped"      # one instance per scope (per-request analog)
```

The service lifetime — how often the container builds a fresh instance. Passed to
`register_factory` / `register_type` and to `@provides(lifetime=...)`.

- **`SINGLETON`** — built once, cached on the container that registered it; shared
  into child scopes.
- **`TRANSIENT`** — built fresh on every `resolve`.
- **`SCOPED`** — built once per scope; a `create_scope()` child gets its own
  instance, isolated from the parent and siblings.

### `DIResolutionError`

```python
class DIResolutionError(Exception): ...
```

Raised when a dependency cannot be resolved — an **unregistered** key, a required
(no-default) injected param whose type is unregistered, or a **resolution cycle**
(the container's cycle guard raises rather than recursing forever). Caught and
suppressed by `try_resolve`.

---

## The `Host` builder

*Module:* `omnigent.sdk.host` · re-exported from `omnigent.sdk`.

```python
class Host:
    @staticmethod
    def build() -> "Host"
```

A fluent composition-root builder over `omnigent.server.app.create_app`. It is a
**named-argument collector** that produces the *same* `create_app()` call a
hand-written composition root would — **not** a second source of truth, and **not** a
parallel discovery/lifecycle/registry. For test setups, embedded deployments, and
lightweight CLI tools that want to compose a host without spelling out the
15-parameter `create_app` signature (Section 12.4).

`Host.build()` starts a fresh builder. The builder methods are chainable (each
returns `self`); `build_app()` is the terminal that produces a `FastAPI` app.

#### Builder methods

```python
def with_store(self, **stores: Any) -> "Host"
```
Set one or more store / collaborator `create_app` arguments (e.g.
`conversation_store=`, `artifact_store=`, `agent_store=`, …). An unknown kwarg
raises `TypeError` immediately (a typo fails loudly instead of being silently
dropped into `create_app`). See the allow-list below.

```python
def with_auth(self, *, auth_provider: Any = None, permission_store: Any = None) -> "Host"
```
Set the auth provider and (optionally) the permission store. `None` arguments are
skipped.

```python
def with_option(self, **options: Any) -> "Host"
```
Set any other `create_app` keyword argument (`admins`, `policy_modules`,
`allowed_domains`, `sandbox_config`, `extra_routers`, …). Same allow-list validation
as `with_store`.

```python
def with_extension(self, ext: Any) -> "Host"
```
Add an explicit extension instance to install, **deduped by `ext.name`** (a second
add of the same name is a no-op).

```python
def disable(self, *names: str) -> "Host"
```
Disable extensions by name — the `OMNIGENT_DISABLED_EXTENSIONS` analog. Empty/falsy
names are ignored.

```python
def build_app(self) -> Any   # -> fastapi.FastAPI
```
The terminal. Compiles the builder down to a single `create_app(**params)` call and
returns the resulting `FastAPI`. `create_app` is imported **lazily** here (it drags
in the full FastAPI/domain stack), so importing `omnigent.sdk` stays kernel-light.

**Kernel hook/seam it compiles to.** `build_app()` calls
`omnigent.server.app.create_app(**collected_params)`. Explicit `with_extension`
instances and `disable` names are fed through the **kernel's own seams**, not a
parallel list, for the duration of the `create_app` call:

- `with_extension` instances are **prepended** to `omnigent.extensions.discover_extensions()`'s
  result (via a temporary monkeypatch restored on exit), so `install_extensions` and
  every `PluggableRegistry.discover_extensions` sees them — deduped against the
  discovered set by `name`.
- `disable(...)` names are **unioned** into the
  `omnigent.extensions.DISABLED_ENV_VAR` (`OMNIGENT_DISABLED_EXTENSIONS`) environment
  variable for the duration of the build, then restored.

**Valid `create_app` parameter names** (the frozen allow-list `with_store` /
`with_auth` / `with_option` validate against):

```
agent_store, file_store, conversation_store, artifact_store, agent_cache,
runner_tunnel_tokens, comment_store, policy_store, permission_store, auth_provider,
host_store, account_store, extra_routers, policy_modules, admins, allowed_domains,
sandbox_config
```

```python
from omnigent.sdk import Host

app = (
    Host.build()
    .with_store(conversation_store=store, artifact_store=art)
    .with_auth(auth_provider=provider, permission_store=perms)
    .with_extension(MyExtension())
    .with_option(admins=["alice@example.com"])
    .disable("omnigent.realtime")        # OMNIGENT_DISABLED_EXTENSIONS analog
    .build_app()                          # -> FastAPI
)
```

> **ServiceStack analog:** `new AppHost(...).Init().Start(...)` — the fluent chain
> that builds the host.

---

## Public type re-exports (`omnigent.sdk.types`)

A stable, **kernel-light** surface of the types an extension author annotates
against. Heavy domain modules are imported **lazily** via PEP 562 module-level
`__getattr__` — `import omnigent.sdk.types` does not drag the FastAPI/runtime stack
onto the import path; only the attribute actually accessed is imported.
Annotation-only use under `TYPE_CHECKING` costs nothing.

| Re-export | Resolves to | Use |
|---|---|---|
| `OmnigentExtension` | `omnigent.extensions.OmnigentExtension` | The kernel Protocol a `@extension` class conforms to; for `isinstance` checks and type hints. |
| `Tool` | `omnigent.tools.base.Tool` | The type a `@tool` factory returns. |
| `ToolContext` | `omnigent.tools.base.ToolContext` | The context a tool's `run`/`execute` receives. |
| `HarnessDescriptor` | `omnigent.runtime.harnesses.descriptors.HarnessDescriptor` | The descriptor a `@harness` synthesises; for type hints. |
| `PolicyRegistryEntry` | `omnigent.policies.registry.PolicyRegistryEntry` | The shape of an entry in a synthesised `POLICY_REGISTRY`. |

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from omnigent.sdk.types import Tool, ToolContext   # zero runtime cost

# Or access at runtime — lazily resolved on first access:
from omnigent.sdk.types import OmnigentExtension
```

A name not in this set raises `AttributeError`; a name whose underlying optional
dependency is absent raises `AttributeError` with the resolution failure attached.

---

## Kernel symbols the SDK compiles to (`omnigent.extensions`)

These are **kernel** symbols (semi-stable, see Section 12.8), not part of the
`omnigent.sdk` facade — but the SDK compiles directly onto them, and the
microkernel refactor (BDP-2504) added several this run. Documented here so the
mapping from each decorator to its seam is complete.

### The `OmnigentExtension` Protocol

`@runtime_checkable` Protocol with one **required** member (`name: str`, `routers(...)`)
and many **optional**, `hasattr`-probed capability methods. The microkernel refactor
added these **optional Protocol methods** (an extension that omits one is simply
skipped — never `getattr`-defaulted, preserving back-compat):

| Optional Protocol method | Stage / role | Aggregator |
|---|---|---|
| `pre_init(self, host) -> None` | Lifecycle stage 1 — before any router is mounted (create tables, validate env, fail fast). A raise drops *only* this extension from later stages. | `install_extensions` |
| `post_init(self, host) -> None` | Lifecycle stage 3 — after all healthy routers mounted (wire inter-extension deps). | `install_extensions` |
| `after_init(self, host) -> None` | Lifecycle stage 4 — final settle, before the lifespan/background tasks start. | `install_extensions` |
| `tool_interceptors(self) -> {prefix: handler}` | Tool-call interception before runner dispatch. | `extension_tool_interceptors()` |

`@extension` fills all of these (with the synthesised `tool_interceptors` when
`@tool_interceptor` is present, and behaviour-neutral no-op lifecycle hooks
otherwise) so the structural `isinstance` check passes.

### Discovery / lookup helpers (BDP-2504)

| Symbol | Signature | Role |
|---|---|---|
| `DISABLED_ENV_VAR` | `str = "OMNIGENT_DISABLED_EXTENSIONS"` | Comma-separated extension *names* to disable (the `EnableFeatures` analog). The `Host.disable(...)` target. |
| `_disabled_from_env()` | `() -> set[str]` | Internal: parse `DISABLED_ENV_VAR` into a name set (empty/unset → no-op filter). |
| `get_extension(name)` | `(str) -> OmnigentExtension \| None` | Lookup over `discover_extensions()` (the `appHost.GetPlugin<T>()` analog); honors the disabled set; re-runs discovery per call. |
| `assert_extension(name)` | `(str) -> OmnigentExtension` | Like `get_extension` but raises `LookupError` if absent (the `appHost.AssertPlugin<T>()` analog). |
| `extension_tool_interceptors()` | `() -> dict` | Aggregate every extension's `tool_interceptors()` into one `{prefix: handler}` table; isolates a raising contributor (logged + skipped). |

---

## Versioning and stability contract (Section 12.8)

- **SDK (`omnigent/sdk/`) — the public API surface. Semver-stable.** Every name
  re-exported from `omnigent.sdk` (and the `omnigent.sdk.types` re-exports) is
  stable: a breaking change to a decorator signature, a `Host`/`Container` method,
  or a public type re-export requires a **major** version bump. Third-party
  extension authors depend on this surface and must not be broken by kernel
  refactors. **Any import from `omnigent.sdk.*` is stable.**

- **Kernel (`omnigent.extensions`, `omnigent.pluggable`, `omnigent.server.lifespan_phases`)
  — the implementation. Semi-stable.** It may churn between minors as seams are
  added or aggregators are generalised; a change that breaks the kernel's own
  Protocol shape is a breaking change to the SDK (it propagates up), so the kernel
  Protocol surface is semver-anchored too — but **via** the SDK surface, with a
  deprecation cycle. Breaking changes require a major bump but are permissible
  between minors with deprecation.

- **First-party plugins / internals (`omnigent.runtime.*`, `omnigent.inner.*`,
  `omnigent.server.routes.*`, `omnigent.harnesses`, `omnigent.tools`, …) — internal.**
  Their seam-registration APIs are not public and may change without notice, as long
  as the kernel seam contracts are preserved.

**The practical rule:** import from `omnigent.sdk.*` and you are on the stable
contract. Anything you reach through `omnigent.extensions` / `omnigent.pluggable` is
semi-stable. Anything under `omnigent.runtime.*` / `omnigent.inner.*` /
`omnigent.server.routes.*` is internal and may move.

---

## Symbol index

Importable from **`omnigent.sdk`** (`omnigent.sdk.__all__`):

| Symbol | Kind | Defined in | Section |
|---|---|---|---|
| `extension` | class decorator | `omnigent.sdk.extension` | [`@extension`](#class-decorator-extension) |
| `tool` | member decorator | `omnigent.sdk.contrib` | [`@tool`](#tool) |
| `harness` | member decorator | `omnigent.sdk.contrib` | [`@harness`](#harness) |
| `policy` | member decorator | `omnigent.sdk.contrib` | [`@policy`](#policy) |
| `background` | member decorator | `omnigent.sdk.contrib` | [`@background`](#background) |
| `router` | member decorator | `omnigent.sdk.contrib` | [`@router`](#router) |
| `tool_interceptor` | member decorator | `omnigent.sdk.contrib` | [`@tool_interceptor`](#tool_interceptor) |
| `provides` | member decorator | `omnigent.sdk.contrib` | [`@provides`](#provides) |
| `Host` | fluent builder | `omnigent.sdk.host` | [`Host`](#the-host-builder) |
| `Container` | DI container | `omnigent.sdk.di` | [`Container`](#container) |
| `Lifetime` | enum | `omnigent.sdk.di` | [`Lifetime`](#lifetime) |
| `DIResolutionError` | exception | `omnigent.sdk.di` | [`DIResolutionError`](#diresolutionerror) |

Importable from **`omnigent.sdk.types`** (lazy re-exports): `OmnigentExtension`,
`Tool`, `ToolContext`, `HarnessDescriptor`, `PolicyRegistryEntry`.

Also public in `omnigent.sdk.contrib`: `CONTRIB_ATTR` (`"__omnigent_contrib__"`).

Kernel symbols the SDK compiles onto (`omnigent.extensions`, semi-stable):
`OmnigentExtension` (+ optional Protocol methods `pre_init`, `post_init`,
`after_init`, `tool_interceptors`), `DISABLED_ENV_VAR`, `get_extension`,
`assert_extension`, `extension_tool_interceptors`.
