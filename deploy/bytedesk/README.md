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
│    • accepts runner WS tunnels at /v1/runner/tunnel                 │
│    • state → omnigent-postgres (dedicated, self-contained)         │
│            ▲ WS tunnel                                              │
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

## Build the image

One image (`bytedesk-omnigent`) serves both server and runner (it ships the
`omnigent` package + CLI + harnesses; the runner image also bakes the bundles in
`agents/`).

```bash
# LOCAL → MicroK8s registry. From the fork root:
docker build -f deploy/docker/Dockerfile -t localhost:32000/bytedesk-omnigent:local .
docker push localhost:32000/bytedesk-omnigent:local
# (PROD is built by TeamCity to registry.prod.bytedesk.ai/bytedesk/bytedesk-omnigent — see "Productionize".)
```
> The base manifests reference `localhost:32000/bytedesk-omnigent:local`. If the
> upstream `ghcr.io/omnigent-ai/omnigent-server` package is public you can use it
> for the *server* and only build a runner image that bakes the bundles — but one
> shared image is simpler.

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

**Smoke ladder** (each rung proves more):
1. `smoke.sh` green → server + Postgres + health OK.
2. Laptop one-shot `omnigent run deploy/bytedesk/agents/bytedesk-engineer --server http://localhost:18000 -m "..."` → **Codex inference works** (Option B).
3. `omnigent run deploy/bytedesk/agents/bytedesk-orchestrator ...` → **delegation works** on the gateway alone.
4. In-cluster `omnigent-runner` Deployment registers as a host (verify the host/auth handshake — alpha).

## Productionize (Fleet/GitOps/TeamCity — platform-repo side)

The fork half is done (`fleet/production/kustomization.yaml`). To make prod
reconcile it, mirror OpenClaw in **`bytedesk-platform`** (one Jira task, one
worktree — touches the release pipeline, so review before it goes live):

1. **Register the Fleet GitRepo** — add a `bytedesk-omnigent-production` `GitRepo`
   to `infra/gitops/fleet/bytedesk-delivery-gitrepos.yaml` (copy the
   `bytedesk-openclaw-production` block; `paths: [deploy/bytedesk/fleet/production]`).
2. **Dockerfile** — `infra/docker/bytedesk-omnigent/Dockerfile` (builds the fork
   image + bakes `deploy/bytedesk/agents/`), mirroring `infra/docker/bytedesk-openclaw/Dockerfile`.
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
