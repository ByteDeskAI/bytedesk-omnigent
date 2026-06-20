# Omnigent autonomous feature loop iteration 31

## Capability shipped

Added a built-in GitLab webhook ingress adapter for Omnigent's durable signal ingress.

GitLab signs webhook delivery with a shared secret token in `X-Gitlab-Token` and names the event in `X-Gitlab-Event`. Omnigent's ingress adapter registry previously had a GitHub-style HMAC default plus a deploy-time extension seam, but GitLab required every deployment to hand-register the same token/event mapping before agents could bind GitLab merge request, pipeline, issue, or push hooks to parked workflows.

This iteration registers `gitlab` as a first-class source adapter:

- verifies `X-Gitlab-Token` with constant-time comparison against the resolved ingress secret
- routes `X-Gitlab-Event` as the binding match key
- falls back to `*` for catch-all bindings when GitLab omits an event header
- composes with the existing secret resolver and durable signal bus without changing route behavior or persistence schema

## Prior loop awareness

Before selecting the feature, I inspected open loop PRs targeting `develop`:

- iteration 1: integration capability catalog
- iterations 2-4: external work intake / workflow plan / connected app manifest
- iterations 5-12: Slack, Stripe, GitHub, JSON payload, Microsoft Teams, Linear, Shopify webhook adapters
- iterations 13-21: webhook binding management, event route compiler, secret readiness, OAuth state/authorize, replay/handoff/activation compilers
- iteration 22: integration workflow harness compiler
- iterations 23-30: Discord, Trello, Zendesk, Asana, HubSpot, Jira, Intercom, webhook adapter manifest

GitLab was not covered by the open loop PR set and does not duplicate those source adapters. It builds on the same integration-catalog direction by making another high-value engineering-system event source available through the deterministic webhook ingress seam.

## Implementation details

Changed files:

- `bytedesk_omnigent/ingress.py`
  - added `GitLabWebhookAdapter`
  - registered `gitlab` in `_build_webhook_adapter_registry()` alongside the GitHub default
- `tests/ingress/test_ingress.py`
  - added coverage proving `resolve_webhook_adapter("gitlab")` returns the GitLab adapter
  - verifies token success/failure, missing token failure, event match-key extraction, and catch-all fallback

The change is surgical: no migrations, no secrets, no route rewiring, and no changes to existing GitHub/default adapter semantics.

## Business case

GitLab is a common source of engineering work signals: merge request reviews, CI/CD pipeline transitions, issue events, release tags, and deployment notifications. Native support lets ByteDesk / Omnigent customers connect GitLab projects to autonomous agents with only:

1. an ingress secret configured as `OMNIGENT_INGRESS_SECRET_GITLAB`
2. a webhook binding such as `source=gitlab, match_key="Pipeline Hook"`
3. a parked workflow waiting on the corresponding durable signal

That reduces connector setup friction, broadens Omnigent's third-party integration story beyond GitHub-centered teams, and helps sell autonomous delivery/release agents into organizations standardized on GitLab.

## Future unlocks

- Add a catalog entry once the integration capability catalog lands in `develop`.
- Add a manifest row once iteration 30's webhook adapter manifest merges.
- Add optional GitLab event normalization so match keys can use stable slugs like `pipeline`, `merge_request`, or `issue` in addition to raw GitLab header names.
- Add UI scaffolding for GitLab webhook setup instructions and binding templates.

## Test plan

TDD evidence:

1. Wrote `test_gitlab_adapter_verifies_shared_token_and_reads_event` first.
2. Ran the targeted test and observed the expected import failure because `GitLabWebhookAdapter` did not exist.
3. Implemented the adapter and registry registration.
4. Re-ran the targeted test and observed it pass.

Verification commands run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py::test_gitlab_adapter_verifies_shared_token_and_reads_event -q
```

Result: `1 passed, 1 warning` (pre-existing `tests/known_failures.yaml` unmatched-entry warning).

Additional verification commands run:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/ingress/test_ingress.py -q
/home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py
git diff --check
```

Results:

- `tests/ingress/test_ingress.py`: `8 passed, 1 warning` (same pre-existing known-failures warning)
- `ruff check`: `All checks passed!`
- `git diff --check`: no whitespace errors
