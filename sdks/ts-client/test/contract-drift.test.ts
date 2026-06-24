import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { CONTRACT_LOCK } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const contractDir = join(here, "..", "src", "contract");

function sha256(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

describe("contract drift gate", () => {
  const lock = JSON.parse(readFileSync(join(contractDir, "contract.lock"), "utf8")) as {
    openapi_sha256: string;
    events_sha256: string;
    omnigent_version: string;
  };

  it("sha256 of the committed openapi.json matches contract.lock", () => {
    expect(sha256(join(contractDir, "openapi.json"))).toBe(lock.openapi_sha256);
  });

  it("sha256 of the committed event schema matches contract.lock", () => {
    expect(sha256(join(contractDir, "server-stream-event.schema.json"))).toBe(lock.events_sha256);
  });

  it("the runtime CONTRACT_LOCK constant equals contract.lock byte-for-byte", () => {
    expect(CONTRACT_LOCK.openapi_sha256).toBe(lock.openapi_sha256);
    expect(CONTRACT_LOCK.events_sha256).toBe(lock.events_sha256);
    expect(CONTRACT_LOCK.omnigent_version).toBe(lock.omnigent_version);
  });
});
