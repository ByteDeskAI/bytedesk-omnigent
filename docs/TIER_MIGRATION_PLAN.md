# Tier Migration Plan — physical kernel/core/extension hierarchy (BDP-2514)

**Status:** proposed (2026-06-25). **Epic:** BDP-2514 (physical three-tier package reorg).
**Branch:** `feature/BDP-2514-tier-reorg`.

This is the staged, ticketed program to turn the *logical* kernel/core/extension
classification (already shipped in `docs/EXTENSION_FRAMEWORK_ANALYSIS.md` §8–13,
epic BDP-2503) into a *physical* package hierarchy: a real `omnigent/kernel/`
package, a clearly bounded core, and out-of-tree extension packages — while the
system stays green at every commit.

## Cross-links

- [`docs/architecture/pluggable-core-and-hard-fork.md`](architecture/pluggable-core-and-hard-fork.md) — the hard-fork posture (BDP-2371). Omnigent is **disconnected from upstream**, so moving / renaming / deleting core files is explicitly allowed. This plan exercises that license; it does not need the upstream-rebase guardrails that are now void.
- [`docs/EXTENSION_FRAMEWORK_ANALYSIS.md`](EXTENSION_FRAMEWORK_ANALYSIS.md) §8 (kernel file inventory), §9 (core as first-party plugins), §10 (three-tier seam map), §11–12 (SDK Facade), §13 (recommended approach). This plan is the *physical-move* companion to that *logical-classification* document; the file lists below are taken verbatim from §8.1.
- **ADR-0153** — the tier-reorg ADR (the decision record for *physically* relocating the kernel and enforcing one-way tier imports in CI). This plan is its implementation track; the ADR records the "why physical, why now, why strangler-shim" decision and supersedes the file-granularity-only stance of §8.1.

## Why physical, when the logical classification already exists

§8–13 proved the kernel is *already nearly extractable*: the eight kernel files
import no domain types at module scope (verified by
`tests/pluggable/test_kernel_import_guard.py`, 15 passing today). But the
classification lives only in a doc and a guard test — the files still sit in
`omnigent/` next to domain code. The physical move buys three things the logical
classification cannot:

1. **A real import boundary you can see.** `from omnigent.kernel.X import ...`
   makes the tier visible at every call site, not just in a doc table.
2. **CI-enforceable one-way dependency.** A package boundary lets the import
   guard forbid *wrong-direction* imports (core → kernel only, never the
   reverse), which a flat namespace cannot express cleanly.
3. **Extractable extensions.** Domain integrations (jira/confluence/google) can
   leave the tree entirely once core no longer imports them by path.

---

## 1. End-state tree

```
omnigent/
  kernel/                         # TIER 1 — minimal boot-required, DOMAIN-FREE, import-safe
    __init__.py                   #          (no FastAPI on the runner hot path)
    extensions.py                 # OmnigentExtension Protocol, discover/install,
                                  #   the extension_*() aggregators
                                  #   (moved; shim at omnigent/extensions.py)
    pluggable/                    # PluggableRegistry, SEAMS, manifest, errors
      __init__.py                 #   (moved; shim package at omnigent/pluggable/)
      registry.py
      manifest.py
      errors.py
    lifespan_phases.py            # LifespanPhase ABC, LifespanOrchestrator,
                                  #   topological_order, LifespanContext,
                                  #   LifespanCycleError
                                  #   (moved; shim at omnigent/server/lifespan_phases.py)
    service_registry.py           # ServiceRegistry typed container
                                  #   (moved; shim at omnigent/server/service_registry.py)
    # app.py is NOT moved. Its create_app composition-root fragment imports FROM
    # omnigent.kernel.* — the file stays in CORE as a facade (see §2/facade).

  # TIER 2 — CORE ("core assembly": kernel + in-tree first-party plugins;
  #          stays under omnigent/, no physical move beyond the kernel extraction)
  config/  entities/  db/  stores/  identity/  coordination/  spec/
  runtime/  runtime/harnesses/  inner/  tools/  policies/   (minus google.py + github.py builtins)
  runner/  llms/  sandbox/  environments/  onboarding/  skills/  terminals/
  server/  server/routes/  server/container.py (facade)  server/app.py (facade)
  server/performance_metrics.py  runtime/memory_maintenance.py
  sdk/                            # public Facade over the kernel (already shipped:
                                  #   extension.py host.py contrib.py types.py di.py)

extensions/                       # TIER 3 — OPTIONAL, domain-bound (NOT needed to boot empty)
  bytedesk_omnigent/              # canonical first-party extension (already a separate
    tools/{jira_tools,confluence_tools,slack_tools,github_tools}.py    #   top-level package; no move)
    secrets/infisical.py
    harnesses/hermes_native_harness.py
  # extracted later from core via the policy_modules() hook:
  #   google-workspace-policy  <- omnigent/policies/builtins/google.py
  #   github-policy            <- omnigent/policies/builtins/github.py
```

### Movable now vs. facade-for-now (verified this run)

The kernel candidates split into two buckets. The first physically moves in
Stage 1; the second stays put and is reached *from* the kernel rather than
relocated. The verdicts below were each checked against the real code.

| File | Verdict | Evidence |
|---|---|---|
| `omnigent/pluggable/` (whole package) | **MOVABLE** → `omnigent/kernel/pluggable/` | 57 files reference `omnigent.pluggable`; `registry.py` defers the `omnigent.extensions` import (kept import-safe). Internal absolute imports rewrite `omnigent.pluggable.* → omnigent.kernel.pluggable.*`. |
| `omnigent/extensions.py` | **MOVABLE** → `omnigent/kernel/extensions.py` | 17 importers; domain-free at module scope, FastAPI only under `TYPE_CHECKING` (confirmed line 24 `if TYPE_CHECKING:` / line 29 `from fastapi import APIRouter, FastAPI`). Mutually decoupled from `pluggable` since `registry.py` defers the extensions import. |
| `omnigent/server/service_registry.py` | **MOVABLE** → `omnigent/kernel/service_registry.py` | **Zero** `omnigent` imports at module scope (confirmed: the only "omnigent" line is the docstring "deliberately holds no omnigent service imports"). 4 reference sites. |
| `omnigent/server/lifespan_phases.py` | **MOVABLE** → `omnigent/kernel/lifespan_phases.py` | `LifespanContext` fields are typed `Any` (confirmed lines 85–92: `agent_store: Any`, `runner_router: Any`, …); concrete phases defer domain imports inside `startup()`/`shutdown()`. 5 reference sites. The default concrete-phase set is core wiring that stays co-located behind the shim. |
| `omnigent/server/app.py` | **FACADE — cannot move** | Only the `create_app()` composition-root fragment is kernel; the file imports domain code and still owns non-core composition concerns (auth, hosts, caller-provided extra routers, SPA/static, extension installation). Already excluded from the guard test's `_KERNEL_PURE_FILES`. Stays CORE; imports orchestration primitives **from** `omnigent.kernel.*`. |
| `omnigent/server/container.py` | **FACADE — cannot move yet** | DI composition root by ROLE but NOT import-safe: imports six domain types at module scope (`RunnerRouter`, `RunnerControlRegistry`, `HostRegistry`/`RunnerExitReports`, `ManagedLaunchTracker`, `ServerMcpPool`, `ServerPerformanceMetrics`/`ServerMetricsOtelPublisher`) + the `dependency_injector` Cython C-ext. Today safe only because gated behind `OMNIGENT_USE_DI_CONTAINER` (default-OFF) and never on the runner hot path. Promotable to kernel only after deferring these imports into `build_core_container`/provider factories the way `lifespan_phases.py` does. Keep as CORE. |

---

## 2. Dependency / tier rules

**Import direction is strictly one-way:** `kernel ← core ← extensions` (arrows =
"is imported by"; **lower tiers never import higher tiers**).

### KERNEL (`omnigent/kernel/*`)

- **MAY** import: Python stdlib; third-party libs that are NOT domain frameworks
  at module scope; other `omnigent.kernel.*` modules.
- **MUST NOT** import any non-kernel `omnigent.*` module at **module scope**
  (no agents / tools / harnesses / stores / entities / config / routes / runner).
- FastAPI may appear only under `if TYPE_CHECKING:` (deferred via
  `from __future__ import annotations`) or inside function bodies. This is the
  invariant that keeps the FastAPI stack off the runner subprocess hot path.
- The two existing guard tests are the executable form of this rule and must
  stay green at every commit:
  - `tests/pluggable/test_kernel_import_guard.py` — AST module-scope purity +
    runtime `sys.modules`-delta cross-check over `_KERNEL_PURE_FILES`.
  - `tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi`
    — importing a runner-hot-path module must not pull FastAPI into `sys.modules`.

### CORE (`omnigent/` minus `omnigent/kernel/` and `omnigent/sdk/`)

- **MAY** import: kernel, other core, stdlib, third-party.
- **MUST NOT** import any Tier-3 extension package (`extensions.*`,
  `bytedesk_omnigent.*`) by path. Extensions reach core **only** through kernel
  seams (`PluggableRegistry`, `OmnigentExtension` hooks). A core module that
  needs a domain integration registers a seam and lets the extension fill it.
- The SDK (`omnigent/sdk/`) is the **public Facade** over the kernel. It depends
  on the kernel; nothing in the kernel depends on the SDK. SDK imports are
  semver-stable (§12.8); `omnigent.kernel.*` imports are semi-stable; everything
  under `omnigent/runtime/`, `omnigent/inner/`, `omnigent/server/routes/` is
  internal and may change without notice.

### EXTENSIONS (`extensions/*`, `bytedesk_omnigent`)

- **MAY** import: SDK, kernel, core, stdlib, third-party, other extensions
  (bound by domain).
- Register into core/kernel seams via the `OmnigentExtension` Protocol +
  entry-point discovery. They are **optional**: an empty system boots with zero
  extensions installed.

---

## 3. The staged sequence

Each stage is independently shippable and green-at-every-commit. The strangler
shims (§4) are what make this possible: a move and its call-site migration are
**different stages**, never the same commit.

### Stage 1 — Kernel package extraction (this run, BDP-2515)

Create `omnigent/kernel/` and physically relocate the four MOVABLE units, each
leaving a re-export shim at its old path:

1. `omnigent/pluggable/` → `omnigent/kernel/pluggable/` (+ shim **package**:
   `omnigent/pluggable/{__init__,registry,manifest,errors}.py` each re-export).
2. `omnigent/extensions.py` → `omnigent/kernel/extensions.py` (+ shim).
3. `omnigent/server/service_registry.py` → `omnigent/kernel/service_registry.py` (+ shim).
4. `omnigent/server/lifespan_phases.py` → `omnigent/kernel/lifespan_phases.py` (+ shim).

`app.py` and `container.py` are NOT moved — they stay CORE and import the
orchestration primitives **from** `omnigent.kernel.*` (facade posture).

**Lockstep guard-test updates** (a `from X import *` shim does NOT expose, e.g.,
`discover_extensions` as a *patchable module attribute*, so these are not
optional):

- `tests/pluggable/test_kernel_import_guard.py` — update `_KERNEL_PURE_FILES`
  (new `omnigent/kernel/...` paths) **and** `_ALLOWED_KERNEL_MODULES` (add
  `omnigent.kernel`, `omnigent.kernel.pluggable*`, `omnigent.kernel.extensions`,
  `omnigent.kernel.lifespan_phases`, `omnigent.kernel.service_registry`).
- `tests/pluggable/test_registry.py` — repoint the `monkeypatch.setattr` target
  from `omnigent.pluggable.registry` to `omnigent.kernel.pluggable.registry`
  (two sites: `test_discover_extensions_registers_contributed`,
  `test_discover_extensions_isolates_bad_extension`, …).
- Any other test that monkeypatches a *module attribute* on a moved module must
  repoint to the new canonical module (shims don't forward attribute patches).

**Exit criteria:** both guard tests green; `import omnigent.pluggable.X` and
`import omnigent.kernel.pluggable.X` resolve to the *same* class objects; the
FastAPI hot-path test stays green. If any single file can't be verified green,
revert *that file* to a shim/facade and report it (Rule 4) — do not block the
rest of the stage.

### Stage 2 — Migrate call sites off kernel shims, then delete the shims (BDP-2516)

Mechanical, low-risk, high-volume. For each of the 57 + 17 + 4 + 5 reference
sites, rewrite the import to the canonical `omnigent.kernel.*` path. Do this in
small batches (per-subpackage PRs) so review stays tractable and any regression
bisects cleanly. When a shim has zero remaining importers (verified by grep +
`pytest -k import`), delete it. Removing the last shim is the commit that proves
Stage 1's boundary is real.

### Stage 3 — Core consolidation: make the BDP-2503 first-party plugin path authoritative (BDP-2517)

Today the first-party seam path is authoritative: the `_plugin.py` files in 10
subpackages (`stores`, `identity`, `coordination`, `spec`, `policies`, `skills`,
`terminals`, `runtime/harnesses`, `server/routes`, `tools/builtins`) register
their seam contributions unconditionally through stable kernel registries. The
core route group is also authoritative through `RoutesExtension.post_init`: boot
exposes the already-built stores/cache/router dependencies on `app.state`, then
installs `firstparty_route_extensions()` through `install_extensions()` before
third-party extension routers so route precedence matches the former inline
mount order.

This stage has flipped the core seam + route path:

1. Keep seam parity tests green: first-party and third-party contributions must
   persist on the same stable `SEAMS` registries.
2. Keep route cutover tests green: the documented core route group must mount
   without any legacy first-party flag.
3. Keep the remaining direct `create_app()` route mounts scoped to
   composition-root concerns outside the core route group: peer tunnel, runner
   tunnel, hosts, accounts/auth, caller-provided `extra_routers`, and SPA/static.

### Stage 4 — Extract domain integrations into entry-point extension packages (BDP-2518)

With core no longer importing extensions by path (Stage 2/3), lift the
domain-bound integrations out of the tree into `extensions/` (or fully separate
distributions) registered via entry points:

- `google-workspace-policy` ← `omnigent/policies/builtins/google.py`
  (via the `policy_modules()` hook).
- `github-policy` ← `omnigent/policies/builtins/github.py` (same hook).
- The `bytedesk_omnigent` first-party extension is already a separate top-level
  package — no move; this stage confirms it registers purely through seams
  (routers, tool_factories, policy_modules, secret_backends, background_tasks,
  config_descriptors, principal_resolvers, assertion_verifiers,
  outbound_credential_providers, authorization_providers).

Each extraction keeps the seam green: register the same factory the inline
builtin used, behind the same hook name, so behavior is unchanged until the
deployer chooses not to install the package.

### Stage 5 — Enforce tier boundaries in CI (BDP-2519)

Extend `tests/pluggable/test_kernel_import_guard.py` (or a sibling
`test_tier_boundaries.py`) from "kernel files have no non-kernel module-scope
import" to the full one-way rule:

- **kernel** files import only kernel + stdlib + non-domain third-party (today's
  guard, repointed to `omnigent/kernel/*`).
- **core** files do not import `extensions.*` / `bytedesk_omnigent.*` at module
  scope.
- nothing imports the SDK *from* the kernel.

This is the executable form of §2 and the last stage — it turns the convention
into a forcing function so a wrong-direction import fails CI rather than rotting.

---

## 4. Strangler-shim mechanics

The strangler-fig pattern is what keeps the system green while a file moves. For
every relocated module:

1. **Move** the file to `omnigent/kernel/...`. Rewrite its *internal* absolute
   imports (`omnigent.pluggable.* → omnigent.kernel.pluggable.*`). Preserve the
   deferred-import discipline (e.g. `registry.py`'s deferred `omnigent.extensions`
   import) — that is what keeps the module import-safe.
2. **Shim** the old path. The shim re-exports the public surface:

   ```python
   # omnigent/pluggable/registry.py  (strangler shim — old import path)
   from omnigent.kernel.pluggable.registry import *  # noqa: F401,F403
   from omnigent.kernel.pluggable.registry import (   # explicit public re-exports
       PluggableRegistry,
       discover_extensions,
       # … every public name call sites rely on …
   )
   ```

   The `import *` covers `__all__`; the explicit list covers names that aren't in
   `__all__` but are imported by call sites. **Caveat:** `import *` does NOT make
   `discover_extensions` a *patchable module attribute* on the shim — tests that
   `monkeypatch.setattr(omnigent.pluggable.registry, "discover_extensions", ...)`
   must repoint to the canonical `omnigent.kernel.pluggable.registry` module
   (this is why the Stage 1 lockstep test edits are mandatory, not optional).
3. **Verify** both paths resolve to the *same* object: `assert
   omnigent.pluggable.registry.PluggableRegistry is
   omnigent.kernel.pluggable.registry.PluggableRegistry`. Same class identity =
   no parallel machinery (Rule 3).
4. **Migrate** call sites (Stage 2) and **delete** the shim (Stage 2 tail) — a
   *later* stage, never the move commit.

Shims are temporary by construction. A shim that outlives Stage 2 is a smell;
the §5 risk register tracks "shim rot" as a named risk.

---

## 5. Risk register

| Risk | Tier(s) | Mitigation |
|---|---|---|
| **FastAPI on the runner hot path.** A kernel module gains a module-scope FastAPI import (directly or transitively) and drags the FastAPI stack onto the runner subprocess. | kernel | Keep `tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi` + the runtime `sys.modules`-delta cross-check green at every commit. FastAPI only under `TYPE_CHECKING` or inside function bodies. This is the load-bearing invariant; a red here blocks the merge. |
| **Circular imports.** `extensions ↔ pluggable` (or kernel ↔ core) re-couple during the move. | kernel | Preserve the existing decoupling: `registry.py` already defers the `omnigent.extensions` import. Never add a module-scope cross-import between the two. The AST guard catches module-scope leaks; the `sys.modules`-delta guard catches transitive ones. |
| **Route double-mount.** Stage 3 runs the first-party plugin path *and* the inline `create_app()` wiring simultaneously, mounting the same routers twice. | core | Parity tests assert route-set *equality*, not superset. Retire inline blocks one subpackage at a time, each gated by the parity test, so a double-mount surfaces as a parity failure before merge. |
| **Flag sprawl (BDP-2511).** Multiple `OMNIGENT_USE_*` strangler flags are still live (`_LIFESPAN_PHASES`, `_SERVICE_REGISTRY`, `_DI_CONTAINER`, `_STORE_BOOTSTRAPPER`, `_HARNESS_PROVIDER_REGISTRY`, `_TOOL_DISPATCHER_REGISTRY`, `_TOOL_EXECUTION_CONTEXT`, `_SPEC_SOURCE`, `_COORDINATION_BACKPLANE`, `_AGENT_MEMORY`, `_MEMORY_EMBEDDER`, `_BACKOFF_POLICY`, `_SCHEMA_VALIDATOR`, `_REMOTE_FUNCTION`, + `OMNIGENT_DISABLED_EXTENSIONS`). Each flag is a dual-run path that can drift. | core | Treat each flag as a strangler with an explicit retirement: dual-run → parity → flip-default → delete-twin → remove-flag. Stage 3 retired the first-party-plugin route-shadow flag; track the rest under BDP-2511 so flags don't outlive their cutover. |
| **Shim rot.** Strangler shims outlive Stage 2 and become permanent indirection. | kernel/core | Stage 2 deletes each shim the moment its importer count hits zero (grep-verified). A CI check (Stage 5) can assert no `omnigent/pluggable/`, `omnigent/extensions.py`, `omnigent/server/{service_registry,lifespan_phases}.py` shim survives once Stage 2 closes. |
| **Patchable-attribute breakage.** A `from X import *` shim silently doesn't forward `monkeypatch.setattr` on module attributes, so a test that patches a moved module's attribute via the old path no-ops and goes green-but-wrong. | tests | Audit every `monkeypatch.setattr(<moved-module>, ...)` in Stage 1 and repoint to the canonical kernel module (already enumerated for `test_registry.py`). |
| **container.py premature promotion.** Someone moves `container.py` into the kernel before deferring its six domain imports. | kernel | Documented as facade-for-now. Promotion is gated on refactoring the six module-scope domain imports into `build_core_container`/provider factories (the `lifespan_phases.py` deferred-import pattern). Until then it stays CORE and is reached only behind `OMNIGENT_USE_DI_CONTAINER` (default-OFF). |

---

## 6. Proposed child-task breakdown (under BDP-2514)

| Ticket | Stage | Scope |
|---|---|---|
| **BDP-2515** | 1 | Create `omnigent/kernel/`; move `pluggable/`, `extensions.py`, `service_registry.py`, `lifespan_phases.py` with strangler shims; rewrite internal absolute imports; update `_KERNEL_PURE_FILES` + `_ALLOWED_KERNEL_MODULES` + `test_registry.py` monkeypatch targets in lockstep. Exit: both guard tests + FastAPI hot-path test green; old & new paths share class identity. |
| **BDP-2516** | 2 | Migrate the ~83 call sites off the kernel shims (per-subpackage batches); delete each shim at zero-importer. Exit: no kernel shim remains; full suite green. |
| **BDP-2517** | 3 | First-party seams are authoritative; the core route group is mounted by `RoutesExtension.post_init` through `install_extensions()`; the route-shadow flag is gone from boot. Exit: first-party seam and route paths are authoritative. |
| **BDP-2518** | 4 | Extract `google.py` → `google-workspace-policy` and `github.py` → `github-policy` entry-point extension packages via `policy_modules()`; confirm `bytedesk_omnigent` registers purely through seams. Exit: empty system boots with zero domain integrations installed. |
| **BDP-2519** | 5 | Extend the import guard to enforce the full one-way tier rule (kernel ← core ← extensions) in CI; add the no-surviving-shim assertion. Exit: a wrong-direction import fails CI. |
| **BDP-2511** | cross-cutting | Flag-sprawl cleanup: retire the `OMNIGENT_USE_*` strangler flags through dual-run → parity → flip → delete → remove-flag, coordinated with Stages 3–5. |

### Sequencing notes

- Stage 1 is the only stage that *moves* kernel files; Stages 2–5 are migration,
  consolidation, extraction, and enforcement on top of that move.
- Stage 2 must fully close (all shims deleted) before Stage 5's no-surviving-shim
  assertion can land.
- Stage 3 and Stage 4 both reduce the core/domain-import surface; run Stage 3
  first (make the plugin path authoritative) so Stage 4's extraction has a clean
  seam to register into.
- `container.py` kernel promotion is explicitly **out of scope** for BDP-2514
  until its six domain imports are deferred; track separately if/when the DI
  container becomes default-ON.

---

## 7. Verification posture (Rule 4)

Every move is verified with real commands against the real tree:

- `tests/pluggable/test_kernel_import_guard.py` — green today (15 passed, 2026-06-25).
- `tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi` — green.
- Class-identity assertion (old path `is` new path) per moved module.

Use `.venv/bin/python` for all pytest (it has `sqlalchemy` + `mcp`;
`/usr/bin/python3` does not). If a move cannot be verified green, revert that
file to a shim/facade and report it rather than landing a red — never fabricate
a green.
