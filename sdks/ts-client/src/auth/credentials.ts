// Credential-provider seam — the TS port of the C# SDK's Auth/. The SDK never
// knows how a token is obtained: callers compose a provider (none, static header,
// bearer, OAuth client-credentials, RFC-8693 token-exchange, a chain) and the SDK
// applies it to each outgoing request. ZERO ByteDesk auth coupling lives here.
//
// A provider mutates `Headers` in place (matching the C# HttpRequestMessage seam)
// rather than the platform `Request` (whose headers are immutable once built), so
// the SDK applies credentials onto a Headers object it then attaches to fetch.

/**
 * Context passed to a {@link CredentialProvider} when a request is about to be
 * sent. Carries optional scope hints so a provider can mint or select a credential
 * narrowed to the call (e.g. an OBO scope per session).
 */
export interface RequestContext {
  /** The operation id of the call, when known (e.g. `"create_session"`). */
  readonly operationId?: string;
  /** An optional space-delimited scope hint for token-minting providers. */
  readonly scope?: string;
  /** An optional target resource/audience hint (RFC 8707 style). */
  readonly resource?: string;
}

/** An empty context (no hints). */
export const EMPTY_REQUEST_CONTEXT: RequestContext = {};

/**
 * Applies an authentication credential to an outgoing Omnigent request by
 * mutating its {@link Headers}. A provider that has nothing to contribute must
 * leave the headers untouched and return without throwing.
 */
export interface CredentialProvider {
  apply(headers: Headers, context: RequestContext): Promise<void>;
}

/** No-op provider — the request is sent unauthenticated. */
export class NoneCredentialProvider implements CredentialProvider {
  static readonly instance = new NoneCredentialProvider();
  async apply(): Promise<void> {
    /* nothing */
  }
}

/** Applies a fixed header (e.g. a gateway secret) to every request. */
export class StaticHeaderCredentialProvider implements CredentialProvider {
  constructor(
    private readonly name: string,
    private readonly value: string,
  ) {
    if (!name || name.trim().length === 0) throw new Error("headerName is required");
  }
  async apply(headers: Headers): Promise<void> {
    headers.set(this.name, this.value);
  }
}

/** A factory that produces a (possibly per-request) bearer token, or `null`. */
export type TokenFactory = (context: RequestContext) => Promise<string | null> | string | null;

/** Applies a static or supplied bearer token as `Authorization: Bearer`. */
export class BearerTokenCredentialProvider implements CredentialProvider {
  private readonly tokenFactory: TokenFactory;
  constructor(tokenOrFactory: string | TokenFactory) {
    if (typeof tokenOrFactory === "string") {
      if (!tokenOrFactory || tokenOrFactory.trim().length === 0) throw new Error("token is required");
      const token = tokenOrFactory;
      this.tokenFactory = () => token;
    } else {
      this.tokenFactory = tokenOrFactory;
    }
  }
  async apply(headers: Headers, context: RequestContext): Promise<void> {
    const token = await this.tokenFactory(context);
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }
}

/**
 * Delegates credential application to a supplied function — the most general
 * escape hatch. A consumer can apply any header/token logic without a class.
 */
export class DelegatingCredentialProvider implements CredentialProvider {
  constructor(
    private readonly applyFn: (headers: Headers, context: RequestContext) => Promise<void> | void,
  ) {}
  async apply(headers: Headers, context: RequestContext): Promise<void> {
    await this.applyFn(headers, context);
  }
}

/**
 * Tries an ordered list of providers and stops at the first that actually applies
 * a credential (changes the headers). A provider that leaves the headers unchanged
 * is treated as "no credential" and the chain moves on. Mirrors the C#
 * ChainCredentialProvider (first that yields a credential wins).
 */
export class ChainCredentialProvider implements CredentialProvider {
  private readonly providers: ReadonlyArray<CredentialProvider>;
  constructor(...providers: CredentialProvider[]) {
    this.providers = providers;
  }
  async apply(headers: Headers, context: RequestContext): Promise<void> {
    for (const provider of this.providers) {
      const before = fingerprint(headers);
      await provider.apply(headers, context);
      if (fingerprint(headers) !== before) return; // first to yield a credential wins
    }
  }
}

// A cheap structural fingerprint of the headers so the chain can tell whether a
// provider contributed anything.
function fingerprint(headers: Headers): string {
  const entries: string[] = [];
  headers.forEach((value, key) => entries.push(`${key}=${value}`));
  entries.sort();
  return entries.join(";");
}
