# Pluggable core & the hard fork (BDP-2371)

**Status:** accepted (2026-06-20). **Supersedes** the "actively-tracked alpha fork, rebased on upstream → minimize surface" posture in the earlier `fork-rebase-strategy.md` and the worktree-lifecycle fork-discipline block.

## 1. Direction: hard fork

`ByteDeskAI/bytedesk-omnigent` is **disconnected from the upstream parent** (`omnigent-ai/omnigent`). We no longer track, rebase on, or pull from upstream. It is an independent ByteDesk product.

**Consequence — these guardrails are VOID** (they only ever existed to keep upstream rebases clean):

- "additive-only edits to upstream-shared files" / "new functionality in new modules only"
- "minimal/append-only core edits", "entry-point indirection purely to avoid editing core"
- "no `@inject`/decorators in core"
- the `gh --repo` guard against a bare `gh` resolving to the `upstream` remote (drop the `upstream` remote)

Edit, refactor, rename, and delete core directly. **Ponytail/YAGNI still applies** — freedom to edit core is not licence to over-build; if anything the discipline matters *more* now that no upstream forcing-function prunes complexity.

## 2. The pluggable core (`omnigent.pluggable`)

Every technology, external dependency, and hardcoded implementation that should be swappable lives behind one uniform recipe (`omnigent/pluggable/registry.py`):

1. **`runtime_checkable` Protocol** per seam — the swap contract.
2. **`PluggableRegistry[T]`** — `register(name, factory)` + `get(name) -> Protocol` + a default registered in-module so behavior is unchanged until overridden; `describe()` for introspection.
3. **Entry-point discovery** — `discover_extensions(hook=...)` consults `BytedeskExtension.<seam>_factories()` so out-of-tree packages contribute with zero core edits. **Discovery runs at server startup, never at module import** (`omnigent/pluggable/manifest.py::discover_all_extensions`, called once in `create_app`'s lifespan) — importing a registry must NOT drag the FastAPI-heavy extension hub onto the runner subprocess hot path (guarded by `tests/runner/test_identity.py::test_importing_identity_does_not_pull_in_fastapi`).
4. **Optional strangler flag** `OMNIGENT_USE_<SEAM>` for risky cutovers (dual-run → parity → flip → delete twin).

Shared error taxonomy: `ProviderError` + `ProviderNotRegistered`/`ProviderUnconfigured`/`ProviderUnavailable`/`RegistryConflict` (`omnigent/pluggable/errors.py`).

Proven precedent the recipe copies: the `SecretBackend` chain (`omnigent/onboarding/secrets.py`).

## 3. What is pluggable today

Converted to registries/strategies across this epic (BDP-2344): harness identity (descriptor SoT, deletes the old hardcoded cross-package `hermes` string), artifact store, web search, memory embedder + the `AgentMemoryProvider` facade (store + embedder + recall), spec source, MCP manager + MCP HTTP auth scheme + MCP schema normalizer, tool-result formatter, idempotency store, cron schedule-kind + a live `SessionInitiator` dispatch, ingress webhook source adapter + secret resolver, suppression store, perf-metrics publisher, hermes binary, sandbox provider factory, advisory-locked maintenance-loop factory, LLM provider adapters (generic `BaseAdapter[TConn]` + wire TypedDicts), reasoning-tier + overflow-detector providers, backoff policy, schema validator, token counter, compaction-layer chain, OIDC IdP adapter, model-provider listing, OTLP metric exporter, inner-SDK exception-classifier chain, UC/remote-function executor, OSEnvironment factory.

**Capability manifest:** `GET /v1/_capabilities` enumerates the live seams (name, active impl, alternatives, override env) — the browser-visible surface of the framework.

**Composition root:** a `dependency-injector` container (`omnigent/server/container.py`, `OMNIGENT_USE_DI_CONTAINER`, default-OFF, boot-parity proven; abi3 wheels resolve on amd64+arm64).

## 4. Strong typing

Wire/domain contracts are typed (generic `BaseAdapter[TConn]`, `ChatCompletionResponse`/`ChatMessage` TypedDicts, durable-store lifecycle StrEnums + generic `LifecycleStateMachine[TStatus]`, `AgentSpecLike` Protocol, carrier fields typed as their seam Protocols — `mcp_manager` via the `McpManager` Protocol so typing reinforces pluggability rather than re-pinning it). Closed sets are concrete (`ProviderKind` StrEnum, frame `Literal`s, `PolicyRegistryRaw` TypedDict); the open provider set stays a registry-validated `NewType`, not a closed `Literal`.

## 5. Deliberately deferred (not built; tracked)

Per Ponytail + the sweep verdicts: infrastructure-provider *wrappers* (DatabaseProvider/WebSocketProvider/etc. — `BDP-2367`, YAGNI until a 2nd real impl); harness/runner *placement* for k8s-sandbox (`BDP-2351`, L-strategic); MLflow→TracerPort + presence store (gated on multi-replica); the `omnigent`↔`bytedesk_omnigent` package-boundary collapse + deterministic-id-scheme unification (`BDP-2372`, evaluate-don't-big-bang + needs an id migration); harness warm-pool (`BDP-2373`, measure-first).

## 6. Follow-up outside this repo

Update the platform repo's `.claude/rules/worktree-lifecycle.md` (the omnigent fork-conventions block) to drop the "rebased on upstream / minimize surface" framing, consistent with §1.
