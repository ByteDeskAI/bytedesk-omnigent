# Tier Architecture — the physical kernel / core / extension hierarchy (BDP-2514)

**Status:** partially realized (2026-06-25). **Epic:** BDP-2514 (physical three-tier
package reorg). **Branch:** `feature/BDP-2514-tier-reorg`.

This document describes the **physical** three-tier package hierarchy as it exists
*after* the Stage-1 kernel extraction. It is the realized companion to the *logical*
classification in [`docs/EXTENSION_FRAMEWORK_ANALYSIS.md`](EXTENSION_FRAMEWORK_ANALYSIS.md)
§8–13 (epic BDP-2503): that document proved the kernel was *nearly extractable*; this
run made the kernel a real `omnigent/kernel/` package you can import and see. The
remaining stages (call-site migration, shim deletion, core consolidation, extension
extraction, CI enforcement) are tracked in
[`docs/TIER_MIGRATION_PLAN.md`](TIER_MIGRATION_PLAN.md).

Grounding note: every structural claim below was read off the actual tree on this
branch and verified with real commands (see [§7](#7-verification--what-is-actually-green)),
per the hard-fork posture in
[`docs/architecture/pluggable-core-and-hard-fork.md`](architecture/pluggable-core-and-hard-fork.md)
(BDP-2371) — omnigent is disconnected from upstream, so moving / renaming / deleting
core files is explicitly allowed, which is what licensed the physical kernel move.

---

## 1. The three tiers, at a glance

```
kernel  ←  core  ←  extensions          (arrows = "is imported by";
                                          lower tiers never import higher tiers)
```

| Tier | What it is | Where it lives (post-run) |
|---|---|---|
| **KERNEL** (Tier 1) | The minimal boot-required microkernel that brings up an **empty** system and hosts plugins. DOMAIN-FREE, import-safe. | `omnigent/kernel/` (a real package — see §2) |
| **CORE** (Tier 2) | The out-of-the-box omnigent functionality — stores, identity, coordination, harnesses, tools, policies, spec, routes, runner, runtime, llms, sandbox, onboarding, skills, terminals, server. Built on the kernel as first-party plugins. | `omnigent/` minus `omnigent/kernel/` and `omnigent/sdk/`; public Facade at `omnigent/sdk/` (see §3) |
| **EXTENSIONS** (Tier 3) | OPTIONAL, domain-bound integrations NOT needed to boot empty — Jira/Confluence/Slack/GitHub/Google-Workspace/Atlassian and the first-party `bytedesk_omnigent` extension. | `bytedesk_omnigent/` (top-level package, entry-point discovered); future out-of-tree `extensions/*` packages (see §4) |

---

## 2. KERNEL — `omnigent/kernel/` (Tier 1)

The kernel is the MINIMAL set required to boot an empty system: the extension
contract + discovery/install, the pluggable-seam machinery, the lifecycle
orchestrator, and the typed service registry. It imports **no** agents / tools /
harnesses / stores / entities / routes at module scope.

### 2.1 What physically MOVED this run (the kernel package that now exists)

The post-run tree under `omnigent/kernel/` is:

```
omnigent/kernel/
  __init__.py            # lazy public surface (PEP 562 __getattr__); bare import is cheap
  extensions.py          # OmnigentExtension Protocol, discover/install,
                         #   the extension_*() aggregators
  pluggable/
    __init__.py          # PluggableRegistry + error taxonomy re-export
    registry.py          # PluggableRegistry[T], OMNIGENT_USE_<SEAM> override,
                         #   discover_extensions(hook=...)
    manifest.py          # SEAMS, discover_all_extensions(), capability_manifest()
    errors.py            # ProviderError + ProviderNotRegistered / ProviderUnconfigured /
                         #   ProviderUnavailable / RegistryConflict
  lifespan_phases.py     # LifespanPhase ABC, LifespanOrchestrator, topological_order,
                         #   LifespanContext, LifespanCycleError + the default
                         #   concrete-phase set + build_default_lifespan_phases()
  service_registry.py    # ServiceRegistry typed {type: instance} container
```

Each moved unit left a **strangler re-export shim** at its old path so existing
imports keep working unchanged (call-site migration + shim deletion is BDP-2516,
a later stage — not this run):

| Old path (now a shim) | Canonical kernel path | Shim re-exports |
|---|---|---|
| `omnigent/pluggable/__init__.py` | `omnigent/kernel/pluggable/__init__.py` | `PluggableRegistry`, `ProviderError` + 4 subclasses |
| `omnigent/pluggable/registry.py` | `omnigent/kernel/pluggable/registry.py` | `PluggableRegistry`, `discover_extensions`, `_override_env_name` |
| `omnigent/pluggable/manifest.py` | `omnigent/kernel/pluggable/manifest.py` | `SEAMS`, `discover_all_extensions`, `capability_manifest` |
| `omnigent/pluggable/errors.py` | `omnigent/kernel/pluggable/errors.py` | `ProviderError` + 4 subclasses |
| `omnigent/extensions.py` | `omnigent/kernel/extensions.py` | `OmnigentExtension`, `discover_extensions`, `install_extensions`, the `extension_*()` aggregators |
| `omnigent/server/service_registry.py` | `omnigent/kernel/service_registry.py` | `ServiceRegistry` |
| `omnigent/server/lifespan_phases.py` | `omnigent/kernel/lifespan_phases.py` | `LifespanPhase`, `LifespanOrchestrator`, `LifespanContext`, `LifespanCycleError`, `topological_order`, `build_default_lifespan_phases`, and the 19 concrete `*Phase` classes |

Each shim is `from omnigent.kernel.<mod> import *` plus an explicit public re-export
list. Both paths resolve to the **same class objects** — there is no parallel
machinery (Rule 3; identity verified in §7).

> **Patch caveat (load-bearing for tests).** A `from X import *` shim does NOT make
> e.g. `discover_extensions` a *patchable module attribute* on the shim. Tests that
> `monkeypatch.setattr(<moved-module>, ...)` must target the canonical
> `omnigent.kernel.*` module. This was applied in lockstep to
> `tests/pluggable/test_registry.py` and the kernel guard test's `_KERNEL_PURE_FILES`
> / `_ALLOWED_KERNEL_MODULES` sets this run.

### 2.2 What stayed put — FACADE, reached *from* the kernel (not moved)

Two composition-root files are kernel *by role* but cannot move yet, so they stay in
CORE and import the orchestration primitives **from** `omnigent.kernel.*`:

| File | Why it stays CORE (facade) |
|---|---|
| `omnigent/server/app.py` | Only the `create_app()` composition-root fragment is kernel-shaped; the file imports domain code and still owns non-core composition concerns (auth, peer/runner tunnel, hosts, caller-provided extra routers, SPA/static). Already excluded from the guard test's pure-kernel set. |
| `omnigent/server/container.py` | DI composition root by ROLE but NOT import-safe: imports six domain types at module scope + the `dependency_injector` Cython C-ext. Promotable only after those imports are deferred into `build_core_container` / provider factories (the `lifespan_phases.py` pattern). Until then it stays CORE behind `OMNIGENT_USE_DI_CONTAINER` (default-OFF). |

### 2.3 Kernel invariants (the executable rules)

- **Domain-free at module scope.** No kernel module imports a non-kernel `omnigent.*`
  module at module scope. FastAPI may appear only under `if TYPE_CHECKING:` or inside
  function bodies — the invariant that keeps the FastAPI stack off the runner
  subprocess hot path.
- **Cheap bare import.** `omnigent/kernel/__init__.py` does NOT eagerly import its
  submodules; the public surface is resolved lazily via PEP 562 `__getattr__`. A bare
  `import omnigent.kernel` pulls in neither FastAPI nor the submodules (verified §7).
- **Two guard tests are the rule's executable form** and must stay green at every commit:
  - `tests/pluggable/test_kernel_import_guard.py` — AST module-scope purity + a runtime
    `sys.modules`-delta cross-check over the kernel files.
  - `tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi`
    — importing a runner-hot-path module must not pull FastAPI into `sys.modules`.

---

## 3. CORE — `omnigent/` (Tier 2) and how first-party plugins build on the kernel

CORE = KERNEL + the in-tree first-party functionality. Aside from the kernel
extraction in §2, core did **not** physically move this run — it remains the set of
`omnigent/` subpackages, now importing orchestration primitives from `omnigent.kernel.*`
(today via the back-compat shims; the canonical kernel imports land in BDP-2516).

### 3.1 The core surface (actual `omnigent/` subpackages on this branch)

```
config/  entities/  db/  stores/  identity/  coordination/  spec/
runtime/  runtime/harnesses/  inner/  tools/  policies/  runner/  llms/
sandbox/  environments/  onboarding/  skills/  terminals/  resources/
accountability/  host/  client_tools/  repl/  tool_steps/  core/
server/  server/routes/   (+ server/app.py + server/container.py facades)
```

Top-level core modules sit alongside (the native-harness bridges, CLI entry, model
catalog, session lifecycle, etc.). The `omnigent/extensions.py` and
`omnigent/pluggable/` entries you still see at the top level are the **strangler
shims** from §2.1, not core logic.

### 3.2 How first-party plugins build on the kernel (the "core as plugins" model)

Core functionality registers into kernel seams using the *same*
`OmnigentExtension` / `PluggableRegistry` contract a third party would use
(EXTENSION_FRAMEWORK_ANALYSIS §9, "the dogfooding argument" — a framework that
special-cases its own features has an incomplete plugin seam). The seam machinery is
the kernel's `PluggableRegistry` and the `OmnigentExtension` hooks; core packages
register their defaults into those seams. Examples present in the tree today:
`coordination_backplane`, `assertion_verifier` / `outbound_credential` / `authorizer`,
`artifact_store`, `agent_memory` / `memory_embedder`, `spec_source`, the harness
descriptor registry, and the tool / policy registries.

First-party seam registration is now authoritative at server startup: the core
`_plugin.py` files register their seam contributions unconditionally through
the same stable `SEAMS` registries that third-party extensions use. The
documented core route group is also authoritative through `RoutesExtension`:
`create_app()` exposes the already-built route dependencies on `app.state`, then
installs `firstparty_route_extensions()` through the same extension lifecycle
used by third-party routers. The first-party plugin files present today are:

```
omnigent/stores/_plugin.py            omnigent/identity/_plugin.py
omnigent/coordination/_plugin.py      omnigent/spec/_plugin.py
omnigent/policies/_plugin.py          omnigent/skills/_plugin.py
omnigent/terminals/_plugin.py         omnigent/runtime/harnesses/_plugin.py
omnigent/server/routes/_plugin.py     omnigent/tools/builtins/_plugin.py
```

The route plugin intentionally owns only the core `omnigent/server/routes/`
group. `create_app()` still mounts composition-root route surfaces directly
when they are not part of that group: peer tunnel, runner tunnel, hosts,
accounts/auth, caller-provided `extra_routers`, and the SPA/static mount.

### 3.3 The SDK Facade — `omnigent/sdk/` (public surface over the kernel)

`omnigent/sdk/` is the public **Facade** over the kernel: it depends on the kernel,
and nothing in the kernel depends on the SDK. The package on this branch is:

```
omnigent/sdk/__init__.py   omnigent/sdk/extension.py   omnigent/sdk/host.py
omnigent/sdk/contrib.py    omnigent/sdk/types.py       omnigent/sdk/di.py
```

It compiles down to the same `OmnigentExtension` Protocol and `PluggableRegistry`
calls the kernel already dispatches (EXTENSION_FRAMEWORK_ANALYSIS §11–12). SDK imports
are semver-stable; `omnigent.kernel.*` imports are semi-stable; everything under
`omnigent/runtime/`, `omnigent/inner/`, `omnigent/server/routes/` is internal and may
change without notice.

---

## 4. EXTENSIONS — Tier 3 (optional, domain-bound)

Extensions are OPTIONAL and domain-bound: an empty system boots with **zero**
extensions installed. They register into core/kernel seams via the
`OmnigentExtension` Protocol + entry-point discovery (group `omnigent.extensions`).

### 4.1 The first-party `bytedesk_omnigent` extension (already a separate package)

`bytedesk_omnigent/` is already a top-level package — **no move was needed**. It is
discovered via the entry point declared in `pyproject.toml`:

```toml
[project.entry-points."omnigent.extensions"]
bytedesk = "bytedesk_omnigent.extension:BytedeskExtension"
```

`BytedeskExtension` registers into essentially every seam — confirmed by the hook
methods present in `bytedesk_omnigent/extension.py`: `routers`, `default_mcp_servers`,
`policy_modules`, `tool_factories`, `secret_backends`, `principal_resolvers`,
`harness_descriptors`, `assertion_verifiers`, `outbound_credential_providers`,
`authorization_providers`, `tool_interceptors`, `config_descriptors`,
`background_tasks`. Its domain-bound pieces present in the tree include:

```
bytedesk_omnigent/tools/{jira_tools,confluence_tools,slack_tools,github_tools}.py
bytedesk_omnigent/secrets/infisical.py
bytedesk_omnigent/harnesses/hermes_native_harness.py  (+ hermes_native_executor.py)
```

(plus other domain tool modules: deliberation, goal, outcome, peer, routing, signal).

### 4.2 Extraction candidates (the domain-bound surface to lift out of core later)

These are the named extraction candidates and where each currently lives. Stage 4
(BDP-2518) lifts the core-resident ones out via the relevant seam hook; the
`bytedesk_omnigent`-resident ones are already in the extension package and Stage 4
just confirms they register purely through seams:

| Candidate | Current location (this branch) | Extraction path |
|---|---|---|
| `google-workspace-policy` | `omnigent/policies/builtins/google.py` (CORE) | extract to an entry-point package via the `policy_modules()` hook |
| `github-policy` | `omnigent/policies/builtins/github.py` (CORE) | extract to an entry-point package via the `policy_modules()` hook |
| `bytedesk_omnigent` | `bytedesk_omnigent/` (already a top-level extension) | already separate; confirm seam-only registration |
| `jira-tools` | `bytedesk_omnigent/tools/jira_tools.py` | already in the extension (via `tool_factories()`) |
| `confluence-tools` | `bytedesk_omnigent/tools/confluence_tools.py` | already in the extension (via `tool_factories()`) |
| `slack-tools` | `bytedesk_omnigent/tools/slack_tools.py` | already in the extension (via `tool_factories()`) |
| `github-tools` | `bytedesk_omnigent/tools/github_tools.py` | already in the extension (via `tool_factories()`) |
| `infisical-secrets` | `bytedesk_omnigent/secrets/infisical.py` | already in the extension (via `secret_backends()`) |
| `hermes-harness` | `bytedesk_omnigent/harnesses/hermes_native_harness.py` | already in the extension (via `harness_descriptors()`) |

> Note: there is **no** top-level `extensions/` directory on this branch yet — the
> end-state `extensions/*` package layout sketched in the migration plan is a Stage-4
> target. Today the only extension package is the top-level `bytedesk_omnigent/`.

---

## 5. Tier dependency rules (kernel ← core ← extensions)

**Import direction is strictly one-way:** `kernel ← core ← extensions` (arrows =
"is imported by"; **lower tiers never import higher tiers**).

### KERNEL (`omnigent/kernel/*`)

- **MAY** import: Python stdlib; third-party libs that are NOT domain frameworks at
  module scope; other `omnigent.kernel.*` modules.
- **MUST NOT** import any non-kernel `omnigent.*` module at module scope (no
  agents / tools / harnesses / stores / entities / config / routes / runner).
- FastAPI only under `if TYPE_CHECKING:` or inside function bodies. This is the
  invariant that keeps the FastAPI stack off the runner subprocess hot path.

### CORE (`omnigent/` minus `omnigent/kernel/` and `omnigent/sdk/`)

- **MAY** import: kernel, other core, stdlib, third-party.
- **MUST NOT** import any Tier-3 extension package (`extensions.*`,
  `bytedesk_omnigent.*`) by path. Extensions reach core **only** through kernel seams
  (`PluggableRegistry`, `OmnigentExtension` hooks). A core module that needs a domain
  integration registers a seam and lets the extension fill it.
- The SDK (`omnigent/sdk/`) is the public Facade over the kernel: it depends on the
  kernel; nothing in the kernel depends on the SDK.

### EXTENSIONS (`bytedesk_omnigent`, future `extensions/*`)

- **MAY** import: SDK, kernel, core, stdlib, third-party, **and other extensions —
  bound by domain**. "Extensions build on extensions, bound by domain": a domain
  integration may depend on another extension within the same domain (e.g. a Jira
  tool extension building on a shared Atlassian extension), but it does not reach
  sideways into unrelated domains, and core never reaches *up* into any of them.
- Register into core/kernel seams via the `OmnigentExtension` Protocol + entry-point
  discovery. They are **optional**: an empty system boots with zero extensions
  installed.

CI enforcement of the full one-way rule (a wrong-direction import fails CI) is Stage 5
(BDP-2519). Today the kernel half of the rule is already enforced by the two guard
tests in §2.3.

---

## 6. What is realized vs. what remains

This run realized **Stage 1** only: the physical `omnigent/kernel/` package + strangler
shims + lockstep guard-test updates. Everything else is staged in
[`docs/TIER_MIGRATION_PLAN.md`](TIER_MIGRATION_PLAN.md):

| Stage | Ticket | What it does | State |
|---|---|---|---|
| 1 — kernel extraction | BDP-2515 | Create `omnigent/kernel/`; move pluggable / extensions / service_registry / lifespan_phases with shims | **realized this run** |
| 2 — migrate call sites, delete shims | BDP-2516 | Rewrite the ~83 importers to the canonical kernel path; delete each shim at zero-importer | pending |
| 3 — core consolidation | BDP-2517 | First-party seams authoritative; core route group mounted by `RoutesExtension`; legacy route-shadow flag removed from boot | **realized this run** |
| 4 — extract domain integrations | BDP-2518 | Lift `google.py` / `github.py` to entry-point packages; confirm `bytedesk_omnigent` registers purely through seams | pending |
| 5 — enforce tier boundaries in CI | BDP-2519 | Extend the import guard to the full one-way rule + no-surviving-shim assertion | pending |

The ADR for the *physical* relocation decision (referenced as **ADR-0153** in the
migration plan) is not yet written to `docs/adr/`; the migration plan is the live
record of the decision until that ADR lands.

---

## 7. Verification — what is actually green

Run with `.venv/bin/python` (it has `sqlalchemy` + `mcp`; `/usr/bin/python3` does not).
Real commands, real output, this run (2026-06-25):

- **Kernel guard + FastAPI hot-path** —
  `tests/pluggable/test_kernel_import_guard.py` +
  `tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi`:
  **15 passed**.
- **Pluggable / extensions / SDK suites** — `tests/pluggable/ tests/extensions/
  tests/sdk/`: **169 passed, 1 failed**. The single failure
  (`tests/extensions/test_abstraction_spine_contract.py::test_dispatch_elif_chain_branch_counts_are_pinned`,
  asserting the tool-dispatch set-family elif count = 16 while the code has 17) is
  **pre-existing and unrelated** to the tier reorg: it fails identically on the HEAD
  baseline (verified by re-running the un-modified test against the same `tool_dispatch.py`,
  which is not touched by this branch). It is a tool-family-count drift to fix
  separately, not a tier-reorg regression.
- **Server / runner sites that touch the moved modules** —
  `tests/server/integration/test_app.py tests/server/test_principal_port.py
  tests/runner/test_identity.py`: **46 passed**.
- **Class identity (old path `is` new path), per moved module** — all pass:
  `omnigent.pluggable.registry.PluggableRegistry is
  omnigent.kernel.pluggable.registry.PluggableRegistry`; the same `is` check holds for
  `ProviderError`, `OmnigentExtension`, `install_extensions`, `ServiceRegistry`,
  `LifespanOrchestrator`, `build_default_lifespan_phases`, and `SEAMS`. No parallel
  machinery (Rule 3).
- **Kernel import is cheap** — a bare `import omnigent.kernel` pulls neither FastAPI
  nor Starlette into `sys.modules`, and resolving `PluggableRegistry` /
  `OmnigentExtension` / `LifespanOrchestrator` / `ServiceRegistry` lazily through the
  package still pulls neither. This is the load-bearing runner-hot-path invariant.

---

## 8. Reference docs

- [`docs/TIER_MIGRATION_PLAN.md`](TIER_MIGRATION_PLAN.md) — the staged, ticketed program
  for the remaining stages (the source of truth for sequencing, strangler-shim
  mechanics, and the risk register).
- [`docs/EXTENSION_FRAMEWORK_ANALYSIS.md`](EXTENSION_FRAMEWORK_ANALYSIS.md) §8–13 — the
  logical kernel/core/extension classification + the SDK Facade design this physical
  layout realizes.
- [`docs/architecture/pluggable-core-and-hard-fork.md`](architecture/pluggable-core-and-hard-fork.md)
  — the hard-fork posture (BDP-2371) that licenses moving / renaming / deleting core
  files and the pluggable-core recipe the kernel seams follow.
