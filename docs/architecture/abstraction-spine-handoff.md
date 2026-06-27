# Core-refactor "abstraction spine" — sequential handoff plan (Phases 1–5)

**Status:** plan (handoff). **Owner stream:** A7. **Jira:** BDP-2327 → BDP-2331
(one task per phase). **Authority:** ADR-0143 (generic `omnigent.extensions` seam),
fork-discipline rule in `.claude/rules/worktree-lifecycle.md` (additive-only edits to
shared/upstream files), dual-DB rule (`Text`-JSON, soft FKs, ABC + `SqlAlchemy*Store`
impl + `sql_X_to_entity` converter).

This document is the **ordered, parity-gated build plan** for the five phases that
together turn `omnigent/server/app.py` from a hand-wired god-factory into a thin
shell driven by three abstractions — **ServiceRegistry**, **HarnessProvider**, and
**StoreBootstrapper** — without ever rewriting the file in one shot. It is the unit
we deliberately **do not parallel-build**: every phase mutates (or is gated by) the
same ~70-line region of `create_app`, so two phases in flight at once collide in the
literal same diff hunk. The sequence below is the contention-safe order.

The invariants this plan leans on (line anchors, `app.state` key set, the four
harness-registration sites, the 20-branch dispatch chain (16 set-family `elif`s + 4
predicate tails), the extension-seam getter shape) are **pinned by a contract test** so the plan can't silently drift:
`tests/extensions/test_abstraction_spine_contract.py`. Re-pin that test (don't delete
its asserts) whenever a phase intentionally moves an anchor.

---

## Why a spine, and why it must be sequential

Today `create_app` (`omnigent/server/app.py:734`–`1072`, **2032 lines** in the file)
does five jobs inline:

1. constructs the cross-cutting singletons (`RunnerControlRegistry`, `RunnerRouter`,
   `HostRegistry`, `ServerMcpPool`, `ServerPerformanceMetrics`, …) and stashes the
   factory-body key set on `app.state.*` (`app.py:1052`–`1066`); the
   `app.state.harness_process_manager` write happens later inside `_lifespan`
   (`app.py:915`) and is Phase 3's concern;
2. selects + starts the inner harness process manager inside `_lifespan`
   (`app.py:909`–`916`, `HarnessProcessManager()` + `set_harness_process_manager`);
3. receives the **already-constructed** stores as ~9 positional/keyword params
   (`agent_store`, `file_store`, … `account_store`) that the CLI builds by hand at
   `omnigent/cli.py:2934`–`2940` then forwards at `omnigent/cli.py:3055`;
4. drives a long `_lifespan` body (`app.py:871`–`1043`) of ordered startup steps and
   a matching teardown `finally`;
5. routes every runner tool call through the **20-branch `elif` chain** (16
   set-family branches + 4 predicate tails: spec-builtin, spec-local-python,
   UC-function, and the spec-callable `else` fallback) in
   `omnigent/runner/tool_dispatch.py:3412`–`3570`.

Each phase extracts **one** of those jobs behind an abstraction. The abstractions
live in **new files** under `omnigent/server/spine/` (core-generic, no ByteDesk
reference — they are the upstream-contributable seam, exactly like
`omnigent/extensions.py`). The phases are sequential because **app.py is a
single-writer contended file**:

- Phases 1, 3, and 4 all edit the **same `_lifespan` closure and the same
  `app.state` assignment block** (`app.py:871`–`1066`). Two of them open concurrently
  → the second rebases onto a `_lifespan` whose shape the first already changed →
  guaranteed hand-merge of the literal same hunk, and the merge can silently drop a
  teardown step (the failure mode the dual-path test below exists to catch).
- Phase 2 (store hooks) changes the **`create_app` signature region**
  (`app.py:734`–`752`) and the **CLI construction block** (`cli.py:2934`–`2940`,
  `cli.py:3055`). Phase 3 (lifespan phases) reads the store handles Phase 2 produces;
  starting 3 before 2 lands means 3 wires against params that are about to move.
- Phase 5 (tool-dispatch registry) is the only phase that does **not** touch app.py —
  but it depends on the **tool-exec-context** object that Phase 4 introduces (Phase 4
  threads a single `ToolExecContext` through `_lifespan` → routes → `dispatch_tool`,
  replacing today's ad-hoc kwarg soup). So 5 after 4.

Net ordering: **1 → 2 → 3 → 4 → 5**, each gated on the prior phase being **landed on
`develop`** (not merely pushed), with a dual-path parity test green before the gate
opens. Parallelizing any adjacent pair reintroduces the app.py merge collision the
whole spine exists to remove.

---

## The three abstractions (all land in Phase 1)

All three are **new files**, mirroring the `omnigent/extensions.py` seam conventions:
`from __future__ import annotations`, a `runtime_checkable` `Protocol` for the
contract, module-level logger, one-bad-entry-must-not-break-boot error isolation,
and a discovery/install split that stays unit-testable with injected fakes.

| Abstraction | New file | Replaces (inline today) | Shape |
|---|---|---|---|
| **ServiceRegistry** | `omnigent/server/spine/services.py` | the factory-body `app.state.*` block `app.py:1052`–`1066` (`runner_control_registry`, `runner_router`, `auth_provider`, `assertion_signer`, `host_registry`, `host_store`, `sandbox_config`, `managed_launches`, `server_metrics`, `server_metrics_otel`, `di_container`, `service_registry`) — **not** `harness_process_manager`, which is set inside `_lifespan` (`app.py:915`) and belongs to Phase 3 | a dict-like registry of named singletons + a `bind(app)` that copies each into `app.state` (so existing `request.app.state.*` reads are byte-for-byte unchanged) |
| **HarnessProvider** | `omnigent/server/spine/harness_provider.py` | the harness selection/start in `_lifespan` (`app.py:909`–`916`) and the registry read of `_HARNESS_MODULES` (`runtime/harnesses/__init__.py:34`) | `Protocol` with `process_manager() -> HarnessProcessManager` + `modules() -> dict[str,str]`; default impl returns `_HARNESS_MODULES` so behavior is identical |
| **StoreBootstrapper** | `omnigent/server/spine/store_bootstrapper.py` | the hand construction in `cli.py:2934`–`2940` | a builder that takes `db_uri` + `art_loc` and returns a frozen `StoreBundle` (agent/file/conversation/comment/policy/permission/artifact); `create_app` gains **one** optional `stores: StoreBundle \| None = None` kwarg (additive — the 9 existing params stay for back-compat) |

**Why one phase for all three:** they are pure additive new files plus the
`ServiceRegistry.bind(app)` swap, which is the *only* app.py edit in Phase 1 and is a
self-contained replacement of the contiguous `1052`–`1066` block. Landing the three
contracts together lets Phases 2–4 each consume a stable interface instead of a
moving target.

---

## Phase 1 — `BDP-2327`: spine contracts + ServiceRegistry bind

**Files touched**

- **New:** `omnigent/server/spine/__init__.py`,
  `omnigent/server/spine/services.py`,
  `omnigent/server/spine/harness_provider.py`,
  `omnigent/server/spine/store_bootstrapper.py`.
- **New tests:** `tests/extensions/test_abstraction_spine_contract.py` (pins the
  anchors), `tests/server/spine/test_services.py`,
  `tests/server/spine/test_harness_provider.py`.
- **Shared edit (additive, single hunk):** `omnigent/server/app.py` — replace the
  contiguous `app.state.*` block (`1052`–`1066`) with
  `ServiceRegistry(...).bind(app)`. No reflow of surrounding lines.

**Feature flag:** `OMNIGENT_SPINE_SERVICES` (default **off**). When off, the old
inline `app.state.*` assignments run; when on, `ServiceRegistry.bind(app)` runs. Both
produce the identical factory-body `app.state` key set. The flag is read once in `create_app` via the
existing `env_var_is_truthy` helper (already imported in this module,
`app.py:971`).

**Dual-path test approach:** the contract test asserts that, for both flag values, the
resulting `app.state` exposes the **same key set** (`{runner_control_registry,
runner_router, auth_provider, assertion_signer, host_registry, host_store,
sandbox_config, managed_launches, server_metrics, server_metrics_otel,
di_container, service_registry}`) and that each value is the same object identity passed in. The
registry impl is unit-tested in isolation with injected fakes (no FastAPI app needed
for the dict semantics) — the `discover/bind` split mirrors
`omnigent/extensions.py`'s `discover_extensions`/`install_extensions`.

**Why it can't parallel:** the `1052`–`1066` block is the **same hunk** Phase 3 will
later read from (lifespan-phase wiring pulls singletons out of the registry). Doing
1 and 3 together double-edits that block.

---

## Phase 2 — `BDP-2328`: StoreBootstrapper store-construction hook

**Files touched**

- **New impl:** `omnigent/server/spine/store_bootstrapper.py` gains the concrete
  `StoreBundle` dataclass + `build_stores(db_uri, art_loc)` (the Phase-1 file already
  declares the Protocol; Phase 2 fills the impl + converter wiring).
- **Shared edit (additive):** `omnigent/server/app.py` — add one optional kwarg
  `stores: StoreBundle | None = None` to `create_app` (`app.py:734`–`752`); when
  provided, unpack it into the existing locals **before** the existing param
  defaults, so the body below is untouched. Append a single field to the param list;
  do not reorder.
- **Shared edit (additive):** `omnigent/cli.py` — replace the 7-line manual store
  construction (`cli.py:2934`–`2940`) with `stores = build_stores(db_uri, art_loc)`
  and pass `stores=stores` at the `create_app(...)` call (`cli.py:3055`). Old call
  still type-checks because the 9 store params keep their defaults.

**Feature flag:** `OMNIGENT_SPINE_STORES` (default **off**). Off → CLI keeps the
inline construction + positional pass-through. On → CLI builds a `StoreBundle` and
passes `stores=`. Same store objects either way; AgentStore is provider-selected
through the pluggable registry while the remaining SQL stores stay SQL-backed (dual-DB rule:
`StoreBundle` holds the **already-constructed** stores; the bootstrapper performs no
schema work — table creation stays in each store's `__init__`, soft FKs and
`Text`-JSON columns unchanged).

**Dual-path test approach:** parametrize over the flag; assert the `StoreBundle`
fields are the same concrete store classes the inline path builds
(`NatsAgentStore` for AgentStore, `SqlAlchemyFileStore`, …) against **both** a SQLite
`db_uri` and a Postgres `db_uri` (the repo's existing dual-DB store fixtures), proving
no JSONB/native-type leak. A converter round-trip (`sql_X_to_entity`) is asserted on at
least one store to confirm the bundle wires the same converter path.

**Why it can't parallel:** Phase 3 reads store handles out of the bundle; Phase 4's
`ToolExecContext` carries store references. Both must build on the **landed**
`StoreBundle` shape. And the `create_app` signature region (`734`–`752`) is the same
region Phase 1's flag-read sits next to — concurrent edits collide on the factory
header.

---

## Phase 3 — `BDP-2329`: lifespan phases (ordered startup/teardown)

**Files touched**

- **New:** `omnigent/server/spine/lifespan.py` — a `LifespanPhase` `Protocol`
  (`async setup(ctx)` + `async teardown(ctx)`) and a `run_lifespan(phases, ctx)`
  driver that runs `setup` in order and `teardown` in **reverse** order inside the
  `finally`, mirroring today's hand-ordered teardown (`app.py:1013`–`1043`).
- **New:** `omnigent/server/spine/builtin_phases.py` — one phase object per existing
  startup step (thread-limiter bump, log-level, harness PM start, subagent block
  notifier, resource registry, runner-WS factory, default agents, policy registry,
  accounts auto-open, metrics loop, memory-maintenance loop, extension background
  tasks). Each phase's `setup`/`teardown` is the verbatim existing block, moved not
  rewritten.
- **Shared edit (additive):** `omnigent/server/app.py` — inside `_lifespan`
  (`app.py:871`–`1043`), when the flag is on, replace the inline body with
  `await run_lifespan(BUILTIN_PHASES, ctx)`; when off, the existing body runs. This
  is the **largest** app.py hunk and is exactly why it must be the only phase editing
  `_lifespan` at a time.

**Feature flag:** `OMNIGENT_SPINE_LIFESPAN` (default **off**). The driver preserves
the **exact** setup order and the reverse teardown order; the flag lets us diff the two
paths step-for-step.

**Dual-path test approach:** an integration test boots the FastAPI app with the flag
**off** and records the ordered sequence of side effects (harness PM started,
`set_harness_process_manager` called, resource registry set, …, then on shutdown the
reverse), then boots it **on** and asserts the recorded sequence is **identical**,
including teardown order. Background-task cancellation is asserted via the existing
`extension_background_factories()` seam (`omnigent/extensions.py:154`) so the
`_ext_bg_tasks` cancel path (`app.py:1020`–`1023`) is exercised in both paths.

**Why it can't parallel:** it edits the single largest contended hunk in app.py
(`_lifespan`). Any other phase touching `_lifespan` concurrently forces a manual merge
of step ordering — the highest-risk merge in the whole spine.

---

## Phase 4 — `BDP-2330`: ToolExecContext (thread one context, not kwarg soup)

**Files touched**

- **New:** `omnigent/server/spine/tool_exec_context.py` — a frozen `ToolExecContext`
  dataclass carrying the handles `dispatch_tool` currently receives as ~15 separate
  kwargs (`server_client`, `terminal_registry`, `resource_registry`,
  `filesystem_registry`, `session_inbox`, `session_async_tasks`, `mcp_manager`,
  `publish_event`, `agent_spec`, `conversation_id`, `task_id`, `agent_id`,
  `agent_name`, `runner_workspace`, `harness_client`).
- **Shared edit (additive):** `omnigent/runner/tool_dispatch.py` — `dispatch_tool`
  gains an optional `ctx: ToolExecContext | None = None`; when provided, the existing
  kwargs are derived from `ctx` at the top of the function (one unpack block), leaving
  the **20-branch `elif` chain (`tool_dispatch.py:3412`–`3570`) byte-for-byte
  unchanged**. The branches still read the same local names.
- **Shared edit (additive):** `omnigent/server/app.py` — the `_lifespan`/route wiring
  constructs the `ToolExecContext` once (behind the flag) and passes `ctx=` instead of
  the kwarg list.

**Feature flag:** `OMNIGENT_SPINE_TOOLCTX` (default **off**). Off → callers pass the
explicit kwargs (unchanged). On → callers pass a single `ctx`. The 20 branches are
identical on both paths — the context is a **parameter-shape** change only, never a
behavior change.

**Dual-path test approach:** drive `dispatch_tool` for a representative tool from each
branch family (`_OS_ENV_TOOLS`, `_REST_TOOLS`, `_FILE_TOOLS`, `_TERMINAL_TOOLS`,
`_SUBAGENT_TOOLS`, `_POLICY_TOOLS`, spec-builtin, spec-local-python, UC-function,
spec-callable fallback) once with explicit kwargs and once with an equivalent
`ToolExecContext`, and assert byte-identical tool output. The branch count is pinned
(20 = 16 set-family + 4 predicate tails) by the contract test so a future branch
addition that forgets `ctx` wiring fails loudly.

**Why it can't parallel:** it both edits `_lifespan` (post-Phase-3 shape) **and**
produces the `ctx` object Phase 5's registry dispatch consumes. Running it next to
Phase 3 re-collides on `_lifespan`; running it next to Phase 5 means 5 wires against a
`ToolExecContext` whose fields are still moving.

---

## Phase 5 — `BDP-2331`: tool-dispatch registry (retire the 20-branch elif)

**Files touched**

- **New:** `omnigent/server/spine/tool_registry.py` — a `ToolFamily` `Protocol`
  (`matches(tool_name, agent_spec) -> bool` + `async execute(tool_name, args, ctx)`)
  and a `TOOL_FAMILIES` ordered list, one entry per existing `elif` branch, each
  delegating to the **existing** `_execute_*_tool` function unchanged. Order is
  preserved exactly (MCP-first, then `_OS_ENV_TOOLS` … then the spec-callable
  fallback) so precedence is identical.
- **Shared edit (additive):** `omnigent/runner/tool_dispatch.py` — when the flag is
  on, `dispatch_tool` iterates `TOOL_FAMILIES` and dispatches to the first match;
  when off, the existing `elif` chain (`3412`–`3570`) runs. **No branch is deleted in
  this phase** — both paths coexist behind the flag until the parity test has run in
  production for a full release.

**Feature flag:** `OMNIGENT_SPINE_TOOLREGISTRY` (default **off**). This is the only
flag we keep longest: the `elif` chain stays in-tree (dead behind the flag) until a
follow-up cleanup task removes it, per fork-discipline (additive first, delete later
under its own ticket).

**Dual-path test approach:** for **every** tool family (all 16 set-family branches +
the 4 predicate-based tails: spec-builtin, spec-local-python, UC-function,
spec-callable), assert the
registry path selects the **same** family the `elif` chain would and returns identical
output for the same inputs. A precedence test asserts the registry's match order equals
the `elif` order (the MCP-manager short-circuit at `tool_dispatch.py:3406` stays a
pre-loop guard, not a family). Consumes the `ToolExecContext` from Phase 4 directly.

**Why it can't parallel:** it depends on Phase 4's `ToolExecContext` as the sole
argument each `ToolFamily.execute` receives. It also re-edits the same
`tool_dispatch.py` dispatch region Phase 4 touched — concurrent edits collide on the
function header and the elif/loop swap.

---

## Per-phase land gate (the parity ratchet)

Each phase is **landed** (merged to `develop`, not just pushed) before the next starts,
and each carries its flag **default-off**. The gate to open the next phase:

1. dual-path parity test for the phase is **green** (old path == new path);
2. the contract test `tests/extensions/test_abstraction_spine_contract.py` still
   passes (anchors intact, or intentionally re-pinned in the same PR);
3. the phase's flag is wired through `cli.py`/`app.py` but **defaults off**, so
   landing is a no-op for production until a later, separate "flip the flags" task
   turns them on one at a time with the same parity test as the safety net.

This is what makes the spine safe to build incrementally on a contended file: at no
point does `develop` change runtime behavior, and at no point are two phases editing
the same app.py hunk at once.

---

## Risks / open questions (for the implementer)

- **`app.state` read-sites are spread across routers.** Phase 1's `ServiceRegistry`
  must `bind` the identical key names; a typo silently breaks a router that reads
  `request.app.state.<key>`. The contract test pins the factory-body key set as the guard.
- **`_HARNESS_MODULES` is also read by `_omnigent_compat.OMNIGENT_HARNESSES`**
  (`omnigent/spec/_omnigent_compat.py:75`). HarnessProvider must keep returning the
  same dict identity/content so the validator allowlist doesn't drift. The provider's
  default impl returns `_HARNESS_MODULES` verbatim.
- **Teardown ordering is load-bearing** (`app.py:1013`–`1043` cancels metrics →
  memory-maintenance → extension bg tasks → managed launches → notifier → registries
  → runner_router → harness PM → terminal registry → mcp pool, in that order). Phase 3's
  reverse-order driver must reproduce it exactly; the integration parity test asserts
  the full ordered teardown, not just setup.
- **The 20-branch count is a moving target across releases.** If upstream adds a tool
  family before Phase 5 lands, the contract test's branch-count assert fails first —
  treat that as the signal to add the family to `TOOL_FAMILIES`, not to bump the number
  blindly.
- **Flag sprawl.** Five `OMNIGENT_SPINE_*` flags is intentional (one parity boundary
  per phase) but must be documented in the env-var contract and removed by the
  follow-up cleanup task once each path is proven in production.
