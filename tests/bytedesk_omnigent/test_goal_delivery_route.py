"""Route tests for goal-delivery webhook idempotent receipt (BDP-2553, ADR-0154/0009).

The ingress wires the durable idempotency plane: a redelivery of an already-applied
event is short-circuited (200 ``duplicate``), while an unmatched event still 404s
and is NOT claimed so the source can retry once its goal exists (BDP-1419).
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.routes.goal_delivery import create_goal_delivery_router


class _StubAdapter:
    @staticmethod
    def verify(_raw: bytes, _headers: dict, _secret: str) -> bool:
        return True


class _ConvStore:
    def __init__(self, location: str) -> None:
        self.storage_location = location


def _setup(tmp_path, monkeypatch) -> tuple[TestClient, str]:
    location = f"sqlite:///{tmp_path / 'goals.db'}"
    # get_goal_store + get_idempotency_store both derive their URI from here.
    monkeypatch.setattr(
        "omnigent.runtime.get_conversation_store", lambda: _ConvStore(location)
    )
    monkeypatch.setattr("bytedesk_omnigent.ingress.resolve_secret", lambda _s: "secret")
    monkeypatch.setattr(
        "bytedesk_omnigent.ingress.resolve_webhook_adapter", lambda _s: _StubAdapter()
    )
    app = FastAPI()
    app.include_router(create_goal_delivery_router(), prefix="/v1")
    return TestClient(app), location


def _seed_goal(location: str) -> None:
    SqlAlchemyGoalStore(location).create_goal(
        title="Goal BDP-1234",
        source="goal-planner",
        payload={
            "jiraEpicKey": "BDP-1234",
            "hierarchy": {
                "milestones": [
                    {
                        "taskKey": "BDP-1235",
                        "title": "API",
                        "status": "in_progress",
                        "jiraDone": False,
                        "prMerged": False,
                        "steps": [],
                        "delivery": {
                            "jira": {"taskKey": "BDP-1235"},
                            "github": {
                                "repo": "ByteDeskAI/bytedesk-platform",
                                "branch": "feature/BDP-1235-x",
                                "baseBranch": "develop",
                                "prNumber": None,
                            },
                        },
                    }
                ]
            },
        },
        now=100,
    )


def _github_body(sha: str, head: str = "feature/BDP-1235-x") -> bytes:
    return json.dumps(
        {
            "action": "closed",
            "pull_request": {
                "number": 987,
                "merged": True,
                "head": {"ref": head},
                "base": {"ref": "develop"},
                "merge_commit_sha": sha,
            },
            "repository": {"full_name": "ByteDeskAI/bytedesk-platform"},
        }
    ).encode()


def test_duplicate_github_delivery_is_skipped(tmp_path, monkeypatch) -> None:
    client, location = _setup(tmp_path, monkeypatch)
    _seed_goal(location)
    body = _github_body("sha-abc")

    first = client.post("/v1/goal-delivery/github", content=body)
    assert first.status_code == 202
    assert first.json()["status"] == "projected"
    assert first.json()["milestoneStatus"] == "awaiting_jira"

    # Redelivery of the same merge_commit_sha is short-circuited.
    second = client.post("/v1/goal-delivery/github", content=body)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


def test_unmatched_delivery_is_not_claimed_and_retryable(tmp_path, monkeypatch) -> None:
    client, location = _setup(tmp_path, monkeypatch)
    _seed_goal(location)
    body = _github_body("sha-zzz", head="feature/unknown")  # matches no milestone

    first = client.post("/v1/goal-delivery/github", content=body)
    assert first.status_code == 404
    assert first.json()["status"] == "no_match"

    # A no-match is never claimed → the redelivery still attempts (not "duplicate").
    second = client.post("/v1/goal-delivery/github", content=body)
    assert second.status_code == 404
    assert second.json()["status"] == "no_match"


# ── ADR-0155 cutover flag (BDP-2565): default OFF = no behavior change ──────────


def _boom_ingest(**_kwargs):  # pragma: no cover - only invoked on regression
    raise AssertionError("ingest() must not run while the cutover flag is OFF")


def test_flag_off_by_default_never_calls_pipeline(tmp_path, monkeypatch) -> None:
    """Default flag state (unset → off) leaves the legacy projector path intact."""
    client, location = _setup(tmp_path, monkeypatch)
    _seed_goal(location)
    # If the route ever routed to the pipeline while off, this ingest would blow up.
    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _boom_ingest)

    resp = client.post("/v1/goal-delivery/github", content=_github_body("sha-abc"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "projected"
    assert resp.json()["milestoneStatus"] == "awaiting_jira"


def test_flag_on_routes_through_pipeline(tmp_path, monkeypatch) -> None:
    """Flag ON → the verified payload is handed to ``ingest`` on the goal channel."""
    from bytedesk_omnigent.inbound.pipeline import IngestResult

    client, _location = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.flags.evaluate_inbound_flag", _async_true
    )

    calls: dict = {}

    def _fake_ingest(**kwargs):
        calls.update(kwargs)
        return IngestResult(
            status="projected", http_status=202, idempotency_key="k-1",
            event_type="pull_request.merged",
        )

    monkeypatch.setattr("bytedesk_omnigent.inbound.pipeline.ingest", _fake_ingest)
    monkeypatch.setattr(
        "bytedesk_omnigent.inbound.store.get_inbound_event_store", lambda: object()
    )
    monkeypatch.setattr("bytedesk_omnigent.inbound.processors.all_processors", list)

    resp = client.post("/v1/goal-delivery/github", content=_github_body("sha-abc"))

    assert resp.status_code == 202
    assert resp.json()["status"] == "projected"
    assert resp.json()["idempotencyKey"] == "k-1"
    assert calls["channel"] == "goal-delivery"
    assert calls["source"] == "github"
    assert calls["raw_payload"]["pull_request"]["merge_commit_sha"] == "sha-abc"


async def _async_true(*_args, **_kwargs) -> bool:
    return True
