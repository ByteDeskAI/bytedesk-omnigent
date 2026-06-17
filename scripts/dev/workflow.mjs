#!/usr/bin/env node
// bytedesk-omnigent worktree-operator shim (BDP-2144).
//
// The worktree-operator lives once in the sibling bytedesk-platform checkout
// (scripts/dev/workflow.mjs). It is repo-aware: it detects the target repo from
// the current working directory's canonical checkout, so running it from here
// drives bytedesk-omnigent (its GitHub repo, develop/develop-remote, and the
// kustomize hostPath localDev remap) with the same gitflow as the platform.
//
// This shim just forwards every arg to that shared operator, preserving cwd so
// detection works. Resolution order:
//   1. $BYTEDESK_PLATFORM_OPERATOR (explicit path override)
//   2. sibling layout: <parent-of-this-repo>/bytedesk-platform/scripts/dev/workflow.mjs
//
// The repo root is resolved via git's common-dir (not this file's path) so the
// shim works the same whether invoked from the canonical checkout or any
// worktree under .claude/worktrees/.
//
// Usage (identical to platform): node scripts/dev/workflow.mjs <status|new|ship|land|...>
import { spawnSync, execSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

function canonicalRepoRoot() {
  // git-common-dir is the canonical .git even from a linked worktree; its parent
  // is the canonical checkout root.
  const commonDir = execSync("git rev-parse --path-format=absolute --git-common-dir", {
    encoding: "utf8",
  }).trim();
  return dirname(commonDir);
}

const siblingParent = dirname(canonicalRepoRoot());

const candidates = [
  process.env.BYTEDESK_PLATFORM_OPERATOR,
  join(siblingParent, "bytedesk-platform", "scripts", "dev", "workflow.mjs"),
].filter(Boolean);

const operator = candidates.find((p) => existsSync(p));
if (!operator) {
  console.error(
    "bytedesk-omnigent: cannot locate the shared worktree-operator.\n" +
      "Expected a sibling bytedesk-platform checkout, or set $BYTEDESK_PLATFORM_OPERATOR.\n" +
      `Tried:\n${candidates.map((c) => `  - ${c}`).join("\n")}`,
  );
  process.exit(1);
}

const result = spawnSync("node", [operator, ...process.argv.slice(2)], {
  stdio: "inherit",
  cwd: process.cwd(),
});
process.exit(result.status ?? 1);
