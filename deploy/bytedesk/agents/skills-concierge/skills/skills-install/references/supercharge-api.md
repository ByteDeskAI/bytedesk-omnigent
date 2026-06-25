# Supercharge Claude Code — marketplace API (source of truth)

Reference: <https://superchargeclaudecode.com/api/agent-docs>

This is the contract the `supercharge` source adapter
(`omnigent/skills/acquisition.py`) is built from. The adapter only uses the
**public, no-auth GET** endpoints below — discovery + download. Publish,
favorites, marketplaces, and auth endpoints are intentionally out of scope.

- **Base URL:** `https://superchargeclaudecode.com` (override:
  `OMNIGENT_SUPERCHARGE_BASE_URL`).
- **Envelope:** every JSON response is `{ "success": boolean, "data"?: T, "error"?: string }`.

## Endpoints used

| Method / path | Purpose | Shape the adapter relies on |
|---|---|---|
| `GET /api/plugins` | List published plugins (search source) | `data: [{ slug, name, description, tags[], files: [{ fileName, s3Url }] }]` |
| `GET /api/plugins/:slug` | One plugin's file manifest | `data: { name, description, version, files: ["skills/<name>/SKILL.md", ...] }` |
| `GET /api/plugins/:slug/:filePath` | Download one file | `302` redirect to S3; body is the raw file bytes |

## How it maps to an omnigent skill

- A marketplace **plugin** is identified by its `slug` (e.g. `dogfood`). The
  `slug` is the `source_ref` for `sys_skill_stage_preview`.
- A plugin is **installable** into an omnigent agent only if it carries a
  `skills/<name>/SKILL.md`. The adapter's search filters to those; ~185 of ~211
  plugins qualify. A plugin may carry **multiple** skills (one preview package
  each).
- On stage, the adapter downloads the plugin's files preserving their relative
  paths into the staging workspace. `discover_skill_packages` then finds each
  `skills/<name>/SKILL.md` and stages only that skill subtree (the skill dir
  name must equal the SKILL.md frontmatter `name`). Plugin scaffolding outside a
  skill dir (`.claude-plugin/`, `commands/`, `hooks/`) is ignored.

## Safety

- The adapter only ever builds URLs against the pinned base host; slug and each
  file-path segment are validated against `[A-Za-z0-9._-]` with `.`/`..`
  rejected, so a malicious manifest cannot escape the workspace or redirect
  egress to an arbitrary host.
- File count and total size are bounded by the same `_MAX_SKILL_FILES` /
  `_MAX_SKILL_BYTES` caps as every other source.
