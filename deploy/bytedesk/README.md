# ByteDesk × Omnigent — Phase 0 deploy

Stand up the forked Omnigent agent runtime **in the `bytedesk` namespace**, wired
into ByteDesk's GitOps/Fleet/TeamCity exactly the way OpenClaw is — as a
**separate Fleet bundle**, so it occupies OpenClaw's deployment niche for a clean
eventual cutover. Phase 0 proves the runtime locally with internal engineering
agents on ByteDesk's Codex inference path; OpenClaw keeps running untouched.

> **Why a separate bundle, not the platform Helm chart?** Omnigent is the
> OpenClaw replacement, and OpenClaw is itself a separate Fleet bundle (not in the
> platform chart). Mirroring that keeps the lifecycles decoupled (a fast-moving
> alpha fork shouldn't gate on platform releases), fits a forked third-party
> Python server (own image/DB/config), and makes teardown trivial — while staying
> seamless: same namespace, same Harbor registry + pull secret, same TeamCity
> release that bumps its tag in lockstep, same Fleet reconcile.

## Architecture (matches `deploy/docker/entrypoint.py`)

```
            bytedesk namespace (MicroK8s local / RKE2 prod)
┌──────────────────────────────────────────────────────────────────┐
│  omnigent-server  (control plane only — NEVER executes agents)     │
│    • REST/SSE API + SPA + policies + sessions                      │
│    • coordinates runners through the configured transport           │
│    • state → omnigent-postgres (dedicated, self-contained)         │
│            ▲ runner control plane                                   │
│  omnigent-runner (host)  ── executes agents, holds Codex creds ─────│
│    • openai-agents harness → HARNESS_OPENAI_AGENTS_* (Codex gw)     │
│    • runs the bundles in deploy/bytedesk/agents/                    │
└──────────────────────────────────────────────────────────────────┘
```

## Files

| Path | What |
|---|---|
| `k8s/` | Base manifests (namespace `bytedesk`). **Local:** `kubectl apply -k k8s/`. |
| `k8s/secret.example.yaml` | Copy → `secret.yaml` (gitignored), fill, apply. Prod uses Infisical/SOPS. |
| `k8s/nats-ui.yaml` | Internal NUI web console for the consolidated Omnigent NATS instance. |
| `fleet/production/kustomization.yaml` | Prod overlay (Harbor image, `# pipeline-managed` tag, pull secret). Fleet-reconciled. |
| `agents/bytedesk-engineer/` | Single agent — proves Codex inference. |
| `agents/bytedesk-orchestrator/` | Orchestrator + 2 sub-agents — proves delegation (gateway-only, no external CLIs). |
| `env/runner.env.example` | Laptop-runner inference creds. |
| `smoke.sh` | Control-plane smoke + prints the agent-run commands. |

## Choose your inference path

The runner needs to reach a model. Pick one (set in `k8s/secret.yaml` →
`omnigent-runner-secrets`, or `env/runner.env` for a laptop runner):

- **Option A — direct provider key** (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`).
  Fastest way to prove the *fork itself* runs. Set the agent harness to match
  (`openai-agents` for OpenAI, `claude-sdk` for Anthropic).
- **Option B — ByteDesk Codex gateway (the Phase-0 goal).** Point the
  `openai-agents` harness at your OpenAI-compatible Codex endpoint:
  `HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL=https://<host>/v1`,
  `HARNESS_OPENAI_AGENTS_API_KEY=<infisical>`, **`HARNESS_OPENAI_AGENTS_USE_RESPONSES=0`**
  (a chat/completions gateway 404s on the Responses wire). ← **fill the real URL/key from Infisical.**
- **Option C — Codex CLI subscription** (most faithful to the platform's mandated
  Codex-OAuth path): use the `codex` harness with `~/.codex` auth mounted on the
  runner. Heaviest to wire; do it once Option B is green.

## Build the images

The server and host images are the local-dev dependency bases. The server image
bakes the Omnigent venv, Postgres/NATS/recall dependencies, and server-side
Skills acquisition tools (`gh`, `node`, `npm`, `npx`). The host image bakes the
harness runtime tools (`git`, `tmux`, `bubblewrap`, Node/npm/npx, and harness
CLIs). Local-dev still source-mounts this repo for code, so rebuilding is only
needed when image dependencies change.

```bash
# LOCAL → MicroK8s registry. From the fork root:
docker build -f deploy/docker/Dockerfile \
  -t localhost:32000/bytedesk-omnigent-server:local .
docker push localhost:32000/bytedesk-omnigent-server:local

docker build -f deploy/docker/Dockerfile --target host \
  -t localhost:32000/bytedesk-omnigent-host:local .
docker push localhost:32000/bytedesk-omnigent-host:local

# PROD is built by TeamCity to registry.prod.bytedesk.ai/bytedesk/* — see "Productionize".
```
> The base manifests reference `localhost:32000/bytedesk-omnigent-server:local`
> and `localhost:32000/bytedesk-omnigent-host:local`. The local-dev overlay
> source-mounts the repo over `/build`, so images are dependency bases rather
> than the code delivery mechanism.

## Local deploy + smoke

```bash
# 1. secrets
cp k8s/secret.example.yaml k8s/secret.yaml   # fill POSTGRES_PASSWORD + DATABASE_URL + Option B creds
kubectl apply -f k8s/secret.yaml

# 2. deploy the bundle (namespace bytedesk already exists from the platform)
kubectl apply -k k8s/

# 3. smoke (control-plane + prints the agent-run commands)
./smoke.sh
```

## NATS management UI

The bundle includes NUI, a ClusterIP-only web console for the consolidated
`omnigent-nats` JetStream instance. It preloads an in-cluster CLI context for
`nats://omnigent-nats:4222`.

```bash
kubectl -n bytedesk port-forward svc/omnigent-nats-ui 31311:31311
```

Open `http://127.0.0.1:31311`. Keep it internal; the UI can inspect and mutate
streams, KV buckets, and Object Store data.

The local-dev overlay also exposes the console through the local ingress at
`http://nats-ui.dev.bytedesk.localhost`.

**Smoke ladder** (each rung proves more):
1. `smoke.sh` green → server + Postgres + health OK.
2. Laptop one-shot `omnigent run deploy/bytedesk/agents/bytedesk-engineer --server http://localhost:18000 -m "..."` → **Codex inference works** (Option B).
3. `omnigent run deploy/bytedesk/agents/bytedesk-orchestrator ...` → **delegation works** on the gateway alone.
4. In-cluster `omnigent-runner` Deployment registers as a host (verify the host/auth handshake — alpha).

## Agentic Inbox MCP for persona agents

Persona agent email access is persisted through Omnigent's template-agent image
API, not by hand-editing seed YAML. The image API rebuilds the agent bundle,
stores it under the content-addressed artifact store, warm-swaps the cache, and
marks the row `sot_tier=migrated` so startup seeding will not overwrite it.

Prerequisites:

- The Agentic Inbox dev Worker is reachable at `https://inbox.agents.dev.bytedesk.ai/mcp`.
- The Cloudflare Access app for that hostname has a service-token policy for
  Omnigent.
- The ByteDesk Agent Configuration Infisical project has a `/agentic-inbox`
  folder in the correct environment. For dev this is env `development`; prod
  uses env `production`.
- The `/agentic-inbox` folder contains `AGENTIC_INBOX_MCP_URL`,
  `AGENTIC_INBOX_EMAIL_DOMAIN`, `AGENTIC_INBOX_CF_ACCESS_CLIENT_ID`, and
  `AGENTIC_INBOX_CF_ACCESS_CLIENT_SECRET`. It also contains
  `OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET`, the shared HMAC secret the Omnigent
  server uses to verify Agentic Inbox `email.received` webhooks. The Infisical
  operator hydrates these into the `agentic-inbox-config-secrets` Kubernetes
  Secret.
- The Agentic Inbox Worker deployment includes
  `OMNIGENT_AGENTIC_INBOX_WEBHOOK_URL`, e.g.
  `https://omnigent.dev.bytedesk.ai/v1/agentic-inbox/events` for development.
  The matching `OMNIGENT_AGENTIC_INBOX_WEBHOOK_SECRET` is configured as a Worker
  secret with the same value as Infisical.

Apply or re-apply the persisted image update:

```bash
OMNIGENT_USERNAME=admin \
OMNIGENT_PASSWORD='<admin-password>' \
python3 scripts/bytedesk/apply_agentic_inbox.py
```

The updater filters `GET /v1/agents` to persona agents only
(`display_name` present and `workflow != true`), sets `params.email` and
`params.mailboxId`, adds an inline `tools.agentic-inbox` MCP entry whose URL and
Cloudflare Access headers resolve from `${AGENTIC_INBOX_*}` env vars, and
appends a prompt note instructing the agent to use its own mailbox ID.

## Productionize (Fleet/GitOps/TeamCity — platform-repo side)

The fork half is done (`fleet/production/kustomization.yaml`). To make prod
reconcile it, mirror OpenClaw in **`bytedesk-platform`** (one Jira task, one
worktree — touches the release pipeline, so review before it goes live):

1. **Register the Fleet GitRepo** — add a `bytedesk-omnigent-production` `GitRepo`
   to `infra/gitops/fleet/bytedesk-delivery-gitrepos.yaml` (copy the
   `bytedesk-openclaw-production` block; `paths: [deploy/bytedesk/fleet/production]`).
2. **Dockerfile** — `infra/docker/bytedesk-omnigent/Dockerfile` (builds the fork
   image + bakes `deploy/bytedesk/agents/` to **`/build/deploy/bytedesk/agents`**),
   mirroring `infra/docker/bytedesk-openclaw/Dockerfile`. The bake path MUST be
   `/build/...` so the `OMNIGENT_BUILTIN_AGENT_DIRS` env in `k8s/server.yaml`
   (which seeds the agent picker) resolves identically in prod and local-dev.
3. **TeamCity build** — `.teamcity/scripts/build-omnigent.sh` (clone fork → buildx →
   push `registry.prod.bytedesk.ai/bytedesk/bytedesk-omnigent:<tag>`), mirroring
   `build-openclaw.sh`; call it from `release-cut.sh`.
4. **TeamCity fleet bump** — `.teamcity/scripts/fleet-update-omnigent.sh` (sed-bump
   the `# pipeline-managed` `newTag` in this overlay + force Fleet sync), mirroring
   `fleet-update-openclaw.sh`; call it from `release-cut-finalize.sh`.
5. **Prod secrets** — provision `omnigent-secrets` + `omnigent-runner-secrets` in
   the `bytedesk` namespace via Infisical-operator/SOPS (mirror
   `bytedesk-openclaw-secrets`). The Fleet bundle deliberately does NOT carry them.

Result: every platform release builds omnigent at the same version, bumps its tag,
and Fleet reconciles it — identical to OpenClaw.

## Teardown

```bash
kubectl delete -k k8s/                 # local
kubectl delete pvc -n bytedesk -l app.kubernetes.io/part-of=bytedesk-omnigent
# prod: remove the bytedesk-omnigent-production GitRepo
```

## What's verified vs needs your input

- **Verified against source:** server is control-plane-only + needs an external
  runner; env contract (`DATABASE_URL`, `ARTIFACT_DIR`, `OMNIGENT_AUTH_ENABLED`);
  the `openai-agents` harness env vars; agent-bundle YAML format; the OpenClaw
  Fleet/TeamCity pattern to mirror; `bytedesk` ns has ample headroom (no quota).
- **Needs your input:** the real Codex gateway URL + key (Infisical); whether to
  use Option B vs C; building/pushing the image; confirming the in-cluster
  runner's host/auth handshake on this alpha.
