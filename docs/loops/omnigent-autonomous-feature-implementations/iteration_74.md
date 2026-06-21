# Autonomous feature loop iteration 74 — platform integration gap analysis API

Branch: `feature/loop/omnigent-autonomous-feature-implementations/iteration_74`

## Capability delivered

Iteration 74 exposes the existing deterministic integration capability gap analysis as a ByteDesk Platform-facing API:

- `POST /v1/integration-capability-gaps/analyze`

The endpoint accepts platform-supplied evidence for catalog capabilities that are already implemented or already in flight, then returns the same JSON-ready `integration_capability_gap_report` used by autonomous planning code. This gives ByteDesk Platform, Office UI, and future planning agents a safe way to ask: "what integration capability should Omnigent build next after accounting for live product evidence and open work?"

## Prior loop awareness

Before choosing this capability, I inspected open loop PRs matching `feature/loop/omnigent-autonomous-feature-implementations/iteration_*`. Open work already covers iterations 2 through 73, including webhook adapters, OAuth plans, activation gates, replay/rollback/rate-limit plans, readiness assessments, marketplace listings, verification matrices, recommendation compilers, evidence packets, and tenant routing manifests.

This iteration intentionally avoids duplicating those deliverables. It builds on the catalog and gap-analysis foundation by turning the gap report into an authenticated route that ByteDesk Platform can call with its own installed/open-work evidence.

The feature also follows the future unlock noted in `docs/loops/omnigent-autonomous-feature-implementations/iteration_57.md`: expose the gap report through a route once Platform has a trusted evidence source.

## Implementation description

Changed files:

- `bytedesk_omnigent/routes/integration_capabilities.py`
  - Adds `POST /integration-capability-gaps/analyze` to the existing integration router.
  - Keeps the route behind the same `require_user` behavior as the catalog endpoints: authenticated in multi-user mode, open in single-user/local mode.
  - Accepts `implemented_slugs` and `open_signals` in the request body.
  - Filters unknown implemented slugs through the existing analyzer rather than treating user/platform input as authoritative.
  - Returns the analyzer's stable JSON shape without secrets or live network calls.

- `bytedesk_omnigent/integration_gap_analysis.py`
  - Improves title matching for open-work signals by considering capability category and implementation-description tokens in addition to slug/name tokens.
  - This lets PR titles like `feat: add integration workflow harness compiler` resolve to the Archon-style workflow blueprint even when the caller does not provide an exact slug.

- `tests/bytedesk_omnigent/test_integration_gap_analysis.py`
  - Adds an API-level test showing ByteDesk Platform can submit implemented slugs and open PR signals, receive covered slugs, ignore unknown catalog entries, and get the next recommended integration capability.

## API example

Request:

```json
{
  "implemented_slugs": ["slack-command-center", "missing-catalog-entry"],
  "open_signals": [
    {
      "source": "pr#118",
      "title": "feat: add integration workflow harness compiler",
      "url": "https://github.com/ByteDeskAI/bytedesk-omnigent/pull/118"
    },
    {
      "slug": "github-engineering-copilot",
      "source": "branch",
      "title": "feature/loop/github-copilot"
    }
  ]
}
```

Response includes:

- `object: integration_capability_gap_report`
- `covered_slugs`
- `implemented_count`
- `open_work_count`
- `next_recommended_slug`
- prioritized `gaps`
- resolved `open_work`

## Business case

ByteDesk Platform needs to coordinate product strategy, operator awareness, and autonomous implementation loops without forcing every caller to reimplement catalog-diff logic. This endpoint turns Omnigent's integration strategy into a reusable product surface:

1. Platform UI can show which capabilities are already covered, which are in flight, and what should be prioritized next.
2. Autonomous loop supervisors can avoid duplicating open PR work before starting a new iteration.
3. Product leadership can compare roadmap coverage against catalog priority with deterministic, auditable output.
4. Tenant onboarding can eventually pass installed connectors and receive a ranked list of missing high-value integrations.

This directly advances Omnigent's mission as the coordination layer for autonomous agents integrated into third-party applications and ByteDesk Platform.

## Future unlocks

1. Populate `implemented_slugs` from actual extension/router discovery instead of caller-supplied evidence.
2. Populate `open_signals` automatically from GitHub PR metadata for ByteDeskAI/bytedesk-omnigent.
3. Add an Office UI integration strategy panel backed by this endpoint.
4. Add tenant-aware gap reports that compare installed integrations per workspace against catalog priority.
5. Feed the result into autonomous feature-loop selection so future iterations can pick the highest-value uncovered capability deterministically.

## Test plan

Targeted tests run from the managed worktree:

```bash
PYTHONPATH="$PWD" /home/ryan/Documents/GitHub/ByteDeskAI/bytedesk-omnigent/.venv/bin/python -m pytest tests/bytedesk_omnigent/test_integration_gap_analysis.py::test_integration_gap_analysis_route_compiles_platform_supplied_evidence -q
```

Expected TDD evidence:

- First run failed with HTTP 404 because the new route did not exist.
- After implementation, the targeted API test passed.

Additional verification for the final branch is documented in the PR after running the full targeted test file and `git diff --check`.
