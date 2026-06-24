# @bytedesk/omnigent-sdk

TypeScript SDK for the [Omnigent](https://omnigent.dev) server — the npm sibling of
the C# `ByteDesk.Omnigent.Sdk` and omnigent's `sdks/python-client`. It is generated
from the **same pinned omnigent schema** as those two SDKs, has **zero runtime
dependencies** (uses the platform `fetch` / `ReadableStream` / `TextDecoder`), and
talks directly to an Omnigent deployment — no ByteDesk coupling.

It gives you:

- **Typed sessions** — bind-or-resume, post a turn, read items / child sessions /
  snapshot, an `isRunnable` readiness check.
- **The `ServerStreamEvent` SSE union** — all 44 event variants as a discriminated
  union keyed on `type`, with a tolerant `parseServerStreamEvent` and version-skew
  safety (an unknown event becomes an `UnknownEvent`, it never tears the stream
  down).
- **The semantic `OmnigentBlock` stream** — the raw event stream folded into
  render-ready blocks (text, reasoning, tool calls + results, the delegation /
  spawn tree) with the same collation fidelity as the C# and Python SDKs, plus
  composable transforms.
- **A credential-provider seam** — none / static-header / bearer / delegating /
  chain, plus OAuth client-credentials and RFC-8693 token-exchange (OBO).
- **The agent roster / org port** — the cursor-paginated agent list and the
  per-agent editable image.

## Install

```bash
npm install @bytedesk/omnigent-sdk
```

Requires a runtime with a global `fetch` (Node ≥ 18, Deno, Bun, the browser). On a
runtime without one, pass `fetch` in the client options.

## Quick start (independent, direct to omnigent)

```ts
import { createOmnigentClient } from "@bytedesk/omnigent-sdk";

const omnigent = createOmnigentClient({
  baseUrl: "https://omnigent.example.com",
});

// 1) Bind to an agent (idempotent: a repeat with the same externalKey resumes).
const session = await omnigent.sessions.bindOrResume({
  agentId: "ag_abc123",
  externalKey: "my-stable-key",
});

// 2) Post a user turn.
await omnigent.sessions.postTurn(session.id, {
  type: "message",
  data: { role: "user", content: [{ type: "input_text", text: "Hi!" }] },
});

// 3a) Read the raw typed event stream.
for await (const event of omnigent.events.readRaw(session.id)) {
  if ("kind" in event && event.kind === "unknown") {
    // A newer server event the SDK's pinned schema doesn't know — tolerated.
    continue;
  }
  switch (event.type) {
    case "response.output_text.delta":
      process.stdout.write(event.delta);
      break;
    case "turn.completed":
      console.log("\n[done]");
      break;
  }
}

// 3b) …or the semantic block stream (render-ready).
for await (const block of omnigent.events.readBlocks(session.id)) {
  switch (block.kind) {
    case "text_chunk":
      process.stdout.write(block.text);
      break;
    case "tool_group":
      for (const ex of block.executions) console.log(`→ ${ex.name}(${ex.argsSummary})`);
      break;
    case "delegation":
      console.log(`spawned ${block.childAgentName} (${block.status})`);
      break;
  }
}
```

### Composable block transforms

```ts
import { pipe, skipBlocks, mergeTextAcrossIterations, onlyAgent } from "@bytedesk/omnigent-sdk";

const stream = pipe(
  omnigent.events.readBlocks(session.id),
  skipBlocks("reasoning"),          // drop reasoning blocks
  mergeTextAcrossIterations(),      // one TextDone per response
  onlyAgent("maya"),                // only the root agent's blocks
);
for await (const block of stream) {
  /* … */
}
```

## Authentication (credential providers)

The SDK never knows how a token is obtained — you compose a provider and it's
applied to every request. Pick the one that matches your deployment:

```ts
import {
  createOmnigentClient,
  StaticHeaderCredentialProvider,
  BearerTokenCredentialProvider,
  ClientCredentialsCredentialProvider,
  TokenExchangeCredentialProvider,
  ChainCredentialProvider,
} from "@bytedesk/omnigent-sdk";

// A gateway secret header:
createOmnigentClient({
  baseUrl,
  credentials: new StaticHeaderCredentialProvider("X-Omnigent-Secret", secret),
});

// A bearer token (static or per-request via a factory):
createOmnigentClient({
  baseUrl,
  credentials: new BearerTokenCredentialProvider(() => getAccessToken()),
});

// OAuth 2.0 client-credentials (cached until near expiry):
createOmnigentClient({
  baseUrl,
  credentials: new ClientCredentialsCredentialProvider({
    tokenEndpoint: "https://idp.example.com/oauth/token",
    clientId,
    clientSecret,
    scope: "omnigent",
  }),
});

// RFC-8693 token exchange (on-behalf-of):
createOmnigentClient({
  baseUrl,
  credentials: new TokenExchangeCredentialProvider({
    tokenEndpoint: "https://idp.example.com/oauth/token",
    clientId,
    clientSecret,
    subjectTokenFactory: () => getUserSubjectToken(),
    audience: "omnigent",
  }),
});

// First-that-yields-a-credential-wins fallback chain:
new ChainCredentialProvider(bearerProvider, gatewaySecretProvider);
```

## Version skew

By default an SSE frame whose `type` the SDK's pinned schema does not know is
surfaced as an `UnknownEvent` and the stream continues. Set
`throwOnUnknownEvent: true` to instead throw `OmnigentSchemaMismatchError` on the
first unknown frame (for callers that require exact contract parity). The pinned
contract identity is exported as `CONTRACT_LOCK`.

## Codegen

`src/generated/` is produced from the pinned `src/contract/` snapshot and is never
hand-edited:

- `server-stream-events.ts` — the 44-variant `ServerStreamEvent` union + its shared
  sub-objects + `parseServerStreamEvent`, emitted by `codegen/generate-events.mjs`.
  A stock generator (openapi-typescript / NSwag) flattens the oneOf+discriminator
  into a bare union with no runtime dispatcher, so a custom emitter is required —
  see [`codegen/SPIKE.md`](./codegen/SPIKE.md).
- `openapi-types.ts` — the broader HTTP DTOs, emitted by `openapi-typescript`.

```bash
npm run regenerate   # reproduces both files byte-for-byte from src/contract/
```

To refresh the contract: re-copy `openapi.json` + regenerate
`server-stream-event.schema.json` from omnigent, re-lock `contract.lock`, then
`regenerate`. The drift-gate test fails until the lock matches.

## Develop

```bash
npm install
npm run build   # tsc (declarations + ESM)
npm test        # vitest
```
