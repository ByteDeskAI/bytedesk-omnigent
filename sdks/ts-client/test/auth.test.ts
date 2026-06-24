import { describe, it, expect } from "vitest";
import {
  BearerTokenCredentialProvider,
  StaticHeaderCredentialProvider,
  ChainCredentialProvider,
  NoneCredentialProvider,
  ClientCredentialsCredentialProvider,
  EMPTY_REQUEST_CONTEXT,
} from "../src/index.js";

describe("credential providers", () => {
  it("bearer applies Authorization", async () => {
    const headers = new Headers();
    await new BearerTokenCredentialProvider("tok").apply(headers, EMPTY_REQUEST_CONTEXT);
    expect(headers.get("Authorization")).toBe("Bearer tok");
  });

  it("static header applies a fixed header", async () => {
    const headers = new Headers();
    await new StaticHeaderCredentialProvider("X-Omnigent-Secret", "s3cret").apply(headers, EMPTY_REQUEST_CONTEXT);
    expect(headers.get("X-Omnigent-Secret")).toBe("s3cret");
  });

  it("chain stops at the first provider that yields a credential", async () => {
    const headers = new Headers();
    const chain = new ChainCredentialProvider(
      NoneCredentialProvider.instance, // contributes nothing
      new BearerTokenCredentialProvider("first"),
      new StaticHeaderCredentialProvider("X-Should-Not-Apply", "no"),
    );
    await chain.apply(headers, EMPTY_REQUEST_CONTEXT);
    expect(headers.get("Authorization")).toBe("Bearer first");
    expect(headers.has("X-Should-Not-Apply")).toBe(false);
  });
});

describe("OAuth client-credentials caching", () => {
  it("mints once and caches until near expiry", async () => {
    let calls = 0;
    let now = 0;
    const fetchSpy = (async () => {
      calls++;
      return new Response(JSON.stringify({ access_token: `tok${calls}`, expires_in: 300 }), { status: 200 });
    }) as unknown as typeof fetch;
    // Swap the global fetch the provider uses.
    const original = globalThis.fetch;
    globalThis.fetch = fetchSpy;
    try {
      const provider = new ClientCredentialsCredentialProvider({
        tokenEndpoint: "https://idp.test/token",
        clientId: "id",
        clientSecret: "secret",
        now: () => now,
      });
      const h1 = new Headers();
      await provider.apply(h1, EMPTY_REQUEST_CONTEXT);
      const h2 = new Headers();
      await provider.apply(h2, EMPTY_REQUEST_CONTEXT);
      expect(calls).toBe(1); // cached
      expect(h1.get("Authorization")).toBe("Bearer tok1");
      expect(h2.get("Authorization")).toBe("Bearer tok1");

      // Advance past expiry (300s - 30s skew = 270s) → re-mints.
      now = 271_000;
      const h3 = new Headers();
      await provider.apply(h3, EMPTY_REQUEST_CONTEXT);
      expect(calls).toBe(2);
      expect(h3.get("Authorization")).toBe("Bearer tok2");
    } finally {
      globalThis.fetch = original;
    }
  });
});
