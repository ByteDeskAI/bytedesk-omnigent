# Fork / Upstream Rebase Strategy — BDP-2323 Abstraction Roadmap

**Status:** Active · **Owner:** ByteDesk Platform · **Authority:** ADR-0145 (bytedesk-platform `docs/architecture/adr/`), BDP-2323 (Phase −1, BDP-2325).

`bytedesk-omnigent` (`ByteDeskAI/bytedesk-omnigent`, remote `origin`) is an
actively-tracked fork of `omnigent-ai/omnigent` (remote `upstream`). The
upstream is alpha and rebased frequently. Every line we change in an
upstream-shared file is a future rebase conflict, so the BDP-2323 abstraction
roadmap is sequenced to keep the fork delta small, additive, and easy to replay
on top of a fresh upstream.

This document is the **per-phase landing policy** for that roadmap. It answers,
for each later phase: is the change an *upstream-shared-file edit* or
*`bytedesk_omnigent`-only*, what are the two mechanical rules that govern any
shared-file touch, where the rebase sync points are, and the order in which
core-touching phases must land relative to an upstream rebase.

It does not restate the per-phase *design* (that lives in the phase's own
goal/ADR); it constrains *where the code lands and in what order*.

---

## The two boundaries

| Boundary | Location | Rebase cost |
| --- | --- | --- |
| **`bytedesk_omnigent`-only** | new files under `bytedesk_omnigent/` (sibling of `omnigent/`, never nested) | zero — upstream never touches this tree |
| **Upstream-shared edit** | any change to a file under `omnigent/`, `ap-web/`, `tests/`, `pyproject.toml`, `setup.py`, or any other upstream-tracked path | one conflict per changed hunk on every rebase |

The default for **every** phase is `bytedesk_omnigent`-only. A phase is allowed
an upstream-shared edit only when there is no seam to reach the behavior from
the extension package — and then only under the two rules below.

New functionality reaches the core through the generic
`omnigent.extensions` entry-point seam (ADR-0143): `BytedeskExtension` mounts
routers under `/v1`, and later phases compose the moved feature submodules onto
it. Reaching for the seam is always preferred over editing a core call site.

---

## Rule 1 — minimize changed lines (shared files)

When a phase genuinely must touch an upstream-shared file:

- Change the **fewest possible lines**. A single appended registration line, one
  new field, one accessor, or one lifespan task — never a reflow, rename, or
  reformat of surrounding code.
- **Never reorder, re-indent, or reformat** code you are not functionally
  changing. `ruff format` drift on untouched lines is a conflict generator;
  leave the upstream formatting exactly as-is even where it differs from how we
  would write it.
- Keep the edit **mechanically replayable**: a hunk that adds N contiguous new
  lines at a stable anchor (end of a list, end of a class body, end of a
  registry dict) replays cleanly; a hunk interleaved into existing logic does
  not.
- Prefer a **pluggable seam** (a `Protocol`/Strategy the core already calls)
  over a call-site edit, so the backend swaps without the core knowing.

The measure is literal: a phase's upstream-shared diff should be readable as
"appended X" lines, not "rewrote Y region".

## Rule 2 — additive-append only (shared files)

Every sanctioned shared-file change is **additive at a stable anchor**:

- **Append**, do not insert mid-body and do not delete. Add a new class at the
  end of a module, a new field at the **end** of a spec's field list, a new
  entry at the **end** of a registry mapping, one new accessor method on a
  class, or one new lifespan/startup task at the end of the startup sequence.
- **Do not remove or replace** upstream code. If upstream behavior must change,
  override it from `bytedesk_omnigent` (subclass, wrap, or register a strategy)
  rather than deleting the upstream line.
- A shared append carries a `# bytedesk(<phase>):` marker comment so the fork
  delta is greppable and a future rebase can locate every intentional add.
- Follow the dual-DB and store conventions even in shared edits: variable
  payloads are **JSON-in-`Text`** columns (never native `JSONB`), extension
  tables use **soft FKs** (plain columns, no cross-tree foreign-key
  constraints), and any new store mirrors the **ABC + `SqlAlchemy*Store` impl +
  `sql_X_to_entity` converter** triad.

These two rules together are what make a shared edit a one-conflict-and-done
replay instead of a merge rewrite.

---

## Per-phase landing policy

Phase numbers track the BDP-2323 roadmap (ADR-0145). Each phase declares its
boundary up front; the *only* phases permitted an upstream-shared edit are the
ones marked **shared** — and even those land under Rules 1 and 2.

| Phase | Boundary | Lands as | Notes |
| --- | --- | --- | --- |
| **−1 — Rebase strategy doc** (BDP-2325) | `bytedesk_omnigent`-only (doc) | `docs/architecture/fork-rebase-strategy.md` + test | This document. No core touch. |
| **0 — Extension seam baseline** (ADR-0143) | already landed | `bytedesk_omnigent/extension.py` + `omnigent.extensions` seam | The one sanctioned shared seam; downstream phases ride it instead of editing core. |
| **1 — Stores & entities abstraction** | `bytedesk_omnigent`-only | new `*Store` ABC + `SqlAlchemy*Store` + `sql_X_to_entity` under `bytedesk_omnigent/` | JSON-in-`Text`, soft FKs. No `omnigent/db` edit; tables register via the seam. |
| **2 — Tools & tool-steps** | `bytedesk_omnigent`-only | new tool modules under `bytedesk_omnigent/tools/`, registered through the extension | Native plugin tools, not MCP. No core tool-registry edit. |
| **3 — Bus / ingress / signals** | `bytedesk_omnigent`-only | new modules under `bytedesk_omnigent/bus`, `ingress` | Runtime state, not upstream schema. |
| **4 — Governance / policies / deliberation** | `bytedesk_omnigent`-only (+ register) | new policy modules; registered via the seam's policy-module hook | Policy handlers register by module path — no `omnigent/policies` edit. |
| **5 — Scheduler / release / outcomes** | `bytedesk_omnigent`-only | new modules under `bytedesk_omnigent/scheduler`, `release`, `outcomes` | Lifespan tasks attach through the extension, not `omnigent/server` startup. |
| **6 — Spec field additions** | **shared (append-only)** | one new optional field at the **end** of the upstream `AgentSpec` field list | Only when the seam genuinely cannot carry it. Optional + defaulted so unset bundles are unchanged. |
| **7 — Server wiring / lifespan hooks** | **shared (append-only)** | one appended `install_extensions(...)` call + at most one appended lifespan task in `omnigent/server` | Lands **last** among core-touching phases. |

A phase not in this table defaults to `bytedesk_omnigent`-only and must justify
any shared edit against Rules 1 and 2 in its goal doc before landing.

### Why the two shared phases land last

Phases 6 and 7 are the only ones that write into the upstream tree. They land
**after** every `bytedesk_omnigent`-only phase that depends on them is otherwise
complete, so that:

1. the extension-side code is already in place and tested before the core is
   asked to call it, and
2. the upstream-shared diff is a single, late, append-only hunk that a rebase
   replays once — not a moving target spread across the whole roadmap.

---

## Rebase sync points

A **sync point** is a deliberate moment where the fork rebases onto a fresh
`upstream/main`. The roadmap pins three:

1. **Before Phase 0 of any wave** — `git fetch upstream && git rebase
   upstream/main` onto a clean fork before opening the wave's worktrees, so the
   wave is authored on top of current upstream and not a stale base.
2. **Immediately before the first shared phase (Phase 6/7)** — re-sync upstream
   right before the append-only core edits land, so the appended hunk is written
   against the exact upstream lines it anchors to. This is the highest-risk
   moment; doing it on a fresh base makes the single hunk trivially replayable.
3. **After upstream cuts a release we adopt** — rebase the whole fork, then run
   the fork-delta audit (below) to confirm every `# bytedesk(...)` shared hunk
   still applies and no `bytedesk_omnigent`-only file was disturbed.

Between sync points, day-to-day feature work follows the repo's own gitflow
(`feature/*` → PR to `develop`, `main` for releases) — the worktree-operator
drives that; sync points are the *additional* upstream-facing rebase cadence on
top of it.

### Fork-delta audit (run at every sync point)

```bash
# Every intentional shared-file append is greppable by its marker.
git grep -n "bytedesk(" -- omnigent/ ap-web/ pyproject.toml setup.py

# A bytedesk_omnigent-only file must never appear in an upstream rebase conflict.
git diff --name-only upstream/main...HEAD -- bytedesk_omnigent/   # informational
```

If the first command surfaces a shared hunk that is *not* additive at a stable
anchor, that hunk is the rebase debt to pay down — move it behind the extension
seam before the next upstream release.

---

## Landing order (the one ordering invariant)

> **`bytedesk_omnigent`-only phases land in any order relative to upstream; the
> two shared phases (6, 7) land last, after a fresh rebase sync point, as a
> single append-only hunk.**

This is the load-bearing rule of the whole roadmap. Everything else —
JSON-in-`Text`, soft FKs, the ABC+impl+converter triad, the extension seam — is
in service of keeping the fork's upstream-facing surface to that one late,
replayable hunk.
