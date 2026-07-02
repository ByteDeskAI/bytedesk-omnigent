# Omnigent Extension Developer Guide

> Audience: people writing an **omnigent extension** — a tool, policy, harness,
> background loop, router, tool interceptor, or pluggable service — and
> packaging it so the server discovers it without ever editing core.
>
> This is the *narrative* guide. For the exhaustive symbol-by-symbol reference
> (every decorator argument, every `Host` method, every public type), see
> [`SDK_REFERENCE.md`](./SDK_REFERENCE.md). For the architectural rationale and
> the kernel/core/extension inventory, see
> [`EXTENSION_FRAMEWORK_ANALYSIS.md`](./EXTENSION_FRAMEWORK_ANALYSIS.md) — this
> guide cross-links its Sections 8–13 throughout. For a runnable, stdlib-only
> proof of the whole shape, see [`../prototype/`](../prototype/) (`python3
> run_demo.py`).
>
> Everything in this guide is grounded in the real shipped code under
> [`../omnigent/sdk/`](../omnigent/sdk/) and the prototype. The import you write
> is always `from omnigent.sdk import …`.

---

## 1. The three-tier model (and why)

Omnigent is a **microkernel**. There are exactly three tiers, and the boundary
between them is the whole point.

```
┌─────────────────────────────────────────────────────────────────┐
│ EXTENSIONS   third-party (e.g. bytedesk_omnigent). Self-register  │
│              via an entry-point. Use the same SDK + same contract │
│              as core. No privilege gap.                           │
├─────────────────────────────────────────────────────────────────┤
│ CORE         kernel + first-party plugins (stores, tools,         │
│              harnesses, policies, routes…). Each is an ordinary   │
│              extension. "core = kernel + a curated set of these." │
├─────────────────────────────────────────────────────────────────┤
│ KERNEL       boot + plugin host + DI. Domain-free; never changes  │
│              when you add a capability.                           │
│  omnigent/kernel/extensions.py      OmnigentExtension Protocol    │
│  omnigent/kernel/pluggable/         PluggableRegistry, SEAMS      │
│  omnigent/kernel/lifespan_phases.py LifespanOrchestrator (DAG)    │
│  omnigent/server/container.py       Core DI container facade      │
│  omnigent/server/app_context.py     ServerAppContext facade       │
│  omnigent/sdk/               the developer facade over all of it  │
└─────────────────────────────────────────────────────────────────┘
```

**Kernel** (`EXTENSION_FRAMEWORK_ANALYSIS.md` §8). The minimum set of files that
can boot the system and host plugins. It knows about an extension only that it
(a) has a `name` and (b) can contribute capabilities (routers, tools, …). It
imports no domain types — agents, conversations, tools, harnesses are all
invisible to it. This is what makes it the kernel: *it never changes when you
add a capability.*

**Core** (§9, §10). `CORE = KERNEL + first-party plugins`. Stores, the builtin
tools, the 12 harness descriptors, the builtin policy modules, the route groups
— each registers itself through the **same** `OmnigentExtension` /
`PluggableRegistry` seam a third party uses. There is no privileged "core"
wiring. This is the *dogfooding argument* (§9.2): if the seam can host omnigent's
own auth, tools, and harnesses, it can host yours.

**Extensions** (§10). `bytedesk_omnigent` and any future third party. They live
*entirely outside* core, self-register via the `omnigent.extensions`
entry-point, and use the identical contract. The prototype proves this directly:
its `bytedesk_ext.py` overrides the core `Clock` interface and adds an `audit`
tool using nothing core does not also use.

**Why this matters to you as an author:** you are never "plugging into" a
second-class extension API. You contribute through the *same* seams the platform
contributes through. Anything core can do, your extension can do — including
*replacing* a core service (see §7 below).

The SDK (`omnigent/sdk/`) is the **Facade** over all of this. You import
decorators; the kernel Protocol, the registries, the discovery call, and the
`create_app` signature stay hidden. The layering invariant
(`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.7) guarantees a `@extension`-decorated
class *is* a kernel `OmnigentExtension` — it compiles down to the contract, it
does not wrap it.

---

## 2. The lifecycle stages

The kernel fires four stages **in order, across all extensions**:

```
pre_init  →  register  →  post_init  →  after_init
```

This is the **Template Method** pattern: the *sequence* is fixed in the kernel
(`install_extensions` in `omnigent.kernel.extensions`); each extension fills in only
the steps it cares about. Every stage except `register` is optional and probed
with `hasattr` — an extension that omits a stage is simply skipped for it, never
`getattr`-defaulted. (See `EXTENSION_FRAMEWORK_ANALYSIS.md` §4.3.)

| Stage | When it fires | What it's for | Failure semantics |
|---|---|---|---|
| **`pre_init(host)`** | *Before* any router is mounted | Create DB tables, validate required env/secrets, **fail fast**. | An exception marks your extension *unhealthy* — it is dropped from every later stage. Never kills server boot. |
| **`register`** (= `routers()`) | Mount routers under `/v1` | Contribute your `APIRouter`s. This is the self-registration step. | A router failure is isolated; healthy extensions still mount. |
| **`post_init(host)`** | *After* all healthy extensions' routers are mounted | Wire cross-extension dependencies now that every contribution is in place. | Logged-and-skipped (observability only). |
| **`after_init(host)`** | After `post_init` for every healthy extension | The final settle hook, before the server lifespan (background tasks) starts. | Logged-and-skipped. |

A few specifics that trip people up:

- **There is no `register()` method to write.** The real `OmnigentExtension`
  Protocol expresses "register" as `routers()` (the required hook) plus the
  capability hooks (`tool_factories()`, `policy_modules()`, …). The SDK
  synthesises these from your `@tool` / `@router` / … decorators. The *prototype*
  has an explicit `register(host)` to make the staging legible in ~700 lines;
  the real kernel mounts routers and merges capability hooks at the register
  stage instead.
- **Each lifecycle hook receives `host`** (the FastAPI `app`) so it can stash
  cross-extension state. In the SDK form you write `def pre_init(self, host):`.
- **`pre_init` is the only fail-fast stage.** Raise there to abort *your*
  extension cleanly. `post_init` / `after_init` exceptions are swallowed and
  logged — they must never break boot.
- **Background tasks are not a lifecycle hook.** They start *after* `after_init`,
  in the server lifespan, and are cancelled on shutdown. Use `@background` (§5).

The real `install_extensions` is the authority — read it in
`omnigent/kernel/extensions.py` (the four `# ── Stage N` blocks). The prototype's
`kernel/host.py` `boot()` shows the same staged loop in miniature.

---

## 3. Write your first extension

Build it up one decorator at a time. Every import is `from omnigent.sdk import …`.

### Step 1 — the class

`@extension` is the only class-level decorator. It takes a `name` (the
discovery key) and an optional `requires` hint:

```python
from omnigent.sdk import extension

@extension(name="my-extension")
class MyExtension:
    ...
```

That decorator alone already makes instances satisfy the kernel Protocol:

```python
from omnigent.sdk.types import OmnigentExtension
assert isinstance(MyExtension(), OmnigentExtension)   # the §12.7 invariant
```

`@extension` synthesises every optional Protocol member as a behaviour-neutral
no-op (empty dict / empty list / no-op lifecycle), so the `@runtime_checkable`
structural check passes without you implementing 15 methods. Hooks you never
use contribute nothing (`routers() == []`, `policy_modules() == []`, …).

### Step 2 — add a tool

`@tool(name=...)` marks a method as a **tool factory**. The method body builds
and returns the `Tool`; the synthesised `tool_factories()` returns
`{name: factory(config) -> Tool}` — exactly the shape the kernel expects.

```python
from omnigent.sdk import extension, tool

@extension(name="my-extension")
class MyExtension:
    @tool(name="my_custom_tool")
    def my_custom_tool(self):
        from mypackage.tools import MyCustomTool   # defer the domain import
        return MyCustomTool()
```

Verify it has the same shape a hand-written `tool_factories()` would (this is a
real shipped test, `tests/sdk/test_extension_decorator.py`):

```python
ext = MyExtension()
factories = ext.tool_factories()
assert "my_custom_tool" in factories
built = factories["my_custom_tool"]({})        # kernel passes per-tool config
assert isinstance(built, MyCustomTool)
```

> **Deferred imports (NON-NEGOTIABLE).** Import your `Tool` class *inside* the
> factory body, not at module top. The kernel discovery hub is on the runner hot
> path; a top-level FastAPI/domain import there costs ~100 ms per process. Every
> first-party plugin and the SDK itself follow this rule — `omnigent/sdk` stays
> kernel-light because its heavy types (`HarnessDescriptor`, `create_app`) are
> imported lazily inside the methods that need them.

### Step 3 — that's a complete extension

Two decorators (`@extension` + one `@tool`) and one entry-point line (§9) is a
shippable extension. Everything below is *more* capability types, all following
the identical decorate-a-method pattern.

---

## 4. Contributing each capability type

Each capability is "decorate a method." The SDK member decorators live in
`omnigent/sdk/contrib.py`; the table maps decorator → synthesised kernel hook
(`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.3, §12.5, §12.6).

| Decorator | Synthesised kernel hook | Returns |
|---|---|---|
| `@tool(name=…)` | `tool_factories()` | `{name: factory(config) -> Tool}` |
| `@policy(name=…, …)` | `policy_modules()` + a synthesised `POLICY_REGISTRY` | `[dotted_module_name]` |
| `@harness(name=…, …)` | `harness_descriptors()` | `{name: () -> HarnessDescriptor}` |
| `@background` | `background_tasks()` | `[factory() -> Awaitable]` |
| `@router(prefix=…)` | `routers()` | `[APIRouter, …]` |
| `@tool_interceptor(prefix=…)` | `tool_interceptors()` | `{prefix: handler}` |
| `@provides(key)` | a registration on the extension's DI container | (wires injection) |

### Tool — `@tool`

Shown in §3. The method's own annotated params are **method-injected** from your
DI container, so a tool can depend on a `@provides` service:

```python
from omnigent.sdk import extension, tool, provides

@extension(name="my-extension")
class MyExtension:
    @provides()
    def clock(self) -> Clock:            # key inferred from the return annotation
        return SystemClock()

    @tool(name="echo")
    def echo_tool(self, clock: Clock):   # clock injected by the container
        return EchoTool(clock)
```

### Policy — `@policy`

`@extension` synthesises a *real module* (registered in `sys.modules` under
`omnigent._sdk_policies.<ext_name>`) carrying a `POLICY_REGISTRY` list-of-dicts
plus the policy callables, then returns that module's dotted name from
`policy_modules()`. The existing `omnigent.policies.registry.load_registry()`
scan and dotted-path handler resolution work against it **unchanged** — no
hand-written `POLICY_REGISTRY` module needed.

```python
from omnigent.sdk import extension, policy

@extension(name="my-extension")
class MyExtension:
    @policy(
        name="Per-Agent Rate Limiter",
        description="Limit calls per agent per minute.",
        kind="factory",         # "factory": method builds the policy callable
        params_schema={"type": "object",
                       "properties": {"calls_per_minute": {"type": "number"}},
                       "required": ["calls_per_minute"]},
    )
    def per_agent_rate_limit(self, calls_per_minute: float):
        def _policy(event, context):
            ...
        return _policy
```

`kind="callable"` means *the method itself is the policy*; `kind="factory"`
(default) means the method is called with `params` to build the policy callable.
The shipped test `test_policy_registry_loadable_by_real_load_registry` proves the
synthesised module is consumed by the real `load_registry()`.

### Harness — `@harness`

Hides `HarnessDescriptor` construction and the `{name: () -> descriptor}` shape
the `harness` `PluggableRegistry` seam expects. Descriptor fields come from the
decorator args; the method body need not return anything.

```python
from omnigent.sdk import extension, harness

@extension(name="my-extension")
class MyExtension:
    @harness(name="my-harness", module_path="mypackage.my_harness", aliases=("mh",))
    def my_harness(self): ...
```

> Note: `harness_descriptors` is **not** an `OmnigentExtension` Protocol member —
> harnesses flow through the `PluggableRegistry` seam, not `install_extensions`.
> The SDK therefore synthesises `harness_descriptors()` *only* when you use
> `@harness`; otherwise the method stays absent (so the registry's `hasattr`
> probe skips you).

### Background task — `@background`

Usable bare (`@background`) or called (`@background()`). The synthesised
`background_tasks()` returns `[factory() -> Awaitable]`; the server lifespan
starts each and cancels it on shutdown.

```python
import asyncio
from omnigent.sdk import extension, background

@extension(name="my-extension")
class MyExtension:
    @background
    async def my_maintenance_loop(self):
        while True:
            await asyncio.sleep(300)
            ...
```

### Router — `@router`

Returns a `fastapi.APIRouter` (or a list). The synthesised
`routers(auth_provider=…, permission_store=…)` collects every `@router`
method's output into one flat list. Declare `auth_provider` / `permission_store`
params and they are forwarded only if your method accepts them (back-compat with
the kernel's `TypeError`-retry).

```python
from fastapi import APIRouter
from omnigent.sdk import extension, router

@extension(name="my-extension")
class MyExtension:
    @router(prefix="/widgets")
    def widget_routes(self, auth_provider=None, permission_store=None):
        r = APIRouter()
        @r.get("/ping")
        def ping():
            return {"ok": True}
        return r
```

`routers()` is the one **required** Protocol hook; `@extension` always
synthesises it (returning `[]` if you wrote no `@router` methods).

### Tool interceptor — `@tool_interceptor`

Claims an interception point by tool-name prefix — core consults the prefix
table *before* runner dispatch. This is how the SDK closes the
`memory_tool_intercept` seam violation (`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.6)
without a hard core→extension name reference.

```python
from omnigent.sdk import extension, tool_interceptor

@extension(name="my-extension")
class MyExtension:
    @tool_interceptor(prefix="memory__")
    def memory_tool_handler(self, tool_name, arguments, *,
                            caller_agent_id, caller_department):
        from mypackage.memory_intercept import execute_memory_tool
        return execute_memory_tool(tool_name, arguments,
                                   caller_agent_id=caller_agent_id,
                                   caller_department=caller_department)
```

A handler returns a result, or `None` to fall through to normal dispatch.

### Service — `@provides`

Marks a method as a **service provider** registered into the extension's DI
container. Its body is the factory; its own annotated params are injected (so a
service can depend on another service). If you omit the key, the method's
**return-type annotation** is used as the key:

```python
from omnigent.sdk import extension, provides

@extension(name="my-extension")
class MyExtension:
    @provides(ArtifactStore)         # explicit interface key
    def store(self) -> S3ArtifactStore:
        return S3ArtifactStore(...)

    @provides()                      # key inferred from -> annotation
    def clock(self) -> Clock:
        return SystemClock()
```

> **One method, one seam.** A method maps to exactly one capability — stacking
> two markers (e.g. `@tool` *and* `@policy`) on one method raises `TypeError` at
> decoration time. Split it into two methods.

---

## 5. Background tasks vs. lifecycle hooks — when to use which

A common confusion. They run at different times:

- **`pre_init` / `post_init` / `after_init`** are *synchronous setup* hooks fired
  during `install_extensions`, before the server starts serving. Use `pre_init`
  to fail fast (missing secret, un-migrated DB); use `post_init` / `after_init`
  to wire cross-extension state.
- **`@background`** is a *long-running async loop* started in the server lifespan
  (after `after_init`) and cancelled on shutdown. Use it for periodic
  maintenance, polling, heartbeat, metrics publishing.

The prototype's `bytedesk_ext.py` shows both styles side by side: a `@background`
heartbeat plus DI service overrides.

---

## 6. Dependency injection and interface-based replaceability

The SDK ships a small, stdlib-only DI container (`omnigent/sdk/di.py`, ported
from `prototype/omnigent_demo/kernel/di.py`). It is the
*extension-author-facing* container — separate from the internal
`omnigent.server.container` composition-root container. The server container is
always on for internal runtime composition; the SDK container is for extension
authors. You rarely touch the SDK `Container` directly; you use `@provides` +
typed params and let the container inject.

It gives your extension:

- **Three lifetimes** — `Lifetime.SINGLETON` (one per container), `TRANSIENT`
  (fresh each resolve), `SCOPED` (one per scope, the per-request `Depends`
  analog via `create_scope()`).
- **Constructor auto-wiring** — `register_type(cls)` reads `cls.__init__`
  annotations and resolves each recursively.
- **Method injection** — `container.call(fn)` injects a function's annotated
  params. This is exactly how a `@tool` / `@harness` factory receives its
  collaborators.
- **By-interface registration** — register a concrete class under a Protocol/ABC
  key so consumers depend on the *capability*, not the class (Dependency
  Inversion). This is what makes "replace any part" trivial.
- **Cycle detection** — a resolution cycle raises `DIResolutionError` instead of
  recursing forever.

### Depend on a Protocol, swap impls

The pattern (proven in `prototype/omnigent_demo/core/stores_ext.py`): register a
capability under its **interface**, choose the concrete impl at registration
time, and let every consumer depend only on the interface.

```python
import os
from omnigent.sdk import extension, provides

@extension(name="core.stores")
class StoresExtension:
    @provides(ArtifactStore)                  # registered under the INTERFACE
    def artifact_store(self) -> ArtifactStore:
        impl = os.environ.get("OMNIGENT_USE_ARTIFACT_STORE", "memory").strip()
        return FakeS3ArtifactStore() if impl == "s3" else InMemoryArtifactStore()
```

A tool that declares `def record_tool(self, store: ArtifactStore, clock: Clock)`
has no idea whether the store is in-memory or "S3" — replaceability is total.

### The `OMNIGENT_USE_<SEAM>` strangler flag

Every `PluggableRegistry` seam reads an `OMNIGENT_USE_<SEAM>` env var to choose
its default provider (the **Strategy** pattern, `EXTENSION_FRAMEWORK_ANALYSIS.md`
§11). That is the *strangler-fig* migration lever: ship a new impl behind a flag,
flip it per-environment, retire the old one with zero consumer edits. The
prototype demonstrates the full loop:

```bash
python3 run_demo.py                                 # default: InMemoryArtifactStore
OMNIGENT_USE_ARTIFACT_STORE=s3 python3 run_demo.py  # swap impl, zero consumer edits
```

### A third party can replace *any* part

Because there is no privilege gap, a third-party extension replaces a core
service by **re-registering its interface** — last registration wins. The
prototype's `bytedesk_ext.py` overrides `Clock`:

```python
@extension(name="bytedesk", requires=("core.stores", "core.tools"))
class BytedeskExtension:
    @provides(Clock)                  # re-registers Clock → replaces core's impl
    def tenant_clock(self) -> Clock:
        return TenantClock()
```

Every consumer that resolves `Clock` transparently gets `TenantClock`. This is
the deepest expression of the microkernel promise: the application is *all*
plugins, including the ones core ships.

---

## 7. Config-driven enable/disable

The `EnableFeatures` analog (`EXTENSION_FRAMEWORK_ANALYSIS.md` §4.5). Disable an
entire extension *by name* without removing its package or editing entry-points:

```bash
OMNIGENT_DISABLED_EXTENSIONS=bytedesk,omnigent.realtime
```

The comma-separated names are filtered inside `discover_extensions()` in
`omnigent/extensions.py` — a disabled extension is dropped before any stage sees
it (and `get_extension(name)` returns `None` for it). Unset (the default) is a
no-op. The same filter is what `Host.disable(...)` maps onto (§9), so test setups
and production share one mechanism.

---

## 8. Packaging and the entry-point — the one irreducible non-Python line

Discovery is **not** the SDK's job. Your extension self-registers by declaring
itself under the `omnigent.extensions` setuptools entry-point group. This is the
single line you cannot hide behind a decorator without adding a build step
(`EXTENSION_FRAMEWORK_ANALYSIS.md` §4.4, §12.3):

```toml
# pyproject.toml — the ONLY non-Python declaration still required
[project.entry-points."omnigent.extensions"]
my-extension = "mypackage.extension:MyExtension"
```

This is exactly how the real `bytedesk_omnigent` registers (see the root
`pyproject.toml`):

```toml
[project.entry-points."omnigent.extensions"]
bytedesk = "bytedesk_omnigent.extension:BytedeskExtension"
```

At runtime the kernel calls `importlib.metadata.entry_points(group=
"omnigent.extensions")`, loads each factory, and instantiates it. The host never
hard-codes a list of known extensions — your *package metadata* is the
registration.

**Local-dev without reinstalling.** When your checkout is source-mounted and the
`entry_points.txt` hasn't been regenerated, use the `OMNIGENT_EXTENSIONS` env var
(comma-separated `module:factory`), checked *in addition* to entry-points:

```bash
OMNIGENT_EXTENSIONS=mypackage.extension:MyExtension
```

Discovery is error-isolated: one bad entry-point is logged and skipped, never
fatal, and the result is deduped by `name`.

---

## 9. Testing extensions

You do **not** need to boot the full FastAPI stack to test an extension. Two
levels:

### Level 1 — unit-test the synthesised hooks directly

A `@extension`-decorated class is a plain object whose hooks return plain data.
Build it and assert on the shape — the same way the shipped
`tests/sdk/test_extension_decorator.py` does:

```python
from omnigent.sdk.types import OmnigentExtension
from omnigent.sdk import extension, tool, provides

@extension(name="di-ext")
class DiExt:
    @provides(Clock)
    def clock(self) -> SystemClock:
        return SystemClock()

    @tool(name="echo")
    def echo_tool(self, clock: Clock):
        return EchoTool(clock=clock)

def test_conforms_and_injects():
    ext = DiExt()
    assert isinstance(ext, OmnigentExtension)              # §12.7 invariant
    built = ext.tool_factories()["echo"]({})               # config passed positionally
    assert isinstance(built.clock, SystemClock)            # DI wired the dep
```

> Keep DI key types **module-level**, not function-local. Under
> `from __future__ import annotations` (PEP 563) every annotation is stringized;
> `get_type_hints` can only resolve names visible in the module globals. A
> function-local class used as a `@provides` return type or an injected param
> type won't resolve. (The shipped tests put `Clock` / `SystemClock` at module
> level for exactly this reason.)

### Level 2 — compose a host with `Host.build()` + fakes

For an integration test, the `Host` fluent builder (`omnigent/sdk/host.py`,
`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.4) collects your stores/auth/extensions and
produces the *same* `create_app()` call a hand-written composition root would —
without the 15-parameter signature. It feeds explicit extensions and disables
through the kernel's existing discovery seam, not a parallel list.

```python
app = (
    Host.build()
    .with_store(conversation_store=FakeConversationStore())
    .with_auth(auth_provider=FakeAuthProvider())
    .with_extension(MyExtension())          # prepended to discovery
    .disable("omnigent.realtime")           # OMNIGENT_DISABLED_EXTENSIONS analog
    .build_app()                            # -> FastAPI
)
```

To assert wiring *without* paying for the full stack, patch `create_app` and the
kernel discovery symbol (this is the pattern in `tests/sdk/test_host_builder.py`):

```python
def test_explicit_extension_seen_by_discovery(monkeypatch):
    captured = {}
    monkeypatch.setattr("omnigent.server.app.create_app",
                        lambda **kw: captured.update(kw) or "APP")
    import omnigent.kernel.extensions as kext
    monkeypatch.setattr(kext, "discover_extensions", lambda: [])  # baseline
    Host.build().with_extension(MyExtension()).build_app()
    # the explicit extension is restored out of discovery after build_app()
    assert kext.discover_extensions() == []
```

`with_store` / `with_option` validate against a frozen allow-list of
`create_app` parameters, so a typo'd builder kwarg raises `TypeError` instead of
being silently dropped.

> The **prototype** offers a third, fully-runnable level: its `Host.build()…
> .boot()` assembles a teaching host you can introspect (`host.manifest()`,
> `host.resolve(Interface)`, `host.seams["tools"].get(name)`) with zero external
> deps. Run `python3 -m unittest test_prototype` to see the 12 invariants —
> Protocol conformance, staged lifecycle, DI lifetimes, interface swap. Note the
> prototype `Host.boot()` returns its own host object; the *real*
> `Host.build_app()` returns a FastAPI app. Use the prototype to *learn the
> shape*, the real SDK to *ship*.

---

## 10. Migrating from the hand-written Protocol form to the SDK

If you have an existing extension written against the raw `OmnigentExtension`
Protocol, migration is mechanical and back-compatible — the SDK form compiles to
the *same* contract (`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.2 → §12.3).

### Before — the verbose Protocol form

```python
# mypackage/extension.py
from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING
from fastapi import APIRouter

if TYPE_CHECKING:
    from omnigent.tools.base import Tool

class MyExtension:
    name = "my-extension"

    def routers(self, auth_provider=None, permission_store=None) -> list[APIRouter]:
        return []

    def tool_factories(self) -> dict[str, Callable[[object], Tool]]:
        from mypackage.tools import MyCustomTool
        return {"my_custom_tool": lambda _c: MyCustomTool()}

    def policy_modules(self) -> list[str]:
        return ["mypackage.policies.rate_limiter"]

    def background_tasks(self):
        return []
```

…plus a *separate* `mypackage/policies/rate_limiter.py` hand-maintaining a
`POLICY_REGISTRY` list-of-dicts and the policy callable.

### After — the SDK form

```python
# mypackage/extension.py
from omnigent.sdk import extension, tool, policy, background

@extension(name="my-extension")
class MyExtension:
    @tool(name="my_custom_tool")
    def my_custom_tool(self):
        from mypackage.tools import MyCustomTool
        return MyCustomTool()

    @policy(name="Per-Agent Rate Limiter",
            description="Limit calls per agent per minute.",
            kind="factory",
            params_schema={"type": "object",
                           "properties": {"calls_per_minute": {"type": "number"}},
                           "required": ["calls_per_minute"]})
    def per_agent_rate_limit(self, calls_per_minute: float):
        def _policy(event, context): ...
        return _policy

    @background
    async def my_maintenance_loop(self):
        ...
```

What changed, and what didn't:

- `name = "..."` → `@extension(name="...")`. The decorator also sets `requires`.
- `def tool_factories(self): return {...}` → one `@tool` method per tool.
- The **separate `POLICY_REGISTRY` module disappears** — `@policy` synthesises it
  in `sys.modules` and returns its dotted name from `policy_modules()`.
- `routers()` returning `[]` → just **delete it**; `@extension` synthesises an
  empty one. (Add `@router` methods only when you have routes.)
- **The entry-point line in `pyproject.toml` is unchanged.** Discovery is
  identical.

**Migrate incrementally and safely.** The SDK is additive and back-compatible:

- `@extension` only synthesises a hook you didn't write by hand — so you can
  override any single hook while letting the SDK generate the rest. (Shipped
  test `test_author_can_override_synthesised_hook`.)
- The result is `isinstance(obj, OmnigentExtension)` and feeds the kernel's
  existing `discover_extensions` / `install_extensions` /
  `PluggableRegistry.discover_extensions` calls identically. There is no parallel
  discovery, plugin list, or lifecycle (`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.7).
  Mixed fleets of hand-written and SDK extensions coexist.

---

## 11. The stability contract

(`EXTENSION_FRAMEWORK_ANALYSIS.md` §12.8.) Know which surface you depend on:

| Surface | Stability | Rule |
|---|---|---|
| **`omnigent.sdk.*`** | **Stable (semver-anchored).** | The public API. Breaking a decorator signature, a `Host` method, or a public type re-export requires a **major** bump. Your extension depends on this and must not break under kernel refactors. |
| `omnigent.kernel.extensions`, `omnigent.kernel.pluggable`, `omnigent.kernel.lifespan_phases` | **Semi-stable.** | The kernel. May churn between minors as seams are added/generalised; breaking the Protocol *shape* is a breaking change (it propagates up to the SDK), permissible between minors only with a deprecation cycle. |
| First-party plugins (`omnigent.harnesses`, `omnigent.tools.builtins`, …) | **Internal.** | Their seam-registration APIs are not public; refactored freely as long as kernel seam contracts hold. |
| `omnigent.runtime.*`, `omnigent.inner.*`, `omnigent.server.routes.*` | **Internal — no notice.** | May change without warning. Do not import. |

**The practical rule:** *any import from `omnigent.sdk.*` is stable.* Everything
the SDK re-exports (`extension`, `tool`, `policy`, `harness`, `background`,
`router`, `tool_interceptor`, `provides`, `Host`, `Container`, `Lifetime`,
`DIResolutionError`, and the types in `omnigent.sdk.types`) is part of that
semver-stable contract. Kernel internals may move underneath you — the SDK's job
is to keep compiling your extension across those moves.

---

## See also

- [`SDK_REFERENCE.md`](./SDK_REFERENCE.md) — exhaustive symbol reference (every
  decorator argument, every `Host` method, every public type).
- [`EXTENSION_FRAMEWORK_ANALYSIS.md`](./EXTENSION_FRAMEWORK_ANALYSIS.md) §§8–13 —
  kernel inventory, core first-party plugins, three-tier seam map, the SDK
  public surface, the layering invariant, the stability contract.
- [`../prototype/`](../prototype/) — runnable, stdlib-only proof of the whole
  shape. `python3 run_demo.py`; `python3 -m unittest test_prototype`.
- Real shipped code: [`../omnigent/sdk/`](../omnigent/sdk/) (the facade),
  [`../omnigent/kernel/extensions.py`](../omnigent/kernel/extensions.py) (the kernel Protocol +
  `install_extensions` staging), [`../tests/sdk/`](../tests/sdk/) (the
  decorator / DI / host-builder tests this guide's snippets mirror).
</content>
</invoke>
