---
name: skills-install
description: Install an agent skill into one or more target agents and prove it took. Use whenever the conversation asks to give an agent, a department, or the whole org a new skill / capability — discover it online, stage it, apply it, verify each target by probing it, and roll back any target that fails.
---

# Install a skill, then prove it

This is the procedure for persistently installing an agent skill and verifying it
actually loaded on each target — an install → verify → diagnose → rollback saga.
It honors the same discipline as configuring an agent image: preserve existing
config, stay idempotent, infer the target set, exclude workflow/orchestrator
agents by default, and never write secrets.

## 1. Resolve the target set

Turn the user's scope into concrete agents with `skills__resolve_targets`:

- `organization` — every non-workflow agent.
- `department:<name>` — one department's agents.
- `employee:<id-or-display-name>` — a single agent.
- a bare agent name — that one agent.

Workflow/orchestrator agents are excluded from `organization` / `department`
scopes; include one only if the user names it explicitly. Echo the resolved
targets back before any mutation.

## 2. Find and stage (no mutation yet)

`skills__search(query)` → choose the best hit; its `name` is the
`owner/repo@skill` install ref. `skills__stage_preview(source="skills",
source_ref=<ref>, target_agent_ids=[...], install_mode="skip_existing")` fetches
and validates the skill files and computes a per-agent action plan WITHOUT
applying. Show the staged files and the per-agent actions and get an explicit
go-ahead.

`install_mode` is `skip_existing` by default so a target that already has the
skill is a no-op (idempotent re-run). Use `replace` only for an explicit
reinstall/update.

## 3. Apply

`skills__apply_preview(preview_id)`. Record exactly which targets the apply
**succeeded** on. Only those are installed; only those are verified or rolled
back. A target that errored at apply time was never installed — don't probe or
roll it back, just report it.

## 4. Verify each installed target (fresh probe session)

A skill only counts as installed when the target agent can actually load it on a
new turn, so probe in a **fresh** session (its bundle was just re-versioned). For
every installed target, in the same turn:

1. `sys_session_create(agent_id="<target id>")` → capture `session_id`.
2. `sys_session_send(session_id=..., args={"input": "<probe>"})`, where the probe
   asks the agent to reply EXACTLY `installed and ready to go` if and only if the
   `<skill-name>` skill is now available to it, and otherwise to say what is
   missing.

Fire create+send for all installed targets, then END THE TURN. The inbox wakes
you per reply; `sys_read_inbox` to drain.

## 5. Classify — three states, not two

- **VERIFIED** — reply contains `installed and ready to go`.
- **FAILED** — reply indicates the skill is missing / errored / did not load.
- **UNVERIFIABLE** — no usable reply (runner offline, timeout, agent un-probeable).

`UNVERIFIABLE` is **not** failure. The bundle write succeeded; report it as
"installed, not runtime-confirmed" and leave it in place. Do **not** roll back an
unverifiable target — rolling back a good install because a probe couldn't run is
the worst outcome.

## 6. Diagnose and, if needed, roll back — per target only

For a FAILED target: re-check `skills__installed(agent_id=<target>)`, re-apply
once if it looks transient, and re-probe. If it still won't load, roll back that
target ALONE:

```
skills__remove("<skill-name>", ["<failed target id>"])
```

Never roll back a target that verified, and never let one target's failure touch
another target's successful install.

## 7. Report

Per target: verified / installed-not-confirmed / rolled-back (with the reason).
Then offer to continue. Never include secret values anywhere in the report.
