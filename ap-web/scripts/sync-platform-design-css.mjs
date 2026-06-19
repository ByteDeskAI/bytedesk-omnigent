import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const apWebRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(apWebRoot, "..");
const worktreeName = path.basename(repoRoot);
const workspaceRoot = repoRoot.includes(`${path.sep}.claude${path.sep}worktrees${path.sep}`)
  ? path.resolve(repoRoot, "..", "..", "..", "..")
  : path.resolve(repoRoot, "..");

const platformCandidates = [
  process.env.BYTEDESK_PLATFORM_ROOT && path.resolve(process.env.BYTEDESK_PLATFORM_ROOT),
  path.resolve(workspaceRoot, "bytedesk-platform", ".claude", "worktrees", worktreeName),
  path.resolve(workspaceRoot, "bytedesk-platform"),
].filter(Boolean);

function missionControlSource(platformRoot) {
  return path.join(
    platformRoot,
    "integration-packages",
    "platform-design-css",
    "mission-control.css",
  );
}

const source = platformCandidates.map(missionControlSource).find(existsSync);
const target = path.join(apWebRoot, "src", "styles", "bytedesk-mission-control.css");

if (!source) {
  throw new Error(
    `Platform Mission Control CSS not found. Checked:\n${platformCandidates
      .map(missionControlSource)
      .join("\n")}\n` +
      "Set BYTEDESK_PLATFORM_ROOT to the Platform checkout that owns integration-packages/platform-design-css.",
  );
}

mkdirSync(path.dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`Synced ${path.relative(apWebRoot, target)} from ${source}`);
