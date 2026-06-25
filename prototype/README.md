# omnigent microkernel prototype

A **runnable, stdlib-only** proof-of-concept for the architecture in
[`../docs/EXTENSION_FRAMEWORK_ANALYSIS.md`](../docs/EXTENSION_FRAMEWORK_ANALYSIS.md).
It exists so you can *see and run* the patterns, not just read about them.

```bash
cd prototype
python3 run_demo.py                                   # full boot walk-through
OMNIGENT_USE_ARTIFACT_STORE=s3 python3 run_demo.py    # swap an impl, zero consumer edits
python3 -m unittest test_prototype                    # 12 invariants
```

## The three tiers

```
┌─────────────────────────────────────────────────────────────┐
│ EXTENSIONS   extensions/   third-party (bytedesk). Same SDK.  │
├─────────────────────────────────────────────────────────────┤
│ CORE         core/         kernel + curated first-party exts  │
│                            (stores, tools, harnesses) — each  │
│                            an ordinary extension, no privilege │
├─────────────────────────────────────────────────────────────┤
│ KERNEL       kernel/       boot + plugin host + DI. Domain-free│
│              sdk/          the developer facade over it all    │
└─────────────────────────────────────────────────────────────┘
   KERNEL  →  CORE = kernel + first-party exts  →  + third-party exts
```

| Tier | Package | What lives here |
|---|---|---|
| **Kernel** | `kernel/` | `Extension` protocol, `Host` (lifecycle engine), `PluggableRegistry`, `Container` (DI), `discovery`. *Never changes when you add a capability.* |
| **SDK** | `sdk/` | The public API: `@extension`, `@tool`, `@harness`, `@policy`, `@background`, `@router`, `@provides`, `Host.build()`. Hides the kernel. |
| **Core** | `core/` | `stores`, `tools`, `harnesses` — first-party extensions built with the SDK. `core = kernel + these`. |
| **Extensions** | `extensions/` | `bytedesk` — third-party, self-registers via entry-points, uses the identical contract. |

## What each pattern looks like in code

**Self-registration via lifecycle (ServiceStack `IPlugin.Register`).** An extension
contributes from *inside itself*; the host never names it:

```python
@extension(name="core.tools", requires=("core.stores",))
class ToolsExtension:
    @tool(name="record")
    def record_tool(self, store: ArtifactStore, clock: Clock) -> RecordTool:
        return RecordTool(store, clock)        # store + clock injected by DI
```

**Interface-based replaceability.** Capabilities are registered by *interface*; the
impl is chosen by a strangler flag and swaps with no consumer change:

```python
@provides(ArtifactStore)                       # registered under the INTERFACE
def artifact_store(self) -> ArtifactStore:
    impl = os.environ.get("OMNIGENT_USE_ARTIFACT_STORE", "memory")
    return FakeS3ArtifactStore() if impl == "s3" else InMemoryArtifactStore()
```

A third-party extension can **replace any part** by re-registering the interface —
`bytedesk` overrides `Clock` and every consumer transparently gets `TenantClock`.

**Fluent composition root (Builder).**

```python
host = (Host.build()
        .with_extension(StoresExtension())
        .with_extension(ToolsExtension())
        .discover()            # pull in self-registered third-party exts
        .boot())               # fires pre_init → register → post_init → after_init
```

## Dependency injection (`kernel/di.py`)

- **Lifetimes:** `SINGLETON`, `TRANSIENT`, `SCOPED` (per-request via `create_scope()`).
- **Constructor auto-wiring:** `register_type(cls)` reads `__init__` annotations and resolves each recursively.
- **Method injection:** `container.call(fn)` injects a factory's annotated params — that's how `@tool`/`@harness` factories receive their collaborators.
- **By-interface registration:** register under a `Protocol`/ABC so consumers depend on the capability, not the class (Dependency Inversion). This is what makes "replace any part" trivial.
- **Cycle detection** and **child scopes** (singletons shared, scoped/transient isolated).

## Design patterns hidden behind the SDK facade

| Pattern | Where | Hidden from author by |
|---|---|---|
| Microkernel / Plugin | the tiering | `@extension` |
| Registry | `PluggableRegistry` per seam | `@tool` / `@harness` / … |
| Dependency Injection | `Container` | `@provides` + typed params |
| Facade | `sdk/` | the import surface |
| Builder | `HostBuilder` | `Host.build()` |
| Template Method | `Host.boot` stage loop | — (fixed sequence) |
| Strategy | named factories + `OMNIGENT_USE_<SEAM>` | by-interface `@provides` |

## Mapping back to real `omnigent`

| Prototype | Real code it mirrors |
|---|---|
| `kernel/registry.py` | `omnigent/pluggable/registry.py` (`PluggableRegistry`) |
| `kernel/protocol.py` | `omnigent/extensions.py` (`OmnigentExtension` Protocol) |
| `kernel/discovery.py` | `omnigent/extensions.py` (`discover_extensions`, `OMNIGENT_EXTENSIONS`) |
| `kernel/host.py` | `omnigent/server/app.py` lifespan + `service_registry.py` + `lifespan_phases.py` |
| `kernel/di.py` | *new* — the DI container the analysis recommends adding |
| `sdk/` | *new* — the developer facade the analysis recommends adding |
| `extensions/bytedesk_ext.py` | `bytedesk_omnigent` (the real third-party extension) |

This is a teaching scaffold: ~700 lines, no external deps. The migration plan for
folding these patterns into the real tree is §"Migration plan" in the analysis doc.
