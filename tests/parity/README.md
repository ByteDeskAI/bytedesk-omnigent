# Parity harness (Phase 0 — BDP-2326)

Characterization + parity infrastructure for the omnigent abstraction
epic (BDP-2323, ADR-0145 §parity). This is the **safety net** that lets
us introduce an abstraction seam under existing subsystems and *prove*
the refactor changed nothing observable.

> **Scope of this phase.** This is the **scaffold + plan**, not the full
> golden capture. The characterization skeletons here assert each
> subsystem's *contract* and mark — with explicit `TODO(golden)` markers
> — where a captured baseline still needs to be wired in. Capturing the
> full golden baseline (the ~1045-case live suite) requires running the
> real, credentialed suite and is deliberately out of scope for Phase 0.

## The three pillars

### 1. Golden baseline

A **golden baseline** is the recorded behaviour of each subsystem *before*
the abstraction seam exists — the legacy path is, by definition, correct.
For black-box subsystems (stores, policies, scheduler, idempotency,
harness aliasing, spawn-tree shape) the baseline is captured as the
serialized contract output of a known input:

- **Where it lives.** `tests/parity/_golden/<subsystem>.json` — JSON in a
  flat file, mirroring the dual-DB rule (variable payloads ride in Text
  columns holding JSON, never native JSONB). One golden file per
  subsystem keeps capture and review reviewable in a diff.
- **How it is captured.** Run the characterization test with the
  capture env set: `OMNIGENT_PARITY_CAPTURE=1`. With capture on, the
  test writes the observed contract output to its golden file instead of
  asserting against it. Without it (the default, and always in CI), the
  test loads the golden file and asserts equality. This is the
  record/replay pattern — capture once on the legacy path, replay
  forever.
- **Determinism.** Golden capture pins everything non-deterministic:
  generated ids (`conv_…`, `ag_…`, `pol_…`) are normalized to stable
  placeholders, timestamps are normalized to `0`, and the per-test
  SQLite DB (`db_uri` fixture) is recreated fresh so migrations run the
  same path as production. A golden file must round-trip through
  `git diff` with zero noise between two capture runs.

The skeletons in this phase mark the exact assertion sites with
`TODO(golden): capture <subsystem> baseline` so the capture wiring lands
as a focused follow-up, not a rewrite.

### 2. Dual-path execution

Every subsystem under abstraction grows a **named feature flag** that
selects the legacy path (OFF) or the new abstraction path (ON). The
flag is a plain boolean env var (default OFF), read at the seam:

- Canonical flag for this epic: `OMNIGENT_ABSTRACTION_SEAM`.
- OFF (unset / not `"1"`): the existing, shipped code path. Zero
  behaviour change for production until the flag flips.
- ON (`"1"`): the new abstraction (Strategy/Adapter seam, ADR-0008).

The dual-path **executor** is [`scripts/test_parity.sh`](../../scripts/test_parity.sh):
it runs a chosen test slice with the flag OFF, then ON, then diffs the
two JUnit reports per `nodeid`. Identical outcomes ⇒ exit 0; any
divergence ⇒ non-zero exit with the offending tests in the diff. The
diff is *outcome-based* (pass/fail/skip per test), so it tolerates
timing noise and is order-independent.

```bash
# Default: parity the characterization skeletons under the epic flag.
scripts/test_parity.sh

# Parity a specific subsystem under a specific flag.
scripts/test_parity.sh OMNIGENT_ABSTRACTION_SEAM tests/stores
```

### 3. Test matrix

The parity test matrix is the cross-product of **{subsystem} ×
{flag OFF, flag ON} × {SQLite, Postgres}**:

| Subsystem               | Contract characterized                                   | Golden file                       |
| ----------------------- | -------------------------------------------------------- | --------------------------------- |
| Harness dispatch        | alias → canonical id; native-vs-SDK classification       | `_golden/harness_dispatch.json`   |
| Store CRUD round-trip   | create → get round-trips entity; missing id → `None`     | `_golden/store_crud.json`         |
| Policy apply            | persisted policy fields; governor ALLOW→DENY at limit    | `_golden/policy_apply.json`       |
| Spawn-tree shape        | `root_conversation_id` / `parent_conversation_id` graph  | `_golden/spawn_tree.json`         |
| Durable-task lifecycle  | cron register→claim-once; idempotency at-most-once claim | `_golden/durable_task.json`       |

Each row also carries an **error-path** characterization (duplicate
claim loses, duplicate policy name raises `IntegrityError`, unknown
harness passes through unchanged, missing id returns `None`) — the
abstraction must preserve failure modes, not just happy paths.

**Dual-DB axis.** The default `db_uri` fixture is SQLite. The same
characterization tests run against Postgres in the integration lane
(`tests/integration`, opt-in via `--integration`) so the JSON-in-Text /
soft-FK behaviour is parity-checked on both backends. SQLite is the fast
inner-loop lane; Postgres is the merge-gate lane.

## CI budget

The parity lane is a **merge gate**, so it must stay cheap:

- **Inner loop (every PR, SQLite only):** the `tests/parity` skeletons
  are pure-CPU contract assertions against per-test SQLite DBs — target
  **< 60s wall** on the default 8-worker shard. No real LLM calls, no
  network (`OMNIGENT_DISABLE_CATALOG_LOOKUP=1` is pinned by the root
  conftest), no credentials.
- **Dual-path run (`scripts/test_parity.sh`):** runs the slice twice, so
  budget it at **≈ 2×** the single-run cost of whatever paths are passed.
  Keep the default slice (`tests/parity`) small enough that the doubled
  run stays under the parity inner-loop budget.
- **Postgres lane:** lives behind `--integration` and is **not** on the
  per-PR critical path; it runs on the integration matrix where a live
  Postgres is already provisioned. Reuse that container — do not stand a
  new one up per parity test.
- **Golden capture is off the critical path.** `OMNIGENT_PARITY_CAPTURE`
  is never set in CI; capture is a developer/maintainer action recorded
  into the golden files and reviewed in the PR diff.

## Files in this directory

- `test_characterization_skeletons.py` — black-box characterization test
  skeletons, one class per subsystem, each asserting a contract with
  `TODO(golden)` markers where the captured baseline plugs in. Includes
  error-path assertions.
- `test_parity_harness.py` — focused unit tests for the parity
  infrastructure itself (golden capture/replay helper + flag reader),
  so the safety net is itself tested.
- `_golden/` — captured golden baselines (created on first capture run;
  absent until then).
