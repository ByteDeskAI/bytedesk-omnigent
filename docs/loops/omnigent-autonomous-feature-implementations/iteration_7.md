# Omnigent autonomous feature loop — iteration 7

## Capability implemented

Implemented first-party GitHub webhook event routing for Omnigent's signed ingress adapter.

New behavior:

- GitHub webhooks signed with the standard `X-Hub-Signature-256: sha256=<hmac>` header can route directly through `POST /v1/ingress/github`.
- The default `GitHubWebhookAdapter` now reads GitHub's standard `X-GitHub-Event` event-name header and uses it as the Omnigent binding match key.
- The existing `X-Omnigent-Event` compatibility shim remains supported for internal/custom senders.
- Existing HMAC verification, secret resolution, catch-all `*` binding fallback, and durable signal-bus delivery semantics are preserved.

This means a binding such as `(source="github", match_key="issues") -> signal_id="github:issue:123"` can wake a parked agent session from a real GitHub Issues webhook without requiring ByteDesk Platform to rewrite headers first.

## Prior loop awareness

Before selecting this iteration, I inspected open loop PRs in `ByteDeskAI/bytedesk-omnigent` whose head branches matched `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`:

- PR #96 / iteration 1: integration capability catalog (`/v1/integration-capabilities`).
- PR #98 / iteration 2: external work-item intake.
- PR #99 / iteration 3: deterministic integration workflow plan compiler.
- PR #100 / iteration 4: connected-app manifest compiler.
- PR #101 / iteration 5: Slack webhook ingress adapter.
- PR #102 / iteration 6: Stripe webhook ingress adapter.

This iteration avoids duplicating those open PRs. It builds on the catalog's GitHub integration direction and the existing ingress adapter seam by making GitHub's native webhook header contract work out of the box.

## Business case

GitHub is one of the highest-value sources of autonomous engineering work: issues, pull requests, discussions, check suites, release events, deployments, and security alerts all originate there. Direct GitHub webhook routing lets ByteDesk Omnigent agents react to repository events where engineering work actually happens:

- triage newly opened issues into Omnigent tasks,
- wake code-review or release agents from pull request events,
- coordinate incident/remediation agents from workflow or security events,
- bridge ByteDesk Platform connected-app setup to a deterministic Omnigent signal target.

Removing the need for a custom header translation layer lowers connector implementation cost and makes GitHub Apps/OAuth installations easier to productize in ByteDesk Platform.

## Future unlocks

- Register GitHub App installation manifests that provision `OMNIGENT_INGRESS_SECRET_GITHUB`, webhook events, and callback URLs automatically.
- Add optional action-aware matching such as `issues.opened`, `pull_request.synchronize`, and `check_suite.completed` once the open body-aware adapter seam lands.
- Feed GitHub event payloads into external work-item intake to create idempotent Omnigent tasks for issues and pull requests.
- Add GitHub writeback actions for comments, labels, status checks, and deployment updates behind approval gates.
- Surface GitHub repository bindings in ByteDesk Platform so admins can connect repos to agent teams without manual binding rows.

## Verification

TDD red/green was used:

- RED: `uv run --extra dev pytest tests/ingress/test_ingress.py::test_github_default_adapter_reads_real_github_event_header tests/ingress/test_ingress.py::test_process_inbound_delivers_real_github_event_header -q` failed because `X-GitHub-Event` resolved to `*` and the `github/issues` binding was not found.
- GREEN: the same targeted test command passed after updating `GitHubWebhookAdapter.match_key`.
- Full targeted ingress coverage passed: `uv run --extra dev pytest tests/ingress/test_ingress.py tests/ingress/test_secret_resolver_seam.py -q`.
- Changed Python files passed lint: `uv run --extra dev ruff check bytedesk_omnigent/ingress.py tests/ingress/test_ingress.py`.
