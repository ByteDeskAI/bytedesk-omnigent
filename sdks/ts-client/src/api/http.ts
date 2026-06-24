// Thin fetch transport shared by the typed ports. Owns base-URL joining and
// credential application (per-request, with operation-scope hints) — never
// ByteDesk policy. The platform `fetch` is used directly; zero runtime deps.

import type { CredentialProvider, RequestContext } from "../auth/credentials.js";

export interface HttpTransportOptions {
  /** Base URL of the Omnigent server/gateway, e.g. `https://omnigent.example.com`. */
  readonly baseUrl: string;
  /** The credential provider applied to every request. */
  readonly credentials: CredentialProvider;
  /** Per-request timeout in ms (default 100_000, the .NET default). `0` disables. */
  readonly timeoutMs?: number;
  /** Injected fetch (defaults to the global `fetch`); lets tests stub the network. */
  readonly fetch?: typeof fetch;
}

/** A small typed transport: applies credentials, joins the base URL, runs fetch. */
export class HttpTransport {
  private readonly baseUrl: string;
  private readonly credentials: CredentialProvider;
  private readonly timeoutMs: number;
  private readonly doFetch: typeof fetch;

  constructor(opts: HttpTransportOptions) {
    if (!opts.baseUrl) throw new Error("baseUrl is required");
    // Normalize: drop a single trailing slash so `${base}${path}` (path starts `/`) is clean.
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.credentials = opts.credentials;
    this.timeoutMs = opts.timeoutMs ?? 100_000;
    this.doFetch = opts.fetch ?? globalThis.fetch;
    if (typeof this.doFetch !== "function") {
      throw new Error("No fetch implementation available — pass `fetch` in options on a runtime without a global fetch.");
    }
  }

  /** Build a fully-resolved request URL from a path (which must start with `/`). */
  url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  /**
   * Send a request: applies credentials (with the given scope context) onto a
   * fresh Headers, attaches an abort-on-timeout signal, and runs fetch. The caller
   * owns response decoding (json / SSE stream).
   */
  async send(
    method: string,
    path: string,
    context: RequestContext,
    init?: { body?: BodyInit; headers?: Record<string, string>; accept?: string; signal?: AbortSignal },
  ): Promise<Response> {
    const headers = new Headers(init?.headers ?? {});
    if (init?.accept) headers.set("Accept", init.accept);
    if (init?.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    await this.credentials.apply(headers, context);

    const signal = this.combineSignals(init?.signal);
    const res = await this.doFetch(this.url(path), {
      method,
      headers,
      ...(init?.body !== undefined ? { body: init.body } : {}),
      ...(signal ? { signal } : {}),
    });
    return res;
  }

  private combineSignals(caller?: AbortSignal): AbortSignal | undefined {
    if (this.timeoutMs <= 0) return caller;
    const timeout = AbortSignal.timeout(this.timeoutMs);
    if (!caller) return timeout;
    // Prefer the standard combinator when available; fall back to the timeout.
    const anyOf = (AbortSignal as unknown as { any?: (s: AbortSignal[]) => AbortSignal }).any;
    return anyOf ? anyOf([caller, timeout]) : timeout;
  }
}

/** Throw a descriptive error for a non-2xx response (mirrors EnsureSuccessStatusCode). */
export async function ensureOk(res: Response, what: string): Promise<void> {
  if (res.ok) return;
  let detail = "";
  try {
    detail = await res.text();
  } catch {
    /* ignore */
  }
  throw new Error(
    `Omnigent ${what} failed: ${res.status} ${res.statusText}${detail ? ` — ${detail.slice(0, 500)}` : ""}`,
  );
}
