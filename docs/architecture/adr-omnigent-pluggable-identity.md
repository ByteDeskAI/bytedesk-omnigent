# ADR: Omnigent pluggable identity & auth (product-first)

**Status**: Accepted · **Date**: 2026-06-23 · **Repo**: bytedesk-omnigent (owned fork)

## Context

Omnigent's identity/auth was partly welded: the inbound principal was verified by an
HMAC hardwired inside one resolver (`bytedesk_omnigent/auth/principal_resolver.py`),
the verified `Principal` died at the server route and never reached tools/policies,
and outbound credential minting (`load_secret`, the OAuth `client_credentials`
egress, the Databricks pass-through) was three ad-hoc paths. We want omnigent to
**act as a product standalone** while letting a consumer (Office/platform, later)
**replace any identity subpart** — without forking the server surface.

The pluggable spine already exists: `omnigent/pluggable/registry.py` (`PluggableRegistry`
with first-class `default=`, `OMNIGENT_USE_<SEAM>` strangler env, per-seam
`discover_extensions`), the `omnigent/extensions.py` hook Protocol, the `SEAMS`
manifest table, and `SecretBackend` (the proven exemplar). This ADR extends that
spine to identity.

## Decision

Add an `omnigent/identity/` core package with a **minimal** set of replaceable ports,
each with an in-box default so a bare omnigent works with zero extensions:

| Port | Default (standalone) | Swap (consumer, later) | Status |
|---|---|---|---|
| `AssertionVerifier` | `HmacAssertionVerifier` (require-`exp`) | JWKS / OIDC-introspect | **registered seam now** — extracts the one inline HMAC + fixes the `exp` fail-open |
| `OutboundCredentialProvider` + `MintStrategy` | `StaticSecretProvider` / `static`·`client_credentials`·`pass_through` | token-exchange OBO | **registered seam now** — consolidates the three *existing* live egress paths |
| `AuthorizationProvider` | `OwnerAllowAuthorizer` | capability-enforcing | **registered seam now (default-only)** — typed seam the propagation can populate |
| `PrincipalResolver` | existing chain (`CompositeAuthProvider`) | gateway header resolver (exists) | **unchanged this slice** |
| `SecretBackend` | `LocalBackend` | `InfisicalBackend` (exists) | **unchanged — the template** |

Plus the boundary value object **`ActingIdentity`** (`principal` + `agent_id` +
`delegation`) propagated additively (`Optional`, default `None`) onto `ToolContext`
and `EvaluationContext` so the verified principal + acting agent reach the point of
action. `ActorIdentityResolver` is a plain function (`acting_identity_for`), **not**
a port — it has no second impl yet.

### The rule (anti-over-engineering)

A port earns a registry/hook/strangler-env only when a sane default exists **and** a
concrete alternative is genuinely needed in the same arc. `AssertionVerifier` (JWKS
second impl) and `OutboundCredentialProvider` (three live strategies) pass. The
`AuthorizationProvider` registry ships **default-only** as a typed seam; do not treat
it as load-bearing pluggability until a capability-enforcing impl lands.

### Secure-default invariants (pinned as tests)

1. `HmacAssertionVerifier` **requires `exp`** — reject absent/non-numeric `exp` (was a
   never-expires fail-open).
2. The inbound HMAC is a **shared secret omnigent itself can forge** — it is an
   identity *assertion*, never an authorization grant. Any future capability decision
   must re-derive server-side; the header contributes zero authz bits.
3. `acting_identity is None` ⇒ providers **degrade to today's standalone default**
   (static secret), never to elevated privilege.
4. Defaults are wired so a bare omnigent (no extensions) resolves a working provider
   for every seam (`resolve_default()` never raises).

### Explicitly deferred (keeps this slice product-safe)

- The "unconditional `CompositeAuthProvider` + `LocalSingleUserResolver` terminus"
  change to `omnigent/server/app.py` — it alters `get_principal()` control flow for
  bare deploys and is the highest-risk auth edit; defer until it has its own test
  matrix. The existing principal chain is untouched here.
- Per-tool migration to consume `OutboundCredentialProvider` (the native `del ctx`
  sites) and the token-exchange OBO strategy — that is the consumer/Office layer.

## Consequences

- Omnigent identity subparts are now replaceable seams, listed in `GET /v1/_capabilities`.
- The one inline HMAC is extracted + the `exp` hole closed.
- `ActingIdentity` reaches tools/policies (additive `None` ⇒ agent and agent→subagent
  spawn behave identically when no identity is present).
- No live egress or auth control-flow path changed ⇒ no runtime-behavior regression.
