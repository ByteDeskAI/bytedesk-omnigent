# Aggregate feature testing guide

This branch is an integration-only test vehicle for the autonomous feature loop PRs merged into PR #194. Do not merge it directly. Use it to exercise representative feature slices and decide which smaller batches should land.

## Quick smoke test

From the aggregate worktree:

```bash
cd /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.claude/worktrees/loop/omnigent-autonomous-feature-implementations/aggregate-test-all-open
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python scripts/aggregate_feature_smoke.py
```

Machine-readable mode:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python scripts/aggregate_feature_smoke.py --json
```

The smoke harness uses FastAPI `TestClient` against the ByteDesk extension routers. It does not require MicroK8s, a running Omnigent server, provider credentials, or external SaaS accounts.

## What the smoke covers

1. Extension wiring
   - `GET /v1/_ext/health`
   - Proves the ByteDesk extension routers can be discovered and mounted together.

2. Capability catalog
   - `GET /v1/integration-capabilities?limit=3`
   - Confirms the catalog is available and ordered by product priority.

3. Static route ordering
   - `GET /v1/integration-capabilities/bundles`
   - `GET /v1/integration-capabilities/recommendations?...`
   - Guards the important FastAPI ordering issue where static routes must be declared before `/integration-capabilities/{slug}`.

4. Capability artifacts
   - Marketplace listing
   - Verification matrix
   - Launch brief
   - Lifecycle plan
   - Tool contract
   - Confirms one catalog slug can compile multiple operator/product artifacts.

5. Readiness and evidence assessment
   - `POST /v1/integration-capabilities/google-workspace-operator/readiness-assessment`
   - `POST /v1/integration-capabilities/google-workspace-operator/evidence-assessment`
   - Confirms supplied rollout evidence is scored against capability verification gates.

6. Ingress adapters
   - `GET /v1/ingress/adapters`
   - Direct GitHub HMAC + match-key smoke.
   - Confirms the aggregate provider registry is populated and provider-specific matching works.

7. Webhook probe generation
   - `POST /v1/integration-probes/webhook`
   - Produces a copy/pasteable signed `curl` command and expected status map for operator webhook setup.

8. Approval planning
   - `POST /v1/integration-approval-plans/compile`
   - Confirms requested scopes/writeback operations compile to approval gates and risk level.

## Manual examples

Catalog:

```bash
python scripts/aggregate_feature_smoke.py --json | jq '.checks[] | select(.name == "capability catalog")'
```

Webhook setup probe:

```bash
python scripts/aggregate_feature_smoke.py --json | jq '.checks[] | select(.name == "webhook probe").detail'
```

Provider adapter list:

```bash
python scripts/aggregate_feature_smoke.py --json | jq '.checks[] | select(.name == "ingress adapters").detail.sample_sources'
```

## Local server path

For full local-dev testing through `omnigent.bytedesk.localhost`, remap local-dev to this aggregate worktree first, then roll the Omnigent deployments. See the `omnigent-local-development` Hermes skill for the canonical MicroK8s checks. The important evidence is:

- hostPath mount points at this aggregate worktree
- `PYTHONPATH=/build`
- `/build` first on `sys.path`
- Omnigent server pod has rolled after remapping

Once mapped, the same route examples become real HTTP calls against:

```text
http://omnigent.bytedesk.localhost/v1/...
```

## Current known result

Last run in this worktree:

- `scripts/aggregate_feature_smoke.py --json`: passed 8 checks
- broader focused verification: `258 passed, 1 warning`
- warning source: stale entries in `tests/known_failures.yaml`
