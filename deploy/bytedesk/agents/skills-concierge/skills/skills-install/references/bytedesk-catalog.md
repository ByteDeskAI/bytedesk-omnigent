# ByteDesk GitHub marketplace catalog

The ByteDesk catalog is a Claude-format marketplace hosted on GitHub:

- Repo: `ByteDeskAI/bytedesk-marketplace`
- Catalog file: `.claude-plugin/marketplace.json` on `main`
- Each plugin is a subdirectory (for example `platform-dev`, `platform-architecture`)

## Search

Use `sys_skill_search(query, sources=["github_marketplace"])`.

Hits name a **plugin** (for example `platform-dev`). The `source_ref` is
`ByteDeskAI/bytedesk-marketplace@<plugin>`.

## Stage a whole plugin

```
sys_skill_stage_preview(
  source="github_marketplace",
  source_ref="ByteDeskAI/bytedesk-marketplace@platform-dev",
  target_agent_ids=[...],
)
```

This materializes every skill under that plugin's `skills/` tree.

## Stage one skill from a plugin

```
sys_skill_stage_preview(
  source="github_marketplace",
  source_ref="ByteDeskAI/bytedesk-marketplace/platform-dev/bytedesk-architect",
  target_agent_ids=[...],
)
```

Append `#<git-ref>` when you need a non-default branch (for example `#develop`).

## Apply, verify, rollback

Identical to skills.sh and Supercharge after staging: `sys_skill_apply` → probe
each target → `sys_skill_remove` only for verified failures.