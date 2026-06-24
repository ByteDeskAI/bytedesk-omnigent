import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  parseServerStreamEvent,
  SERVER_STREAM_EVENT_TYPES,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const schemaPath = join(here, "..", "src", "contract", "server-stream-event.schema.json");
const schema = JSON.parse(readFileSync(schemaPath, "utf8")) as {
  discriminator: { propertyName: string; mapping: Record<string, string> };
};

describe("every wire type has a generated variant", () => {
  const wireTypes = Object.keys(schema.discriminator.mapping);

  it("the schema declares 44 discriminator variants", () => {
    expect(wireTypes).toHaveLength(44);
  });

  it("the generated known-set covers exactly the schema's discriminator mapping", () => {
    expect(new Set(SERVER_STREAM_EVENT_TYPES)).toEqual(new Set(wireTypes));
  });

  it("every wire type parses to a typed (non-unknown) event", () => {
    for (const t of wireTypes) {
      const result = parseServerStreamEvent({ type: t });
      expect("kind" in result && result.kind === "unknown", `type ${t} should be known`).toBe(false);
      expect((result as { type: string }).type).toBe(t);
    }
  });

  it("an unmapped type parses to UnknownEvent (never throws)", () => {
    const result = parseServerStreamEvent({ type: "response.totally_new", extra: 1 });
    expect("kind" in result && result.kind === "unknown").toBe(true);
    if ("kind" in result && result.kind === "unknown") {
      expect(result.type).toBe("response.totally_new");
      expect(result.raw).toEqual({ type: "response.totally_new", extra: 1 });
    }
  });

  it("a non-object / type-less input parses to UnknownEvent with empty type", () => {
    expect(parseServerStreamEvent(null)).toMatchObject({ kind: "unknown", type: "" });
    expect(parseServerStreamEvent(42)).toMatchObject({ kind: "unknown", type: "" });
    expect(parseServerStreamEvent({ no_type: true })).toMatchObject({ kind: "unknown", type: "" });
  });
});
