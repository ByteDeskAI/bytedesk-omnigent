# Extension Framework Analysis

**Scope:** `bytedesk_omnigent/` (the first-party extension package) and `omnigent/` (the framework/runtime core).
**Date:** 2026-06-25

**Current-state note (2026-07-01):** the logical classification remains useful, but
some mechanics below were historical. Canonical extension host code now lives under
`omnigent.kernel.*`; extension authors should import the stable `omnigent.sdk`
facade. The old `memory_tool_intercept` core→extension coupling is closed through
the generic `tool_interceptors()` extension hook.

---

## 1. Inventory and Classification

The table below covers every top-level package and notable stand-alone module in `bytedesk_omnigent/`. The `omnigent/` packages are covered in a separate section because they constitute the other side of the seam.

### 1.1 `bytedesk_omnigent/` — all EXTENSION

Every module here is an extension. The `bytedesk_omnigent.extension.BytedeskExtension` class is the single integration point that binds them to the omnigent host via the `omnigent.extensions` Protocol and setuptools entry-point group.

| Package / Module | Classification | Justification |
|---|---|---|
| `extension.py` | EXTENSION — coordination point | The `BytedeskExtension` class. Pure registration glue: declares routers, tool factories, policy modules, background tasks, secret backends, config descriptors, and identity providers. Zero business logic of its own; everything it contributes is imported from sibling modules. |
| `accountability/` | EXTENSION — background loop | Domain-specific org accountability sweep (reopen stalled goals, escalate to manager). Depends only on bytedesk domain stores and `omnigent.db`. |
| `agentic_inbox.py` | EXTENSION — inbound integration | Email-event trigger store + HMAC webhook handler for the Agentic Inbox (BDP-2455). ByteDesk-specific; uses `bytedesk_omnigent.db_models`. |
| `approval_strategy.py` | EXTENSION — policy seam | `ApprovalStrategy` Protocol + `DefaultApprovalStrategy` wrapping the core ASK path. Additive typing/strategy shim; carries no own approval logic beyond delegating to `omnigent.runtime.policies.approval`. |
| `assignment.py` | EXTENSION — routing logic | Capability-filter + scoreboard-rank resolver for goal assignment (BDP-2335). Pure Python, no DB of its own; reads from the goal store scoreboard. |
| `auth/obo_credential_provider.py` | EXTENSION — identity port | On-Behalf-Of outbound credential provider; registered via `outbound_credential_providers()` hook on the extension. |
| `auth/principal_resolver.py` | EXTENSION — identity port | Gateway-header HMAC/RSA principal resolver; registered via `principal_resolvers()`. Adapts platform-gateway trust to omnigent `Principal`. |
| `bus/` | EXTENSION — durable store | `SqlAlchemySignalBus` — durable `pending_waits` table for signal/await semantics (BDP-2248). ByteDesk-specific; shares the core engine via `omnigent.runtime.get_conversation_store`. |
| `bus/reaper.py` | EXTENSION — background loop | Periodic bus reaper loop. Runs under `advisory_locked_loop` from `bytedesk_omnigent.maintenance`. |
| `compliance.py` | EXTENSION — domain store | Do-not-contact suppression store (`suppressions` table). Pure ByteDesk org concern. |
| `config/` | EXTENSION — config descriptors | ByteDesk-specific `ConfigDescriptor` list contributed to the Settings Registry (ADR-0150). Reads the core `omnigent.config` API; contributes only bytedesk-scoped keys. |
| `db_models.py` | EXTENSION — DB models | All ByteDesk-owned SQLAlchemy ORM models (`pending_waits`, `cron_triggers`, `tool_steps`, `goals`, `peer_messages`, `deliberations`, `business_outcomes`, `suppressions`, `tasks`, `agent_messages`, `webhook_bindings`, `idempotency_keys`, `scoreboard_entries`, `agentic_inbox_events`, etc.). Shares `omnigent.db.db_models.Base`. |
| `deliberation.py` | EXTENSION — domain store | Durable deliberation store (C6). ByteDesk org-decision substrate. |
| `executor_protocols.py` | EXTENSION — typing shim | Runtime-checkable `Protocol` types for `ToolExecutorProtocol`, `ElicitationHandlerProtocol`, `PolicyEvaluatorProtocol`. Additive; names contracts that the core `ExecutorAdapter` already relies on implicitly. |
| `github_app_mcp.py` | EXTENSION — MCP server | GitHub App-backed MCP Streamable HTTP server for engineering agents. Standalone; not mounted through `BytedeskExtension`. |
| `goals.py` | EXTENSION — domain store | Durable goals backlog + scoreboard (`SqlAlchemyGoalStore`). ByteDesk C3 substrate. |
| `governance.py` | EXTENSION — read model | Pure-function governance summary over injected goal + deliberation stores. No state. |
| `harnesses/hermes_native_harness.py` | EXTENSION — harness | `create_app()` factory for the `hermes` harness. Module path is **hard-wired into `omnigent/runtime/harnesses/descriptors.py`** as part of the default descriptor set — this is the one current coupling violation documented in the descriptor source (`# hard fork`). |
| `harnesses/hermes_native_executor.py` | EXTENSION — executor | ACP stdio bridge to the local Hermes Agent CLI. |
| `harnesses/config_apply.py` | EXTENSION — harness config | Applies omnigent agent spec fields to the Hermes `~/.config/hermes/config.toml`. |
| `idempotency.py` | EXTENSION — durable store | Generic at-most-once claim plane (`idempotency_keys` table, BDP-2251). |
| `ingress.py` | EXTENSION — inbound integration | Signed webhook/event ingress: HMAC-verify → match binding → deliver to signal bus. Pure + injectable. |
| `integration_capabilities.py` | EXTENSION — static catalog | Read-only integration blueprint catalog (no DB, no side effects). |
| `integration_gap_analysis.py` | EXTENSION — static analysis | Read-only gap analysis output (documentation artifact). |
| `integration_verification_matrix.py` | EXTENSION — static analysis | Read-only verification matrix (documentation artifact). |
| `lifecycle.py` | EXTENSION — domain types | Closed-set `StrEnum` vocabularies + `LifecycleStateMachine` for all ByteDesk domain stores. |
| `maintenance.py` | EXTENSION — template method | `advisory_locked_loop` — shared scaffold for the three background maintenance loops. Delegates the PG advisory lock to `omnigent.runtime.memory_maintenance.advisory_lock`. |
| `memory_access.py` | EXTENSION — access control | Three-tier memory compartment resolver (org/dept/agent scope). Pure function; no DB. |
| `memory_mcp.py` | EXTENSION — MCP server | Stdio MCP advertisement front for `memory__*` tools (schema-only; bodies are intercepted server-side). |
| `memory_tool_intercept.py` | EXTENSION — server-side hook | Server-side execution of `memory__*` tool calls. Registered through `BytedeskExtension.tool_interceptors()` under the `memory__` prefix; core no longer imports this module directly. |
| `outcomes.py` | EXTENSION — domain store | Business Outcome Ledger (`business_outcomes` + `scoreboard_entries`). |
| `peer.py` | EXTENSION — domain store | Durable peer-message store (`peer_messages` — lateral agent-to-agent messaging, C2). |
| `policies/` | EXTENSION — policy builtins | Eight named policy modules (`budget`, `delegation`, `dry_run`, `forever_gate`, `outreach_compliance`, `spawn_governor`, `two_key`, `verify_gate`). Each declares a module-level `POLICY_REGISTRY: list[PolicyRegistryRaw]`. Contributed via `BytedeskExtension.policy_modules()`. |
| `provider_metadata.py` | EXTENSION — introspection | `ProviderMetadata` + `ProviderMetadataMixin` declarative capability metadata. Additive read-side; no behavior change. |
| `realtime/` | EXTENSION — realtime bridge | `office:agents` roster fan-out bridge (BDP-2301). Subscribes to store-neutral AgentStore events at lifespan startup; depends on `bytedesk_omnigent.realtime.publisher`. |
| `release/` | EXTENSION — orchestration | Ops-release orchestrator: park signal → bind webhook → trigger TeamCity pipeline. |
| `routes/` | EXTENSION — FastAPI routes | Ten route factories (`agentic_inbox`, `config`, `goals`, `governance`, `ingress`, `integration_capabilities`, `omni_cli_terminal`, `tasks/router`, `scheduler/router`, `_health`). Contributed via `BytedeskExtension.routers()`. |
| `runtime.py` | EXTENSION — store accessors | Lazy per-URI accessors for `SqlAlchemySignalBus`, `SqlAlchemyCronScheduler`, `SqlAlchemyToolStepStore`, `SqlAlchemySessionStateStore`. They delegate URI discovery to `omnigent.runtime.get_conversation_store` (correct direction: extension → core). |
| `scheduler/` | EXTENSION — durable store + loop | `SqlAlchemyCronScheduler` (`cron_triggers` table) + `cron_scheduler_loop`. |
| `secrets/infisical.py` | EXTENSION — secret backend | Infisical-backed `SecretBackend` with two-tier cache. Contributed via `BytedeskExtension.secret_backends()`. |
| `session_state_store.py` | EXTENSION — store facade | Read/write facade over `conversations.session_state` / `session_usage` columns. No new table. |
| `sessions/` | EXTENSION — session initiation | `SessionInitiator` Protocol + `build_self_call_initiator_from_env` + `build_cron_dispatch`. The `_cron_scheduler` background task uses this to re-enter the omnigent server via HTTP. |
| `skills_mcp.py` | EXTENSION — MCP server | Stdio MCP proxy for the skill-acquisition routes (BDP-2462). |
| `tasks/` | EXTENSION — durable store + routes | `SqlAlchemyTaskStore` (`tasks` table, BDP-2333) + task router + `seed_workflow_tasks`. |
| `tool_steps/` | EXTENSION — durable store | `SqlAlchemyToolStepStore` (`tool_steps` table, BDP-2252). Durable at-most-once tool execution tracking. |
| `tools/` | EXTENSION — builtin tools | `BytedeskConfluenceTool`, `BytedeskGitHubTool`, `BytedeskJiraTool`, `BytedeskSlackTool`, `DeliberationStartTool` / `FindTool` / `PositionTool` / `DecideTool`, `GoalCreate/List/Claim/Advance/DependencyUpdateTool`, `OutcomeRecordTool`, `PeerSendTool` / `PeerInboxTool`, `SignalAwaitTool` / `DeliverTool` / `CheckTool`, `FindSpecialistTool`, `ResolveAssigneeTool`, `RoutingToolsTool`. Contributed via `BytedeskExtension.tool_factories()`. |

### 1.2 `omnigent/` — CORE (the framework and runtime)

These are the packages that form the extension host. An extension must not depend on them in the wrong direction (extension imports core is acceptable; core must not import extension except through the Protocol seam).

| Package | Classification | Justification |
|---|---|---|
| `kernel/extensions.py` | CORE — extension host / discovery | Defines `OmnigentExtension` Protocol. Owns `discover_extensions()` (entry-point + env-var), `install_extensions()` (router mounting), and the `extension_*()` aggregator functions that the rest of core consumes. This IS the plugin framework; `omnigent.sdk` is the stable author-facing facade. |
| `pluggable/` | CORE — pluggable registry | `PluggableRegistry[T]` — the generic 4-invariant seam (Protocol per seam, named-factory registry, entry-point discovery via hook, `OMNIGENT_USE_<SEAM>` strangler flag). `manifest.py` declares `SEAMS` and `discover_all_extensions()`. |
| `server/app.py` | CORE — application factory | `create_app()` + `_lifespan`. Calls `install_extensions()`, `extension_principal_resolvers()`, `extension_background_factories()`. The app composition root. |
| `kernel/lifespan_phases.py` | CORE — lifespan DAG | Dependency-sorted startup/shutdown phases. This is the authoritative `create_app` lifespan path. |
| `runtime/` | CORE — execution engine | Harness process manager, executor adapter, policy engine/enforcement, memory maintenance, tool output, tool retry, compaction, agent cache, session stream, subagent block notifier. |
| `runtime/harnesses/` | CORE — harness registry | `HarnessDescriptor` + `HARNESS_REGISTRY` (`PluggableRegistry[HarnessDescriptor]`). The `hermes` descriptor is hard-wired in the default set (documented as "hard fork"). Extensions contribute additional harnesses via the `harness_descriptors` hook. |
| `tools/builtins/__init__.py` | CORE — builtin tool registry | `_BUILTIN_REGISTRY` dict. Merges `**extension_tool_factories()` at import time. |
| `tools/manager.py` | CORE — tool lifecycle | `ToolManager` — registers builtin, client-specified, local-Python tools per-conversation. |
| `policies/registry.py` | CORE — policy registry | `load_registry()` merges `BUILTIN_POLICY_MODULES + extension_policy_modules()`. Scans `POLICY_REGISTRY` lists. |
| `policies/builtins/` | CORE — built-in policies | Core policies (safety, cost, content). Distinct from bytedesk extension policies. |
| `identity/` | CORE — identity machinery | `PluggableRegistry` seams for `assertion_verifier`, `outbound_credential`, and `authorizer`. Extension contributes providers via `assertion_verifiers()`, `outbound_credential_providers()`, `authorization_providers()` hooks. |
| `stores/` | CORE — storage abstractions | `AgentStore`, `ConversationStore`, `ArtifactStore`, `FileStore`, `CommentStore`, `PermissionStore`, `PolicyStore`, `MemoryStore`. The bytedesk extension borrows the conversation store's DB engine but owns its own tables. |
| `db/` | CORE — DB utilities | `get_or_create_engine`, `make_managed_session_maker`, `now_epoch`, `Base` ORM declarative base, Alembic migrations. |
| `entities/` | CORE — domain entities | `Agent`, `Conversation`, `Permission`, `Policy`, `SessionResources`, etc. |
| `spec/` | CORE — agent spec | `AgentSpec`, `MCPServerConfig`, `SkillSpec`, `ToolRuntime`. Parsed from YAML bundles. |
| `config/` | CORE — config system | `ConfigDescriptor`, `ConfigRegistry`, `EnvConfigStore`, `runtime_store`. Extension contributes descriptors via `config_descriptors()`. |
| `onboarding/secrets.py` | CORE — secret resolution | Calls `extension_secret_backends()` to populate the backend chain. |
| `coordination/` | CORE — backplane | Pluggable coordination (in-process, NATS). |
| `runner/` | CORE — runner process | Tool dispatch, MCP lifecycle, session stream. Tool interception now flows through the extension Protocol, not a direct ByteDesk import. |
| `inner/` | CORE — LLM harness adapters | Executor ABC + per-harness implementations (claude-sdk, codex, pi, grok-native, etc.). |
| `llms/` | CORE — LLM client | Model catalog, LLM retry, context-window pricing. |
| `environments/`, `sandbox/` | CORE — sandbox | Sandbox launcher abstractions. |

---

## 2. Current Registration Mechanics — the "Before" Picture

### 2.1 Router mounting

`omnigent/server/app.py` line 1879–1885:

```python
from omnigent.extensions import install_extensions

install_extensions(
    app,
    auth_provider=auth_provider,
    permission_store=permission_store,
)
```

`install_extensions` iterates `discover_extensions()`, calls each extension's `routers(auth_provider=..., permission_store=...)`, and mounts every returned `APIRouter` under `/v1`. A `TypeError` on the two-arg call is caught and retried with fewer args for back-compat.

### 2.2 Tool factories (builtin tool registry)

`omnigent/tools/builtins/__init__.py` lines 223–264 — the `_BUILTIN_REGISTRY` dict:

```python
_BUILTIN_REGISTRY: dict[str, _BuiltinFactory | None] = {
    # ... core tools ...
    **extension_tool_factories(),       # <── extension seam, called at module import
    "web_fetch": None,
    # ...
}
```

`extension_tool_factories()` calls `discover_extensions()` and merges each extension's `tool_factories()` return value. This call runs at **module import time** — the registry is built once when `omnigent.tools.builtins` is first imported. A late-registered extension is not visible.

### 2.3 Policy modules

`omnigent/policies/registry.py` lines 88–100:

```python
from omnigent.extensions import extension_policy_modules
from omnigent.policies.builtins import BUILTIN_POLICY_MODULES

all_modules = (
    list(BUILTIN_POLICY_MODULES)
    + extension_policy_modules()
    + list(extra_modules or [])
)
for module_path in all_modules:
    mod = importlib.import_module(module_path)
    raw_entries = getattr(mod, "POLICY_REGISTRY", None)
    ...
```

`load_registry()` is called once at server startup. Each extension policy module must expose a module-level `POLICY_REGISTRY: list[PolicyRegistryRaw]`. The registry then stores `PolicyRegistryEntry` objects keyed by handler dotted path.

### 2.4 Secret backends

`omnigent/onboarding/secrets.py` calls `extension_secret_backends()` to prepend extension-provided backends ahead of the default local-keyring backend. Consulted at secret-resolution time, not at import time.

### 2.5 Background tasks

`omnigent/server/app.py` line 1104–1108:

```python
from omnigent.extensions import extension_background_factories

ext_tasks = [
    asyncio.ensure_future(factory())
    for factory in extension_background_factories()
]
```

Called inside `_lifespan`, started as asyncio tasks, cancelled on shutdown.

### 2.6 Harness registration

`omnigent/runtime/harnesses/descriptors.py` — `_DEFAULT_DESCRIPTORS` tuple. The `hermes` harness is hard-wired here:

```python
HarnessDescriptor(
    name="hermes",
    module_path="bytedesk_omnigent.harnesses.hermes_native_harness",
),
```

The `HARNESS_REGISTRY` is a `PluggableRegistry[HarnessDescriptor]` that exposes a `harness_descriptors` hook for extension-contributed harnesses, but the `hermes` descriptor is in the default set rather than contributed through that hook. The source comment marks this as intentional ("hard fork").

### 2.7 Pluggable seam registries

`omnigent/pluggable/manifest.py` defines `SEAMS` — a tuple of `(seam_name, registry_accessor, extension_hook)` triples. `discover_all_extensions()` iterates `SEAMS` and calls `registry.discover_extensions(hook=hook)` on each, which in turn iterates `discover_extensions()` and calls `getattr(ext, hook, None)()` on each extension that has the hook method. Seams currently declared:

- `harness` → hook `harness_descriptors`
- `artifact_store` → hook `artifact_store_providers`
- `web_search` → hook `web_search_providers`
- `memory_embedder` → hook `memory_embedder_providers`
- `agent_memory` → hook `agent_memory_providers`
- `spec_source` → hook `spec_source_providers`
- `coordination_backplane` → hook `coordination_backplane_providers`
- `assertion_verifier` → hook `assertion_verifiers`
- `outbound_credential` → hook `outbound_credential_providers`
- `authorizer` → hook `authorization_providers`

### 2.8 Tool interception — seam violation closed

The former direct import of `bytedesk_omnigent.memory_tool_intercept` from the
session route was removed. Core now calls the generic
`extension_tool_interceptors()` aggregator and dispatches by prefix. The ByteDesk
extension contributes:

- `memory__` → `bytedesk_omnigent.memory_tool_intercept`
- `org__` → `bytedesk_omnigent.org_tool_intercept`
- connector prefixes such as `atlassian__` and `google__`

The intercept still runs at the server tool-dispatch choke point where caller
identity is already bound, but the implementation is now extension-owned through
the Protocol seam.

---

## 3. Repeated Patterns and Duplication

### 3.1 Per-background-loop scaffold (three clones)

`bytedesk_omnigent/bus/reaper.py`, `scheduler/loop.py`, and `accountability/loop.py` all follow an identical pattern before `advisory_locked_loop` was extracted (BDP-2355):

```
while True:
    await asyncio.sleep(interval)
    store = get_<store>()
    with advisory_lock(store.engine, LOCK_KEY) as acquired:
        if acquired:
            count = await asyncio.to_thread(<blocking_tick>)
            logger.info("...", count)
    # CancelledError propagates for clean shutdown
```

`bytedesk_omnigent/maintenance.py` already provides `advisory_locked_loop` as a Template Method for this scaffold. All three loops have been or should be migrated to it. The current `bus/reaper.py` and `scheduler/loop.py` still contain the scaffold verbatim; only `accountability/loop.py` uses `advisory_locked_loop`.

### 3.2 Per-store shape (six stores, one mold)

Every ByteDesk durable store (`SqlAlchemySignalBus`, `SqlAlchemyCronScheduler`, `SqlAlchemyToolStepStore`, `SqlAlchemySessionStateStore`, `SqlAlchemyGoalStore`, `SqlAlchemyPeerMessageStore`, `SqlAlchemyTaskStore`, `SqlAlchemyDeliberationStore`, `SqlAlchemyOutcomeLedger`) shares:

1. Constructor: `def __init__(self, storage_uri: str)`
2. `self._engine = get_or_create_engine(storage_uri)`
3. `self._Session = make_managed_session_maker(self._engine)`
4. A module-level `_<store>_cache: dict[str, Store]` dict
5. A module-level `get_<store>() -> Store` accessor in `bytedesk_omnigent/runtime.py`

The cache accessor pattern repeats verbatim nine times across `runtime.py` and the store modules.

### 3.3 Per-tool external-API pattern (four tools)

`BytedeskJiraTool`, `BytedeskConfluenceTool`, `BytedeskGitHubTool`, and `BytedeskSlackTool` all follow:

1. A private `_<Service>Client` class with `httpx.Client` or `httpx.AsyncClient`
2. `load_secret(SECRET_NAME)` for credentials
3. A `dispatch(op, args)` method switching on `op`
4. A never-crash contract returning `{"ok": false, "error": "..."}` on any failure
5. Registration as `lambda _c: <Tool>()` in `BytedeskExtension.tool_factories()`

The `HttpToolClient` base class in `tools/_http_adapter.py` already captures part of this pattern, but the four tools still duplicate the `{"ok": false, ...}` error-return contract in their dispatch methods individually.

### 3.4 Per-policy module POLICY_REGISTRY declaration (eight modules)

Every policy module (`verify_gate.py`, `budget.py`, `dry_run.py`, `two_key.py`, `delegation.py`, `outreach_compliance.py`, `forever_gate.py`, `spawn_governor.py`) exposes a `POLICY_REGISTRY: list[PolicyRegistryRaw]` at module level with identical structure. The registry scanner `omnigent/policies/registry.py` imports each module and reads this attribute by name. The `PolicyRegistryRaw` TypedDict in `bytedesk_omnigent/policies/__init__.py` pins the shape, but there is no base class enforcing it; a misspelling of `POLICY_REGISTRY` silently yields an empty contribution.

### 3.5 Extension-surface aggregation pattern (eight `extension_*` functions)

`omnigent/extensions.py` contains eight nearly-identical aggregation functions:

```python
def extension_tool_factories() -> dict:
    factories: dict = {}
    for ext in discover_extensions():
        if hasattr(ext, "tool_factories"):
            factories.update(ext.tool_factories())
    return factories

def extension_policy_modules() -> list[str]:
    modules: list[str] = []
    for ext in discover_extensions():
        if hasattr(ext, "policy_modules"):
            modules.extend(ext.policy_modules())
    return modules
# ... six more identical in structure ...
```

The `PluggableRegistry.discover_extensions(hook=...)` method already generalizes this pattern for the seam registries. The `OmnigentExtension`-surface aggregators could be collapsed to a single generic collector.

---

## 4. Proposed Core Layer and Plugin Framework

The framework is substantially already here. The design below names what exists accurately, proposes the one structural gap to close, and gives the recommended migration direction.

### 4.1 The Protocol — `OmnigentExtension` (already exists)

`omnigent/extensions.py` defines:

```python
@runtime_checkable
class OmnigentExtension(Protocol):
    name: str

    def routers(self, auth_provider=..., permission_store=...) -> list[APIRouter]: ...

    # Optional capability methods (probed with hasattr):
    def tool_factories(self) -> dict[str, Callable[[object], object]]: ...
    def policy_modules(self) -> list[str]: ...
    def secret_backends(self) -> list[object]: ...
    def default_mcp_servers(self) -> list[object]: ...
    def background_tasks(self) -> list[Callable[[], Awaitable[None]]]: ...
    def config_descriptors(self) -> list[object]: ...
    def principal_resolvers(self) -> list[object]: ...
    def assertion_verifiers(self) -> dict[str, Callable[[], object]]: ...
    def outbound_credential_providers(self) -> dict[str, Callable[[], object]]: ...
    def authorization_providers(self) -> dict[str, Callable[[], object]]: ...
```

**ServiceStack mapping:** This is `IPlugin.Register(IAppHost)` in spirit. The difference is that `OmnigentExtension` uses *method dispatch per surface* rather than a single `register(host)` call; the host calls each surface aggregator independently. Both models are valid; the multi-method form makes each contribution independently optional (`hasattr` probing vs. an empty default).

**ServiceStack staged lifecycle (`IPreInitPlugin` / `IPostInitPlugin` / `IAfterInitAppHost`):** There is no staged pre-init hook today. Extensions' `background_tasks()` is the post-init hook (tasks start after all routes mount). A `pre_init()` → `register()` → `post_init()` staging would allow an extension to, e.g., create its DB tables before any route is mounted. This is the one gap worth filling (see Section 4.3).

### 4.2 The Host — `discover_extensions` + `install_extensions` (already exists)

`omnigent/extensions.py` + `omnigent/server/app.py` together form the AppHost:

- `discover_extensions()` — walks `importlib.metadata.entry_points(group="omnigent.extensions")` then the `OMNIGENT_EXTENSIONS` env var. Deduplicates by `name`. One bad extension is isolated.
- `install_extensions(app, ...)` — mounts routers; the composition root.
- Eight `extension_*()` aggregators — surface-specific collectors consumed by the tool registry, policy registry, secrets system, lifespan, and spec loader.
- `PluggableRegistry.discover_extensions(hook=...)` — covers the nine seam-level registries.

**ServiceStack mapping:** `appHost.Plugins` (the list) → `discover_extensions()` return value. `appHost.GetPlugin<T>()` → not currently provided; see gap below.

**Gap — `get_extension(name)` / `assert_extension(name)`:** There is no way for one extension to look up another by name at runtime. A `get_extension(name)` helper on top of `discover_extensions()` would close this gap:

```python
def get_extension(name: str) -> OmnigentExtension | None:
    for ext in discover_extensions():
        if ext.name == name:
            return ext
    return None

def assert_extension(name: str) -> OmnigentExtension:
    ext = get_extension(name)
    if ext is None:
        raise MissingExtensionError(name)
    return ext
```

### 4.3 Staged Lifecycle (the one structural addition recommended)

Add two optional Protocol methods to `OmnigentExtension`:

```python
# omnigent/extensions.py addition
class OmnigentExtension(Protocol):
    # ... existing methods ...

    def pre_init(self) -> None:
        """Called BEFORE any router is mounted.

        Use to create DB tables, validate required env vars, or fail fast.
        An exception here aborts this extension and logs — it must not kill the server.
        """
        ...

    def post_init(self) -> None:
        """Called AFTER all extensions' routers are mounted, before lifespan starts.

        Use to register cross-extension state or wire inter-extension dependencies.
        """
        ...
```

`install_extensions` becomes:

```python
def install_extensions(app, *, extensions=None, auth_provider=None, permission_store=None):
    exts = discover_extensions() if extensions is None else extensions

    # Stage 1 — pre_init
    for ext in exts:
        if hasattr(ext, "pre_init"):
            try:
                ext.pre_init()
            except Exception:
                logger.exception("extension %r pre_init failed", ext.name)
                continue   # or remove from active list

    # Stage 2 — register (routers)
    for ext in exts:
        ...mount routers...
        installed.append(ext.name)

    # Stage 3 — post_init
    for ext in exts:
        if hasattr(ext, "post_init"):
            try:
                ext.post_init()
            except Exception:
                logger.exception("extension %r post_init failed", ext.name)

    return installed
```

**ServiceStack mapping:** `IPreInitPlugin.BeforePluginsLoaded` → `pre_init`, plugin `Register` → routers mounting, `IPostInitPlugin.AfterPluginsLoaded` → `post_init`, `IAfterInitAppHost` → the background-tasks lifespan startup.

### 4.4 Self-Registration Mechanism — Recommendation

The existing system uses **mechanism (b): entry-points + env-var fallback**. This is the correct choice. The analysis below weighs all four options.

**(a) Explicit `host.plugins.add(MyPlugin())`**
Requires the application entry point to know every extension by name. Defeats the goal of self-registration from within the extension. Good for test injection only.

**(b) Entry-points + `OMNIGENT_EXTENSIONS` env var (current approach — recommended)**
```toml
# pyproject.toml of bytedesk_omnigent (already wired)
[project.entry-points."omnigent.extensions"]
bytedesk = "bytedesk_omnigent.extension:BytedeskExtension"
```
The extension package declares itself. `importlib.metadata.entry_points(group="omnigent.extensions")` discovers it at runtime. The env var covers source-mounted local-dev where the egg-info has not been regenerated. Error-isolated: one bad entry-point is logged and skipped, never fatal. This mirrors ServiceStack's `appHost.Plugins.Add(new SomePlugin())` — explicit and observable — while making the addition live in the extension's own package metadata rather than in the host's startup code.

**(c) Decorator + import-time registry**
```python
@register_extension
class BytedeskExtension:
    ...
```
Requires the extension module to be imported before the registry is consulted. Import order becomes load order; circular import hazard is real. No clear mechanism to discover modules without either entry-points or env var.

**(d) Namespace-package scanning**
Scan all installed packages for a naming convention (`bytedesk_omnigent_*`). Fragile, slow, and harder to isolate. ServiceStack's assembly scanning is the C# analog; it works because assemblies are explicit compilation units. Python packages are too fluid.

**Recommendation: keep (b), add `pre_init`/`post_init` staging, add `get_extension`/`assert_extension` lookup, collapse the eight `extension_*` aggregator functions into a single generic collector.**

### 4.5 Config-Driven Enable/Disable (the `EnableFeatures` analog)

The current system has no extension-level feature flag. Extensions are either registered (entry-point present) or not. The `OMNIGENT_EXTENSIONS` env var is an inclusion list for source-mounted extras.

The analogous `EnableFeatures` mechanism:

```python
# omnigent/extensions.py
def discover_extensions(
    *,
    disabled: set[str] | None = None,
) -> list[OmnigentExtension]:
    _disabled = disabled or _disabled_from_env()
    found = [ext for ext in _raw_discover() if ext.name not in _disabled]
    return found

def _disabled_from_env() -> set[str]:
    raw = os.environ.get("OMNIGENT_DISABLED_EXTENSIONS", "")
    return {name.strip() for name in raw.split(",") if name.strip()}
```

An `OMNIGENT_DISABLED_EXTENSIONS=bytedesk` env var disables the entire ByteDesk extension without removing the package or editing entry-points. Each extension could also expose a capability bitmask on `OmnigentExtension.feature_flags` for finer-grained control, but that is an optimization for when multiple logical features ship inside one extension package.

### 4.6 Worked Before/After: `BytedeskJiraTool` as a Plugin

**Before (current):**

Three places must be edited to add a new external-API tool:

1. `bytedesk_omnigent/tools/jira_tools.py` — implement `BytedeskJiraTool`
2. `bytedesk_omnigent/extension.py` → `tool_factories()` — add `"bytedesk_jira": lambda _c: BytedeskJiraTool()`
3. (Implicit) the tool is available to agents that declare `bytedesk_jira` in their spec's `tools.builtins`

No single file declares "this tool belongs to this extension". The connection between tool and extension lives only in `extension.py`'s `tool_factories()` method.

**After (proposed — self-registering within the extension's `register` phase):**

The `BytedeskExtension` class already IS the self-registration point. What the "after" improves is making each tool's contribution explicit within its own module, so `extension.py` does not need to import every tool class at the top level:

```python
# bytedesk_omnigent/tools/jira_tools.py
# ... class BytedeskJiraTool(Tool): ...

# At module bottom — self-declaration within the extension's surface
TOOL_CONTRIBUTIONS: dict[str, object] = {
    "bytedesk_jira": BytedeskJiraTool,
}
```

```python
# bytedesk_omnigent/extension.py
def tool_factories(self) -> dict[str, Callable[[object], Tool]]:
    import importlib
    factories: dict = {}
    for mod_path in [
        "bytedesk_omnigent.tools.jira_tools",
        "bytedesk_omnigent.tools.slack_tools",
        # ... one line per tool module
    ]:
        mod = importlib.import_module(mod_path)
        for name, cls in getattr(mod, "TOOL_CONTRIBUTIONS", {}).items():
            factories[name] = lambda _c, c=cls: c()
    return factories
```

This is a modest improvement: each tool module declares its own registry contribution, and `extension.py` becomes a module scanner rather than a manual import list. The gain is that adding a new tool requires only creating the module + the `TOOL_CONTRIBUTIONS` entry in it; no edit to `extension.py` needed, mirroring the `POLICY_REGISTRY` pattern the policy modules already use.

For a fuller ServiceStack analog — where a tool truly self-registers into the host without touching any list — entry-points at the tool sub-package level would be needed:

```toml
[project.entry-points."bytedesk_omnigent.tools"]
bytedesk_jira = "bytedesk_omnigent.tools.jira_tools:BytedeskJiraTool"
```

This adds complexity without benefit for a single-extension monorepo. It is the right model when third parties extend the extension itself.

---

## 5. Migration Plan

The existing framework is already mature. The migration plan below is additive and low-risk; each step is independently deployable.

**Step 1 — Close the `memory_tool_intercept` seam violation (high priority)**

`omnigent/server/routes/sessions.py` line 11832 imports `bytedesk_omnigent.memory_tool_intercept` directly. This is the only hard core→extension coupling. Move it behind the extension Protocol:

Option A: Add a `memory_tool_handler()` method to `OmnigentExtension` returning a callable `(tool_name, arguments, caller_agent_id, caller_department) -> str | None`. Core calls `get_extension("bytedesk").memory_tool_handler()` (or iterates extensions checking `hasattr`). Returns `None` when no extension handles the tool, and core falls through to the runner dispatch.

Option B: Add a new `tool_interceptors()` hook returning `{prefix: handler}` (e.g. `{"memory__": execute_memory_tool}`). Core checks the prefix table before runner dispatch. This is more generic and does not require naming the bytedesk extension.

Option B is preferred — it removes the hard name reference and opens the interception point to any extension.

**Step 2 — Add `pre_init` / `post_init` staging to `install_extensions`**

Purely additive. `BytedeskExtension` can implement `pre_init` to run `ensure_tables()` (or call `advisory_lock` for the initial schema check) before any route is mounted. Existing extensions without these methods are unaffected.

**Step 3 — Add `get_extension` / `assert_extension` lookup helpers**

Two lines in `omnigent/extensions.py`. No impact on existing call sites.

**Step 4 — Add `OMNIGENT_DISABLED_EXTENSIONS` support to `discover_extensions`**

Modify `discover_extensions()` to filter by `_disabled_from_env()`. The empty env var case (the default) is a no-op.

**Step 5 — Migrate `bus/reaper.py` and `scheduler/loop.py` to `advisory_locked_loop`**

These two loops still contain the scaffold verbatim. Replace with `advisory_locked_loop` (already written in `bytedesk_omnigent/maintenance.py`). Reduces ~60 lines of duplicate code. Zero behavior change.

**Step 6 — Collapse the eight `extension_*` aggregator functions into a generic collector**

```python
# omnigent/extensions.py
def _aggregate_extension_surface(
    hook: str, *, merge: Literal["update", "extend"] = "extend"
) -> list | dict:
    result: list | dict = {} if merge == "update" else []
    for ext in discover_extensions():
        contributed = getattr(ext, hook, None)
        if contributed is None:
            continue
        try:
            value = contributed()
            if merge == "update":
                result.update(value)   # type: ignore
            else:
                result.extend(value)   # type: ignore
        except Exception:
            logger.exception("extension %r failed to contribute %s", ext.name, hook)
    return result

# Keep the named helpers as thin wrappers for call-site clarity:
def extension_tool_factories() -> dict:
    return _aggregate_extension_surface("tool_factories", merge="update")

def extension_policy_modules() -> list[str]:
    return _aggregate_extension_surface("policy_modules")
```

This is a refactor with no behavior change; the eight named helpers are preserved for existing call sites.

**Step 7 — Introduce `TOOL_CONTRIBUTIONS` module-level declarations in tool modules (optional)**

Convert each `bytedesk_omnigent/tools/*.py` to declare `TOOL_CONTRIBUTIONS: dict[str, type[Tool]]`. Update `BytedeskExtension.tool_factories()` to scan them. No impact on the framework; purely an internal bytedesk extension concern.

**Step 8 — Extract `hermes` harness descriptor into a `harness_descriptors` hook**

Remove the `hermes` entry from `_DEFAULT_DESCRIPTORS` in `omnigent/runtime/harnesses/descriptors.py` and contribute it through `BytedeskExtension.harness_descriptors()`. This closes the remaining hard fork in core. Requires `discover_all_extensions()` to be called before `HARNESS_REGISTRY` is first consulted, which is already the pattern (deferred to server startup via `omnigent/kernel/lifespan_phases.py`). Risk: any test that reads `_HARNESS_MODULES` without a lifespan setup will no longer see `hermes`; test fixtures may need updating.

---

## 6. Risks and Open Questions

**Circular-import hazards**

`omnigent.tools.builtins.__init__` calls `extension_tool_factories()` at module import time. `extension_tool_factories()` calls `discover_extensions()`, which calls `importlib.metadata.entry_points()` and then imports `bytedesk_omnigent.extension:BytedeskExtension`. `BytedeskExtension.tool_factories()` imports all tool modules. If any tool module transitively imports `omnigent.tools.builtins`, there is a cycle. The existing code avoids this with deferred imports inside each factory lambda — this constraint must be preserved if `TOOL_CONTRIBUTIONS` scanning is added. The `if TYPE_CHECKING:` guard in `bytedesk_omnigent/extension.py` already demonstrates awareness of this issue.

**Load-order determinism**

`discover_extensions()` is called multiple times: once at tool-builtin import time (`extension_tool_factories()`), once during lifespan (`extension_background_factories()`), once during auth setup (`extension_principal_resolvers()`), and once inside `install_extensions()`. Each call re-runs `entry_points()` and `_load_env_extensions()`. Extensions that have side effects (rare) would fire them multiple times. A cached `_discovered: list[OmnigentExtension] | None = None` singleton with a `reset_discovery_cache()` for tests would make discovery idempotent.

**Lifecycle-stage edge cases**

If `pre_init` raises, the extension must be excluded from subsequent stages. The current install loop has no such tracking; a failed `pre_init` followed by a successful `routers()` call would mount routes for an extension whose pre-init failed. The `install_extensions` refactor in Step 2 must maintain a `healthy: set[str]` that tracks which extensions passed pre-init and skip unhealthy ones in later stages.

**Testing the discovery path**

`discover_extensions()` is heavily patched in tests via `monkeypatch` of the module-level function in `omnigent.pluggable.registry`. A cached singleton adds a second patch point. Tests that call `discover_all_extensions()` must also reset the seam registries between tests (some seam registries are module-level singletons, e.g., `HARNESS_REGISTRY`).

**`memory_tool_intercept` migration (Step 1) sequencing**

The `is_memory_tool` / `execute_memory_tool` seam is in the hot path for every MCP `tools/call` that matches `memory__*`. Any Protocol-based indirection adds one `hasattr` check and one dict lookup per call. Profile before and after if latency matters for this path.

**Alembic migration scope**

ByteDesk extension DB models (`db_models.py`) share `omnigent.db.db_models.Base` and appear in the same Alembic migration chain as core tables (the `omnigent/db/migrations` directory). If `bytedesk_omnigent` is separated into its own installable package with independent lifecycle, the migrations must be split or the extension must provide its own Alembic environment. This is currently not a problem but becomes one if a "bytedesk-free" omnigent deployment is ever needed.

---

## 7. Recommended Approach

- **The plugin framework already exists** (`OmnigentExtension` Protocol, `discover_extensions`, `install_extensions`, `PluggableRegistry`, `SEAMS`). No rewrite is warranted — the design is sound and already mirrors ServiceStack's explicit-list-plus-entry-point-scan model.

- **Close the one seam violation first.** The deferred import of `bytedesk_omnigent.memory_tool_intercept` in `omnigent/server/routes/sessions.py` is the only place core bypasses the Protocol. Introduce a `tool_interceptors()` hook on `OmnigentExtension` (Step 1) to make it generic.

- **Add lifecycle staging (`pre_init` / `post_init`).** The current model has no "before-routes" hook. `BytedeskExtension` needs it for table-creation and boot-sweep coordination. Two optional Protocol methods, zero impact on existing extensions.

- **Add `get_extension` / `assert_extension` lookup.** Costs two lines; eliminates ad-hoc re-discovery in any future extension that needs to compose with another.

- **Add `OMNIGENT_DISABLED_EXTENSIONS`.** Four lines; gives operators a runtime kill switch without removing packages.

- **Migrate the two duplicate background-loop scaffolds.** `bus/reaper.py` and `scheduler/loop.py` should use `advisory_locked_loop` (already available in `maintenance.py`). Pure internal cleanup; no API change.

- **The self-registration mechanism is already correct: setuptools entry-points + `OMNIGENT_EXTENSIONS` env-var fallback.** An extension declares `[project.entry-points."omnigent.extensions"]` in its `pyproject.toml` and the host discovers it without any startup code edit. This is the ServiceStack `IPlugin` model in idiomatic Python.

---

---

# Part 2 — Kernel / Core / Extensions

## Overview

Part 1 described the system as two tiers (framework + extension). The refined architecture has **three tiers** with a precise boundary at each level:

```
┌─────────────────────────────────────────────────────┐
│  EXTENSIONS (third-party / optional)                │
│  bytedesk_omnigent, future third-party packages     │
├─────────────────────────────────────────────────────┤
│  CORE (kernel + bundled first-party plugins)        │
│  harnesses, tools, policies, stores, auth …         │
├─────────────────────────────────────────────────────┤
│  KERNEL (boot machinery only)                       │
│  Plugin Protocol, Host, PluggableRegistry,          │
│  discovery, lifecycle, composition root             │
└─────────────────────────────────────────────────────┘
```

The key insight: the boundary between KERNEL and CORE is not "framework vs. application code." It is "what would still be true in a different domain?" The kernel is the machinery for hosting plugins. CORE is the kernel enriched by a curated set of first-party plugins — plugins that happen to ship in the same repository and are added to the host by default, but which use the **same registration contract** as any third-party plugin. There is no privileged hard-wiring for first-party code: a first-party harness extension registers into `HARNESS_REGISTRY` through the same `PluggableRegistry.discover_extensions(hook="harness_descriptors")` call a third-party harness would use.

This is precisely the ServiceStack model. ServiceStack ships `MetadataFeature`, `AuthFeature`, `CorsFeature`, and `RazorFormat` as `IPlugin`s that the default `AppHost` calls `Plugins.Add(new ...)` on — they are first-party plugins, not special-cased core code. The framework is validated ("dogfooded") by its own first-party use: if the plugin seam cannot cleanly host `AuthFeature`, it cannot cleanly host a third-party auth replacement either. The same logic applies here.

---

## 8. The Kernel — Exact File Inventory

The kernel is the minimum set of files required to boot the system and host plugins. Nothing in the kernel is domain-specific. Removing any kernel file makes the host unable to discover, stage, or dispatch to any plugin.

### 8.1 Kernel files (current `omnigent/` codebase)

| File | Kernel role |
|---|---|
| `omnigent/extensions.py` | Defines `OmnigentExtension` Protocol; owns `discover_extensions()`, `install_extensions()`, the eight `extension_*()` aggregator functions. This IS the plugin contract and the discovery mechanism. |
| `omnigent/pluggable/__init__.py` | Public re-export of `PluggableRegistry` and the error taxonomy. The package docstring states the 4-invariant recipe that every seam follows. |
| `omnigent/pluggable/registry.py` | `PluggableRegistry[T]` — named-factory registry, `OMNIGENT_USE_<SEAM>` override, `discover_extensions(hook=...)` per-seam hook. The single shared data structure behind all ten seams. |
| `omnigent/pluggable/manifest.py` | `SEAMS` tuple (the single source of truth for which seams exist), `discover_all_extensions()`, `capability_manifest()`. |
| `omnigent/pluggable/errors.py` | `ProviderError` + subclasses (`ProviderNotRegistered`, `RegistryConflict`, `ProviderUnconfigured`, `ProviderUnavailable`). Shared error taxonomy for all seams. |
| `omnigent/kernel/lifespan_phases.py` | `LifespanPhase` ABC, `LifespanOrchestrator` (topological DAG), `topological_order`, `LifespanContext`, `LifespanCycleError`. The authoritative lifecycle engine. |
| `omnigent/server/app.py` (the composition root fragment) | `create_app()` signature, the `_lifespan` context manager (specifically: the calls to `discover_all_extensions()`, `install_extensions()`, `extension_principal_resolvers()`, `extension_background_factories()`). **Not** the 2000+ lines of domain route mounting — those are first-party plugin contributions (see Section 9). |

### 8.2 What does NOT belong in the kernel

Everything else in `omnigent/` is either a first-party plugin (registered through the kernel seams) or runtime infrastructure that a first-party plugin contributes. Specifically:

- `omnigent/inner/` — harness executors. Each is a first-party harness plugin.
- `omnigent/runtime/harnesses/` — the harness registry and process manager. The registry is a `PluggableRegistry` (kernel-seam based); the process manager is runtime infrastructure contributed by a first-party "harness management" plugin.
- `omnigent/tools/` — the tool manager and builtin tools. The registry (`_BUILTIN_REGISTRY`) is a plain dict today; it should become a `PluggableRegistry` seam, and each tool group is a first-party tool plugin.
- `omnigent/policies/` — the policy engine and builtin policies. The policy registry (`load_registry`) is a module-scanned dict; it should become a `PluggableRegistry` seam. Each policy module is a first-party policy plugin.
- `omnigent/stores/` — persistence stores. Each store is a first-party storage plugin.
- `omnigent/server/routes/` — HTTP routes. Each route group is a first-party routes plugin.
- `omnigent/coordination/` — coordination backplane. Already a `PluggableRegistry` seam (`coordination_backplane`); `InProcessBackplane` and `NatsBackplane` are first-party backplane plugins.
- `omnigent/identity/` — identity machinery. Already uses `PluggableRegistry` seams (`assertion_verifier`, `outbound_credential`, `authorizer`); each default verifier/provider is a first-party identity plugin.
- `omnigent/onboarding/secrets.py` — secret backend chain. Extension-contributed backends already flow through `extension_secret_backends()`; the `LocalBackend` is a first-party secret plugin.
- `omnigent/runtime/memory_maintenance.py` — memory maintenance loop. A first-party background plugin.
- `omnigent/server/performance_metrics.py` — metrics publish loop. A first-party background plugin.

### 8.3 The kernel is already nearly extractable

The kernel files listed in 8.1 have no imports of domain types (agents, conversations, tools, harnesses). Their only dependencies are:
- Python stdlib (`importlib`, `importlib.metadata`, `logging`, `os`, `typing`, `abc`, `asyncio`)
- `fastapi.APIRouter` and `fastapi.FastAPI` (for `install_extensions`)
- Each other

`omnigent/kernel/lifespan_phases.py` keeps the concrete domain imports deferred inside phase `startup()` / `shutdown()` methods. `LifespanContext` carries injected app wiring as `Any` fields plus `state: dict[str, Any]`, so the module stays import-light while still serving as the authoritative lifecycle engine.

---

## 9. The Core — First-Party Plugins

CORE = KERNEL + these first-party plugins. Each plugin registers itself into one or more kernel seams using the same `OmnigentExtension` / `PluggableRegistry` contract a third party would use. The table below maps each current `omnigent/` subpackage to its proposed first-party plugin identity.

### 9.1 Subpackage → First-Party Plugin Table

| Current subpackage | Proposed plugin name | Kernel seams it registers into | Depends on (boot order) | Notes |
|---|---|---|---|---|
| `omnigent/stores/` (agent, conversation, file, artifact, comment, permission, policy, memory) | `omnigent.stores` | `artifact_store` (already `PluggableRegistry`), `agent_memory` (already `PluggableRegistry`), `memory_embedder` (already `PluggableRegistry`) | kernel only | Store ABCs stay in kernel as type contracts; the `SqlAlchemy*` impls are the plugin contribution. |
| `omnigent/db/` | `omnigent.db` | None (provides `Base`, engine utils, migrations — kernel infrastructure) | kernel only | Shared `Base` is a kernel type; Alembic env is plugin-contributed. |
| `omnigent/inner/` + `omnigent/runtime/harnesses/` | `omnigent.harnesses` (one plugin per harness, or a single "builtin harnesses" plugin) | `harness` (`PluggableRegistry[HarnessDescriptor]`, hook `harness_descriptors`) | `omnigent.stores` | Each `HarnessDescriptor` in `_DEFAULT_DESCRIPTORS` becomes a `harness_descriptors()` contribution instead of hard-wired default set. The process manager is plugin-provided runtime infrastructure. |
| `omnigent/tools/builtins/` | `omnigent.tools.builtins` | `tools` (new `PluggableRegistry` seam, hook `tool_factories`) | `omnigent.stores`, `omnigent.harnesses` | Today `_BUILTIN_REGISTRY` is a dict with `**extension_tool_factories()` spliced in at import time. Convert to a proper `PluggableRegistry` keyed by tool name. The default set (web_search, upload_file, memory_*, sys_* tools) becomes this plugin's `tool_factories()` return. |
| `omnigent/policies/` | `omnigent.policies` | `policies` (new `PluggableRegistry` seam, hook `policy_modules`; or keep module-scan pattern behind a seam) | `omnigent.stores` | `BUILTIN_POLICY_MODULES` list becomes this plugin's `policy_modules()` return. `load_registry()` stays as the seam aggregator. |
| `omnigent/identity/` | `omnigent.identity` | `assertion_verifier`, `outbound_credential`, `authorizer` (all already `PluggableRegistry` seams) | kernel only | `HmacAssertionVerifier`, `StaticSecretCredentialProvider`, `OwnerAllowAuthorizer` are first-party identity plugin defaults. |
| `omnigent/coordination/` | `omnigent.coordination` | `coordination_backplane` (already `PluggableRegistry`) | kernel only | `InProcessBackplane` is the first-party default; `NatsBackplane` is an optional first-party alternate. |
| `omnigent/stores/artifact_store/` | `omnigent.artifact_store` | `artifact_store` (already `PluggableRegistry`, hook `artifact_store_providers`) | `omnigent.db` | `LocalArtifactStore`, `DataBricksVolumesArtifactStore`, `NatsObjectStoreArtifactStore` are all first-party providers. |
| `omnigent/stores/memory_store/` | `omnigent.memory` | `agent_memory`, `memory_embedder` (both already `PluggableRegistry` seams) | `omnigent.db` | `SqlAlchemyMemoryStore` + `ComposedAgentMemoryProvider` are first-party defaults. |
| `omnigent/server/routes/` | `omnigent.routes` | `routers` (via `OmnigentExtension.routers()` — already exists) | all stores, `omnigent.identity` | Each route factory in `omnigent/server/routes/` is a first-party router contribution. Today they are mounted directly inside `create_app()`; a first-party routes plugin would mount them via `routers()`. |
| `omnigent/onboarding/secrets.py` | `omnigent.secrets` | `secret_backends` (via `OmnigentExtension.secret_backends()`) | kernel only | `LocalBackend` (keyring + file) is the first-party default; extension-contributed backends prepend. |
| `omnigent/runtime/memory_maintenance.py` | `omnigent.memory_maintenance` | `background_tasks` (via `OmnigentExtension.background_tasks()`) | `omnigent.memory` | `memory_maintenance_loop` becomes a background task contributed by this plugin. |
| `omnigent/server/performance_metrics.py` | `omnigent.metrics` | `background_tasks` | kernel only | `publish_server_metrics_periodically` becomes a background task contributed by this plugin. |
| `omnigent/spec/` | `omnigent.spec` | `spec_source` (already `PluggableRegistry`, hook `spec_source_providers`) | `omnigent.stores` | `spec_source_registry` is already a `PluggableRegistry`; `omnigent.spec` is this plugin's registration of the default source. |
| `omnigent/skills/` | `omnigent.skills` | `routers` + `tool_factories` (skill-acquisition tools) | `omnigent.spec` | Skill acquisition tools (`sys_skill_*`) become a first-party tool plugin; the skills route group becomes a first-party router contribution. |
| `omnigent/terminals/` | `omnigent.terminals` | `tool_factories` (sys_terminal_* tools) + runtime infrastructure | `omnigent.harnesses` | Terminal registry is runtime infrastructure; the `sys_terminal_*` tools are a first-party tool plugin. |

### 9.2 The Dogfooding Argument

When a first-party harness like `claude-sdk` registers through `HARNESS_REGISTRY.register("claude-sdk", lambda: HarnessDescriptor(...))` instead of appearing in `_DEFAULT_DESCRIPTORS`, three things are true simultaneously:

1. **The seam is validated.** If `register()` cannot handle the volume and variety of the 12 built-in harnesses, it cannot handle a third-party harness either. Shipping all first-party harnesses through the seam proves the seam works.
2. **Core has no privileged harnesses.** A deployer who wants to ship only `claude-sdk` can register only that descriptor. There is no unreachable hard-coded set.
3. **Testing becomes uniform.** Tests inject fakes via `registry.register("test-harness", ...)` using the same API the real harnesses use. There is no split between "how tests add harnesses" and "how production adds harnesses."

ServiceStack expresses this identically. The team ships `AuthFeature` as a plugin not because authentication is optional, but because expressing it as a plugin proves the plugin seam is expressive enough to host it. A framework that requires special-casing its own features is a framework whose plugin seam is incomplete.

### 9.3 Dependency Order for Boot

The proposed boot sequence (replacing the monolithic `_lifespan` with composable `LifespanPhase` nodes):

```
kernel boots
    → discover_all_extensions()          [kernel: manifest.py]
    → db plugin pre_init                 [create tables, run migrations]
    → stores plugin pre_init             [build engine + session factories]
    → identity plugin registers          [assertion_verifier, credential, authz seams]
    → coordination plugin registers      [backplane seam]
    → harnesses plugin registers         [HARNESS_REGISTRY seam]
    → tools plugin registers             [_BUILTIN_REGISTRY / tools seam]
    → policies plugin registers          [load_registry() / policies seam]
    → spec plugin registers              [spec_source seam]
    → routes plugin registers            [install_extensions() → routers()]
    → secrets plugin registers           [secret backend chain]
    → third-party extensions register    [same stages, same seams]
    → lifespan background tasks start    [extension_background_factories()]
    → server ready
```

This is a topological sort over the same `LifespanPhase.depends_on` DAG that `LifespanOrchestrator` already implements in `omnigent/kernel/lifespan_phases.py`. The only change is that first-party "core" packages become `LifespanPhase` contributors rather than inline `_lifespan` statements. `ExtensionBackgroundTasksPhase` is the proof that the pattern works for third-party extensions and now also starts first-party background task contributions.

---

## 10. The Three-Tier Picture — Concrete Seam Map

```
KERNEL
  omnigent/extensions.py          OmnigentExtension Protocol, discover/install
  omnigent/pluggable/             PluggableRegistry, SEAMS, manifest
  omnigent/kernel/lifespan_phases.py  LifespanPhase, LifespanOrchestrator

CORE (kernel + first-party plugins below)
  omnigent.db          → pre_init: run Alembic migrations
  omnigent.stores      → registers: artifact_store, agent_memory, memory_embedder seams
  omnigent.identity    → registers: assertion_verifier, outbound_credential, authorizer seams
  omnigent.coordination → registers: coordination_backplane seam
  omnigent.harnesses   → registers: harness seam (12 descriptors via harness_descriptors hook)
  omnigent.tools       → registers: tools seam (web_search, memory_*, sys_* via tool_factories hook)
  omnigent.policies    → registers: policies seam (BUILTIN_POLICY_MODULES via policy_modules hook)
  omnigent.spec        → registers: spec_source seam
  omnigent.memory      → registers: agent_memory + memory_embedder seams
  omnigent.secrets     → registers: secret_backends (LocalBackend)
  omnigent.routes      → registers: routers (sessions, agents, comments, skills, policy_registry …)
  omnigent.skills      → registers: sys_skill_* tools + skills router
  omnigent.terminals   → registers: sys_terminal_* tools
  omnigent.metrics     → registers: background_tasks (metrics publish loop)
  omnigent.memory_maintenance → registers: background_tasks (decay/eviction loop)

EXTENSIONS (third-party / optional)
  bytedesk_omnigent   → registers into ALL above seams (routers, tool_factories,
                         policy_modules, secret_backends, background_tasks,
                         config_descriptors, principal_resolvers, assertion_verifiers,
                         outbound_credential_providers, authorization_providers)
  <future third-party>  → same contract
```

---

---

# Part 3 — The SDK / Framework Facade

## Overview

The kernel and plugin contract described in Parts 1 and 2 are correct as an implementation target. They are not the right developer experience surface. A developer writing a new extension today must:

1. Implement `OmnigentExtension` Protocol methods by hand
2. Know which kernel seam names correspond to which method names (`tool_factories`, `policy_modules`, `harness_descriptors`, ...)
3. Write entry-point registration in `pyproject.toml`
4. Understand `PluggableRegistry`, `SEAMS`, and `discover_extensions()` internals
5. Manage deferred imports to avoid circular-import hazards

The SDK is a thin **Facade** over the kernel that hides all of this behind developer-friendly declarative abstractions. The SDK does not replace the kernel — it compiles down to the same `OmnigentExtension` Protocol and `PluggableRegistry` calls the kernel already dispatches. The SDK is the public API surface; the kernel is the implementation.

**ServiceStack analog:** ServiceStack exposes `[Route]`, `[Authenticate]`, `[ValidateRequest]` attributes and `AppHostBase.Configure()` as the developer-facing API. Underneath, every attribute resolves to a plugin filter registered into the `IPlugin`/`IAppHost` machinery. The attributes are the Facade; the machinery is the kernel.

---

## 11. Design Patterns Hidden by the SDK

| Pattern | Where it exists in the kernel | What the SDK hides |
|---|---|---|
| **Microkernel / Plugin** | `OmnigentExtension` Protocol, `discover_extensions`, `install_extensions` | The Protocol declaration, the entry-point string, the discovery call |
| **Registry** | `PluggableRegistry[T]`, `_BUILTIN_REGISTRY` dict, `POLICY_REGISTRY` list | `registry.register(name, factory)`, `registry.discover_extensions(hook=...)`, the hook name strings |
| **Facade** | The SDK itself | Everything above |
| **Builder** | `create_app()` factory function | The store injection, auth wiring, middleware stacking |
| **Template Method** | `LifespanPhase.startup`/`shutdown` ABC | The `depends_on` DAG declaration, `LifespanOrchestrator` |
| **Strategy** | `PluggableRegistry.resolve_default()` → `OMNIGENT_USE_<SEAM>` | Seam name env-var construction, factory invocation |
| **Chain of Responsibility** | Secret backend chain (`extension_secret_backends()` → `LocalBackend`), principal resolver chain (`CompositeAuthProvider`) | Chain construction, fallback logic |
| **Decorator / Interceptor** | `memory_tool_intercept.py` — server-side tool interception before runner dispatch | Interception registration, `is_memory_tool()` prefix check |

---

## 12. The SDK Public Surface

### 12.1 Proposed module layout

```
omnigent/sdk/__init__.py        re-exports the main entry points
omnigent/sdk/extension.py       @extension decorator + Extension base class
omnigent/sdk/host.py            Host builder (fluent API)
omnigent/sdk/contrib.py         @tool, @harness, @policy, @background, @router decorators
omnigent/sdk/types.py           public type re-exports (Tool, ToolContext, etc.)
```

### 12.2 Before (today's verbose Protocol form)

A third-party developer today must write this to contribute one tool and one policy:

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
        return []  # no routes

    def tool_factories(self) -> dict[str, Callable[[object], Tool]]:
        from mypackage.tools import MyCustomTool
        return {"my_custom_tool": lambda _c: MyCustomTool()}

    def policy_modules(self) -> list[str]:
        return ["mypackage.policies.rate_limiter"]

    def background_tasks(self):
        return []
```

```toml
# pyproject.toml
[project.entry-points."omnigent.extensions"]
my-extension = "mypackage.extension:MyExtension"
```

```python
# mypackage/policies/rate_limiter.py
from bytedesk_omnigent.policies import PolicyRegistryRaw

POLICY_REGISTRY: list[PolicyRegistryRaw] = [
    {
        "handler": "mypackage.policies.rate_limiter.per_agent_rate_limit",
        "kind": "factory",
        "name": "Per-Agent Rate Limiter",
        "description": "Limit calls per agent per minute.",
        "params_schema": {"type": "object", "properties": {"calls_per_minute": {"type": "number"}}, "required": ["calls_per_minute"]},
    }
]

def per_agent_rate_limit(calls_per_minute: float):
    def _policy(event, context):
        ...
    return _policy
```

### 12.3 After (proposed SDK form)

```python
# mypackage/extension.py
from omnigent.sdk import extension, tool, policy, background

@extension(name="my-extension")
class MyExtension:

    @tool(name="my_custom_tool")
    def my_custom_tool(self):
        from mypackage.tools import MyCustomTool
        return MyCustomTool()

    @policy(
        name="Per-Agent Rate Limiter",
        description="Limit calls per agent per minute.",
        kind="factory",
        params_schema={"type": "object", "properties": {"calls_per_minute": {"type": "number"}}, "required": ["calls_per_minute"]},
    )
    def per_agent_rate_limit(self, calls_per_minute: float):
        def _policy(event, context): ...
        return _policy

    @background
    async def my_maintenance_loop(self):
        while True:
            await asyncio.sleep(300)
            ...
```

```toml
# pyproject.toml — the ONLY non-Python declaration still required
[project.entry-points."omnigent.extensions"]
my-extension = "mypackage.extension:MyExtension"
```

The `@extension` decorator:
- Makes `MyExtension` conform to `OmnigentExtension` Protocol automatically
- Collects all `@tool`-decorated methods into `tool_factories()` return value
- Collects all `@policy`-decorated methods into both `policy_modules()` (by synthesizing a module) and the `POLICY_REGISTRY` list
- Collects all `@background`-decorated methods into `background_tasks()`
- Generates `routers()` returning `[]` unless `@router`-decorated methods are present

The entry-point string in `pyproject.toml` remains necessary — it is the self-registration hook that lets the kernel discover the package without the host importing it by name. This is the irreducible minimum that cannot be hidden without a separate build step.

### 12.4 Host Builder (fluent API for composition roots)

For test setups, embedded deployments, or lightweight CLI tools that want to compose a host without a full `create_app()` call:

```python
# Proposed fluent builder
from omnigent.sdk import Host

host = (
    Host.build()
    .with_store(conversation_store=SqlAlchemyConversationStore("sqlite:///test.db"))
    .with_extension(MyExtension())
    .with_extension(BytedeskExtension())
    .disable("omnigent.realtime")        # OMNIGENT_DISABLED_EXTENSIONS analog
    .build_app()                          # returns FastAPI
)
```

This wraps `create_app()` behind a fluent builder, hiding the 15-parameter constructor signature. The builder compiles down to the same `create_app()` call with the same stores and extension list. It is not a second source of truth — it is a named-argument collector that produces the same positional call.

**ServiceStack analog:** `new AppHost("My App", typeof(MyServices).Assembly).Init().Start("http://*:8080/")` — the fluent chain that builds the host.

### 12.5 Harness Decorator

A first-party or third-party harness plugin registers a descriptor:

```python
# Current form (requires knowing HarnessDescriptor and PluggableRegistry)
from omnigent.runtime.harnesses.descriptors import HarnessDescriptor
from omnigent.pluggable import PluggableRegistry

def harness_descriptors(self) -> dict[str, ...]:
    return {
        "my-harness": lambda: HarnessDescriptor(
            name="my-harness",
            module_path="mypackage.my_harness",
        )
    }

# Proposed SDK form
from omnigent.sdk import extension, harness

@extension(name="my-extension")
class MyExtension:
    @harness(name="my-harness", module_path="mypackage.my_harness")
    def my_harness(self): ...
```

The `@harness` decorator synthesizes the `harness_descriptors()` hook return value, hiding `HarnessDescriptor` construction and the `{name: lambda: descriptor}` factory shape that the `PluggableRegistry` requires.

### 12.6 Tool Interceptor Decorator

Closing the `memory_tool_intercept` seam violation (Part 1, Step 1) via the SDK:

```python
from omnigent.sdk import extension, tool_interceptor

@extension(name="my-extension")
class MyExtension:
    @tool_interceptor(prefix="memory__")
    def memory_tool_handler(self, tool_name, arguments, *, caller_agent_id, caller_department):
        from mypackage.memory_intercept import execute_memory_tool
        return execute_memory_tool(tool_name, arguments, caller_agent_id=caller_agent_id, caller_department=caller_department)
```

The `@tool_interceptor` decorator synthesizes the `tool_interceptors()` hook return value (`{prefix: handler}`), hiding the prefix-match pattern that core uses to intercept tool calls before runner dispatch.

### 12.7 Layering Invariant

The SDK must satisfy one invariant: **it must compile down to the same kernel Protocol contract**. Concretely:

```python
from omnigent.sdk import extension, tool
from omnigent.extensions import OmnigentExtension

@extension(name="test")
class TestExt:
    @tool(name="my_tool")
    def my_tool(self): return MyTool()

# Must be true — the decorator makes the class conform to the Protocol:
assert isinstance(TestExt(), OmnigentExtension)

# Must produce the same result as the manual Protocol form:
ext = TestExt()
factories = ext.tool_factories()
assert "my_tool" in factories
assert isinstance(factories["my_tool"]({}), MyTool)
```

The SDK does not introduce a parallel discovery mechanism, a parallel plugin list, or a parallel lifecycle. It generates code that the kernel's existing `discover_extensions()` / `install_extensions()` / `PluggableRegistry.discover_extensions()` calls consume identically to hand-written Protocol implementations.

### 12.8 Versioning and Stability

- **SDK (`omnigent/sdk/`)** — the public API surface. Semver-stable: breaking changes to decorator signatures, builder methods, or public type re-exports require a major version bump. Third-party extension authors depend on this surface and must not be broken by kernel refactors.
- **Kernel (`omnigent/extensions.py`, `omnigent/pluggable/`)** — the implementation. May churn between minor versions as seams are added, aggregators are generalized, or the lifecycle DAG is refined. Changes that break the kernel's own Protocol shape are breaking changes to the SDK (they propagate up), so the kernel's protocol surface is semver-anchored too, but via the SDK surface not directly.
- **First-party plugins (`omnigent.harnesses`, `omnigent.tools`, etc.)** — internal; their seam-registration APIs are not public. They may be refactored freely as long as the kernel seam contracts are preserved.

The practical rule: any import from `omnigent.sdk.*` is stable. Any import from `omnigent.extensions`, `omnigent.pluggable`, or `omnigent.server.lifespan_phases` is semi-stable (breaking changes require a major bump but are permissible between minors with a deprecation cycle). Any import from `omnigent.runtime.*`, `omnigent.inner.*`, or `omnigent.server.routes.*` is internal and may change without notice.

---

## 13. Updated Recommended Approach

This supersedes Section 7.

**Tier 1 — Kernel (extract, stabilize, never change without a major version)**

Identify the kernel files listed in Section 8.1. Draw a hard import boundary: nothing outside those files is imported by them at module scope. `omnigent/kernel/lifespan_phases.py` follows this pattern with deferred imports inside `startup()` / `shutdown()`. `omnigent/server/app.py` does not — refactor `create_app()` so all domain imports are deferred into lifecycle phases or injected as parameters. This makes the kernel independently testable with zero domain dependencies.

**Tier 2 — Core as first-party plugins (incremental, one subpackage at a time)**

Following the dependency order in Section 9.3:
1. Convert `omnigent/stores/artifact_store/` to register through `artifact_store` `PluggableRegistry` (already done — verify coverage).
2. Convert `omnigent/tools/builtins/` from the `_BUILTIN_REGISTRY` dict + `**extension_tool_factories()` splice to a proper `PluggableRegistry` seam with hook `tool_factories`. Builtin tools register themselves; extension tools continue via the same hook.
3. Convert `omnigent/policies/builtins/` from the `BUILTIN_POLICY_MODULES` list to a `policy_modules()` hook on a first-party plugin. The module-scan pattern in `load_registry()` stays unchanged as the seam aggregator.
4. Extract `hermes` from `_DEFAULT_DESCRIPTORS` into a `harness_descriptors()` hook on a first-party harnesses plugin (Section 5, Step 8).
5. Close the `memory_tool_intercept` seam violation with a `tool_interceptors()` hook (Section 5, Step 1, `tool_interceptor` SDK decorator).

**Tier 3 — SDK Facade (build after kernel is stable)**

Build `omnigent/sdk/` once the kernel file list is frozen and the Protocol contract is semver-anchored. The SDK decorators (`@extension`, `@tool`, `@policy`, `@harness`, `@background`, `@router`, `@tool_interceptor`) are the only stable public surface. The Host builder (`Host.build()...`) is the composition-root replacement for test and embedded-server contexts.

**On ServiceStack dogfooding**

ServiceStack ships `MetadataFeature`, `AuthFeature`, `ValidationFeature`, `CorsFeature`, `RazorFormat`, and `SwaggerFeature` all as `IPlugin`s that the default AppHost adds via `Plugins.Add(...)`. The framework's plugin seam is proven correct because the framework's own first-party features — including auth, which every non-trivial service needs — are expressed as plugins. Omnigent should apply the same discipline: the `omnigent.harnesses` first-party plugin, the `omnigent.tools.builtins` first-party plugin, and the `omnigent.policies` first-party plugin should all register through the same `PluggableRegistry` and `OmnigentExtension` seam that `bytedesk_omnigent` uses. When they do, two things are simultaneously true: the seam is validated (it can host the most complex use cases), and the system is deployable in subsets (a deployment that needs only the `claude-sdk` harness can register only that descriptor, with no dead code from the other eleven).

**Summary of concrete next actions (priority order)**

1. Lock the kernel file list (Section 8.1). Add an import-guard CI check: no file outside the kernel list may be imported by a kernel file at module scope.
2. Add `pre_init` / `post_init` / `after_init` optional Protocol methods to `OmnigentExtension` (Section 4.3). Update `install_extensions()`. Zero impact on existing extensions.
3. Add `get_extension(name)` / `assert_extension(name)` to `omnigent/extensions.py`.
4. Add `OMNIGENT_DISABLED_EXTENSIONS` env-var filter to `discover_extensions()`.
5. Close the `memory_tool_intercept` seam violation via a `tool_interceptors()` hook.
6. Convert `omnigent/tools/builtins/__init__.py` `_BUILTIN_REGISTRY` to a `PluggableRegistry` seam.
7. Extract `hermes` from `_DEFAULT_DESCRIPTORS` into the `bytedesk_omnigent` extension's `harness_descriptors()` hook.
8. Build `omnigent/sdk/` with `@extension`, `@tool`, `@policy`, `@harness`, `@background`, `@router`, `@tool_interceptor` decorators, and the `Host` builder. SDK decorators must satisfy the `isinstance(ext, OmnigentExtension)` invariant.
9. Migrate first-party subpackages to register as plugins, following the dependency order in Section 9.3, using the SDK decorators.
