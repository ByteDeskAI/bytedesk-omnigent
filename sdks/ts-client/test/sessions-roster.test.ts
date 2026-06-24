import { describe, it, expect } from "vitest";
import { createOmnigentClient, StaticHeaderCredentialProvider } from "../src/index.js";
import { fakeJsonFetch } from "./fixtures.js";

describe("Sessions port", () => {
  it("bind-or-resume posts agent_id + sends Idempotency-Key from external_key", async () => {
    let captured: { url: string; init: RequestInit } | null = null;
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      credentials: new StaticHeaderCredentialProvider("X-Omnigent-Secret", "s"),
      fetch: fakeJsonFetch(
        { id: "conv_1", agent_id: "ag_1", status: "idle", created_at: 1 },
        (url, init) => {
          captured = { url, init };
        },
      ),
    });
    const snap = await client.sessions.bindOrResume({ agentId: "ag_1", externalKey: "ext-key-1" });
    expect(snap.id).toBe("conv_1");
    expect(captured!.url).toBe("https://omni.test/v1/sessions");
    const body = JSON.parse(captured!.init.body as string);
    expect(body).toEqual({ agent_id: "ag_1", external_key: "ext-key-1" });
    const headers = new Headers(captured!.init.headers);
    expect(headers.get("Idempotency-Key")).toBe("ext-key-1");
    // The credential provider was applied too.
    expect(headers.get("X-Omnigent-Secret")).toBe("s");
  });

  it("isRunnable returns false for a terminal status", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeJsonFetch({ id: "conv_1", status: "failed" }),
    });
    expect(await client.sessions.isRunnable("conv_1")).toBe(false);
  });

  it("isRunnable returns true for a live status", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeJsonFetch({ id: "conv_1", status: "running" }),
    });
    expect(await client.sessions.isRunnable("conv_1")).toBe(true);
  });

  it("getItems unwraps the data array", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeJsonFetch({ data: [{ id: "a" }, { id: "b" }] }),
    });
    const items = await client.sessions.getItems("conv_1");
    expect(items).toEqual([{ id: "a" }, { id: "b" }]);
  });
});

describe("Roster port", () => {
  it("getRoster tolerates workflow: null (the BDP-2301 footgun)", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeJsonFetch({
        data: [
          { name: "maya", workflow: null },
          { name: "orchestrator", workflow: true },
        ],
        has_more: false,
        last_id: null,
      }),
    });
    const roster = await client.roster.getRoster();
    expect(roster).toHaveLength(2);
    expect(roster[0]!.workflow).toBeNull();
    expect(roster[1]!.workflow).toBe(true);
  });

  it("getAgentImage returns null on 404", async () => {
    const client = createOmnigentClient({
      baseUrl: "https://omni.test",
      fetch: fakeJsonFetch({}, undefined, 404),
    });
    expect(await client.roster.getAgentImage("ag_missing")).toBeNull();
  });
});
