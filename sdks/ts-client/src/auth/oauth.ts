// OAuth credential providers — the TS port of the C# SDK's OAuthCredentialProviders.
// Dependency-free: uses the platform `fetch` for the token endpoint. Both providers
// share a single-flight token cache that refreshes a little before expiry.

import type { CredentialProvider, RequestContext } from "./credentials.js";

/** The token-endpoint response subset both OAuth providers read (RFC 6749 §5.1). */
export interface OAuthTokenResponse {
  readonly access_token?: string;
  readonly token_type?: string;
  readonly expires_in?: number;
}

// Refresh a little early so a token never expires mid-flight.
const SKEW_MARGIN_MS = 30_000;

/**
 * Shared cache + Authorization application for the OAuth providers. A single
 * in-flight token fetch is serialized so a burst of requests mints once.
 */
export abstract class CachingTokenCredentialProvider implements CredentialProvider {
  private token: string | null = null;
  private expiresAt = 0;
  private inFlight: Promise<string> | null = null;
  private readonly now: () => number;

  protected constructor(now?: () => number) {
    this.now = now ?? Date.now;
  }

  /** Fetch a fresh token from the authorization server. */
  protected abstract fetchToken(context: RequestContext): Promise<OAuthTokenResponse>;

  async apply(headers: Headers, context: RequestContext): Promise<void> {
    const token = await this.getToken(context);
    headers.set("Authorization", `Bearer ${token}`);
  }

  private async getToken(context: RequestContext): Promise<string> {
    if (this.token !== null && this.now() < this.expiresAt) return this.token;
    // Single-flight: callers racing a cold cache await the same fetch.
    if (this.inFlight === null) {
      this.inFlight = this.refresh(context).finally(() => {
        this.inFlight = null;
      });
    }
    return this.inFlight;
  }

  private async refresh(context: RequestContext): Promise<string> {
    if (this.token !== null && this.now() < this.expiresAt) return this.token;
    const response = await this.fetchToken(context);
    if (!response.access_token) {
      throw new Error("Omnigent token endpoint returned no access_token.");
    }
    const lifetimeSec = response.expires_in && response.expires_in > 0 ? response.expires_in : 300;
    this.expiresAt = this.now() + lifetimeSec * 1000 - SKEW_MARGIN_MS;
    this.token = response.access_token;
    return this.token;
  }
}

async function postForm(tokenEndpoint: string, form: Record<string, string>): Promise<OAuthTokenResponse> {
  const body = new URLSearchParams(form);
  const res = await fetch(tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) {
    throw new Error(`Omnigent token endpoint returned ${res.status} ${res.statusText}.`);
  }
  return (await res.json()) as OAuthTokenResponse;
}

/** Options for {@link ClientCredentialsCredentialProvider}. */
export interface ClientCredentialsOptions {
  readonly tokenEndpoint: string;
  readonly clientId: string;
  readonly clientSecret: string;
  readonly scope?: string;
  /** Clock injection for tests (defaults to `Date.now`). */
  readonly now?: () => number;
}

/**
 * OAuth 2.0 Client Credentials grant (RFC 6749 §4.4). Posts
 * `grant_type=client_credentials` to the token endpoint and caches the resulting
 * bearer token until it nears expiry.
 */
export class ClientCredentialsCredentialProvider extends CachingTokenCredentialProvider {
  constructor(private readonly opts: ClientCredentialsOptions) {
    super(opts.now);
  }
  protected async fetchToken(context: RequestContext): Promise<OAuthTokenResponse> {
    const form: Record<string, string> = {
      grant_type: "client_credentials",
      client_id: this.opts.clientId,
      client_secret: this.opts.clientSecret,
    };
    const scope = context.scope ?? this.opts.scope;
    if (scope) form["scope"] = scope;
    if (context.resource) form["resource"] = context.resource;
    return postForm(this.opts.tokenEndpoint, form);
  }
}

/** Options for {@link TokenExchangeCredentialProvider}. */
export interface TokenExchangeOptions {
  readonly tokenEndpoint: string;
  readonly clientId: string;
  readonly clientSecret: string;
  /** Per-request subject token (the caller's identity to delegate). */
  readonly subjectTokenFactory: (context: RequestContext) => Promise<string> | string;
  /** Optional actor token for delegation/OBO. */
  readonly actorTokenFactory?: (context: RequestContext) => Promise<string | null> | string | null;
  readonly audience?: string;
  readonly scope?: string;
  /** Clock injection for tests (defaults to `Date.now`). */
  readonly now?: () => number;
}

const GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange";
const ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token";

/**
 * OAuth 2.0 Token Exchange (RFC 8693). Exchanges a subject token (and optional
 * actor token, for delegation/OBO) for an access token scoped to Omnigent. The
 * subject token is supplied per-request so the SDK can carry the caller's identity.
 */
export class TokenExchangeCredentialProvider extends CachingTokenCredentialProvider {
  constructor(private readonly opts: TokenExchangeOptions) {
    super(opts.now);
  }
  protected async fetchToken(context: RequestContext): Promise<OAuthTokenResponse> {
    const subjectToken = await this.opts.subjectTokenFactory(context);
    const form: Record<string, string> = {
      grant_type: GRANT_TYPE,
      client_id: this.opts.clientId,
      client_secret: this.opts.clientSecret,
      subject_token: subjectToken,
      subject_token_type: ACCESS_TOKEN_TYPE,
      requested_token_type: ACCESS_TOKEN_TYPE,
    };
    if (this.opts.actorTokenFactory) {
      const actorToken = await this.opts.actorTokenFactory(context);
      if (actorToken) {
        form["actor_token"] = actorToken;
        form["actor_token_type"] = ACCESS_TOKEN_TYPE;
      }
    }
    const audience = context.resource ?? this.opts.audience;
    if (audience) form["audience"] = audience;
    const scope = context.scope ?? this.opts.scope;
    if (scope) form["scope"] = scope;
    return postForm(this.opts.tokenEndpoint, form);
  }
}
