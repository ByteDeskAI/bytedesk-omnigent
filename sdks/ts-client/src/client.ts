// Composed entry point for the Omnigent SDK (ADR-0152). Exposes the typed ports:
// `sessions` (protocol mechanics over /v1/sessions), `events` (the typed SSE reader
// + semantic block stream), and `roster` (the cursor-paginated agent roster +
// per-agent image over /v1/agents). TS port of the C# SDK's OmnigentClient +
// OmnigentSdkOptions + the DI factory (here a plain constructor function).

import {
  NoneCredentialProvider,
  type CredentialProvider,
} from "./auth/credentials.js";
import { HttpTransport } from "./api/http.js";
import { Sessions } from "./api/sessions.js";
import { Events } from "./api/events.js";
import { Roster } from "./api/roster.js";

/** Options for {@link createOmnigentClient}. */
export interface OmnigentClientOptions {
  /** Base URL of the Omnigent server/gateway, e.g. `https://omnigent.example.com`. Required. */
  readonly baseUrl: string;
  /**
   * The credential provider applied to outgoing requests. Defaults to
   * {@link NoneCredentialProvider} (unauthenticated) — set a real provider
   * (bearer, static header, OAuth, or a chain) for protected deployments.
   */
  readonly credentials?: CredentialProvider;
  /** Per-request timeout in ms. Defaults to 100_000 (the .NET default); `0` disables. */
  readonly timeoutMs?: number;
  /**
   * Version-skew strictness for the event reader. When `false` (default), an SSE
   * frame whose `type` the SDK's pinned schema does not know is surfaced as an
   * `UnknownEvent` and the stream continues — newer server events never tear a
   * live stream down. When `true`, the reader instead throws
   * `OmnigentSchemaMismatchError` on the first unknown frame.
   */
  readonly throwOnUnknownEvent?: boolean;
  /** Injected fetch (defaults to the global `fetch`); lets tests stub the network. */
  readonly fetch?: typeof fetch;
}

/** The composed Omnigent SDK client: `sessions`, `events`, `roster`, + the active credential provider. */
export class OmnigentClient {
  /** The sessions port: bind-or-resume, post a turn, read items / child sessions / snapshot, runnable check. */
  readonly sessions: Sessions;
  /** The events port: typed SSE reader (`readRaw`) and semantic block stream (`readBlocks`). */
  readonly events: Events;
  /** The roster/org port: cursor-paginated agent list (`getRoster`) and per-agent image read/write. */
  readonly roster: Roster;
  /** The credential provider applied to SDK requests. */
  readonly credentials: CredentialProvider;

  constructor(options: OmnigentClientOptions) {
    if (!options.baseUrl) throw new Error("OmnigentClientOptions.baseUrl is required.");
    let url: URL;
    try {
      url = new URL(options.baseUrl);
    } catch {
      throw new Error("OmnigentClientOptions.baseUrl must be an absolute URL.");
    }
    if (!url.protocol.startsWith("http")) {
      throw new Error("OmnigentClientOptions.baseUrl must be an http(s) URL.");
    }

    this.credentials = options.credentials ?? NoneCredentialProvider.instance;
    const transport = new HttpTransport({
      baseUrl: options.baseUrl,
      credentials: this.credentials,
      ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
      ...(options.fetch !== undefined ? { fetch: options.fetch } : {}),
    });

    this.sessions = new Sessions(transport);
    this.events = new Events(transport, options.throwOnUnknownEvent ?? false);
    this.roster = new Roster(transport);
  }
}

/** Construct an {@link OmnigentClient} from options (the factory mirror of the C# `AddOmnigentSdk`). */
export function createOmnigentClient(options: OmnigentClientOptions): OmnigentClient {
  return new OmnigentClient(options);
}
