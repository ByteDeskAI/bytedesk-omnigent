"""Tests for the GoalDeliveryProjector (ADR-0154, BDP-2542).

The two-key milestone gate: a milestone (Jira Task) is ``done`` only when the
Jira Task is Done AND the linked GitHub PR is merged to the base branch. Order
does not matter; replays are no-ops; completing every milestone completes the
goal (Epic) and unlocks dependent milestones.
"""
from __future__ import annotations

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from bytedesk_omnigent.goals_delivery import (
    GithubPrEvent,
    GoalDeliveryProjector,
    JiraIssueEvent,
    compute_milestone_status,
    parse_github_pr_event,
    parse_jira_issue_event,
)


def _store(tmp_path) -> SqlAlchemyGoalStore:
    return SqlAlchemyGoalStore(f"sqlite:///{tmp_path / 'goals.db'}")


def _goal_payload(epic="BDP-1234", task="BDP-1235", *, branch="feature/BDP-1235-x", pr=None):
    return {
        "jiraEpicKey": epic,
        "hierarchy": {
            "milestones": [
                {
                    "taskKey": task,
                    "title": "Customer attach API",
                    "status": "in_progress",
                    "jiraDone": False,
                    "prMerged": False,
                    "steps": ["BDP-1236", "BDP-1237"],
                    "delivery": {
                        "jira": {"taskKey": task},
                        "github": {
                            "repo": "ByteDeskAI/bytedesk-platform",
                            "branch": branch,
                            "baseBranch": "develop",
                            "prNumber": pr,
                        },
                    },
                }
            ]
        },
    }


def _make_goal(store, **kw):
    payload = _goal_payload(**kw)
    return store.create_goal(
        title=f"Goal {payload['jiraEpicKey']}",
        source="goal-planner",
        payload=payload,
        now=100,
    )


def _milestone(store, goal_id, idx=0):
    goal = store.get_goal(goal_id=goal_id)
    return goal.payload["hierarchy"]["milestones"][idx]


# -- pure two-key gate -------------------------------------------------------
def test_compute_milestone_status_two_key_gate() -> None:
    assert compute_milestone_status(jira_done=False, pr_merged=False) == "pending"
    assert compute_milestone_status(jira_done=True, pr_merged=False) == "awaiting_pr"
    assert compute_milestone_status(jira_done=False, pr_merged=True) == "awaiting_jira"
    assert compute_milestone_status(jira_done=True, pr_merged=True) == "done"
    # current in_progress is preserved when neither key is set
    assert compute_milestone_status(jira_done=False, pr_merged=False, current="in_progress") == "in_progress"


# -- order independence ------------------------------------------------------
def test_github_then_jira_completes_milestone(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    goal = _make_goal(store)

    r1 = proj.apply_github_pr_merged(
        GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=987,
                      head_ref="feature/BDP-1235-x", base_ref="develop"),
        now=110,
    )
    assert r1.matched and r1.http_status == 202
    assert r1.milestone_status == "awaiting_jira"
    assert r1.milestone_completed is False
    # prNumber backfilled
    assert _milestone(store, goal.id)["delivery"]["github"]["prNumber"] == 987

    r2 = proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1235", issue_type="Task", status="Done",
                       status_category="done", parent_epic_key="BDP-1234"),
        now=120,
    )
    assert r2.matched and r2.milestone_status == "done"
    assert r2.milestone_completed is True
    assert r2.goal_completed is True
    assert store.get_goal(goal_id=goal.id).status == "done"


def test_jira_then_github_completes_milestone(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    goal = _make_goal(store)

    r1 = proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1235", issue_type="Task", status="Done",
                       status_category="done", parent_epic_key="BDP-1234"),
        now=110,
    )
    assert r1.matched and r1.milestone_status == "awaiting_pr"
    assert r1.milestone_completed is False

    r2 = proj.apply_github_pr_merged(
        GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=987,
                      head_ref="feature/BDP-1235-x", base_ref="develop"),
        now=120,
    )
    assert r2.milestone_completed is True and r2.goal_completed is True


# -- idempotency -------------------------------------------------------------
def test_replayed_pr_merge_is_noop(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    event = GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=987,
                          head_ref="feature/BDP-1235-x", base_ref="develop")
    first = proj.apply_github_pr_merged(event, now=110)
    second = proj.apply_github_pr_merged(event, now=111)
    assert first.matched and second.matched
    # second is a matched no-op: still awaiting_jira, never re-fires completion
    assert second.milestone_status == "awaiting_jira"
    assert second.milestone_completed is False


def test_replayed_completion_fires_once(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1235", issue_type="Task", status="Done",
                       status_category="done"), now=110)
    event = GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=987,
                          head_ref="feature/BDP-1235-x", base_ref="develop")
    first = proj.apply_github_pr_merged(event, now=120)
    second = proj.apply_github_pr_merged(event, now=121)
    assert first.milestone_completed is True
    assert second.milestone_completed is False  # exactly once


# -- no match → 404 ----------------------------------------------------------
def test_unmatched_github_event_returns_404(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    r = proj.apply_github_pr_merged(
        GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=1,
                      head_ref="feature/unknown", base_ref="develop"), now=110)
    assert r.matched is False and r.http_status == 404


def test_pr_to_wrong_base_does_not_match(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    r = proj.apply_github_pr_merged(
        GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=987,
                      head_ref="feature/BDP-1235-x", base_ref="main"), now=110)
    assert r.matched is False and r.http_status == 404


def test_unmatched_jira_event_returns_404(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    r = proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-9999", issue_type="Task", status="Done",
                       status_category="done"), now=110)
    assert r.matched is False and r.http_status == 404


# -- subtask is progress only, never gates -----------------------------------
def test_subtask_done_is_progress_only(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    goal = _make_goal(store)
    r = proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1236", issue_type="Subtask", status="Done",
                       status_category="done", parent_epic_key="BDP-1234"), now=110)
    assert r.matched is True
    assert r.milestone_completed is False
    m = _milestone(store, goal.id)
    assert "BDP-1236" in m.get("stepsDone", [])
    assert m["status"] != "done"


# -- epic event is informational ---------------------------------------------
def test_epic_event_is_informational_match(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    r = proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1234", issue_type="Epic", status="Done",
                       status_category="done"), now=110)
    assert r.matched is True
    assert r.milestone_completed is False and r.goal_completed is False


# -- cross-goal milestone dependency unlock ----------------------------------
def test_completed_milestone_unlocks_dependent_goal(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    upstream = _make_goal(store, epic="BDP-1234", task="BDP-1235")
    # A second goal that waits on the upstream milestone BDP-1235.
    downstream = store.create_goal(
        title="Downstream",
        source="goal-planner",
        payload=_goal_payload(epic="BDP-2000", task="BDP-2001",
                              branch="feature/BDP-2001-y"),
        dependencies=[{"kind": "milestone", "label": "Customer attach API", "ref": "BDP-1235"}],
        now=100,
    )
    assert store.get_goal(goal_id=downstream.id).activation_state == "waiting"

    # Complete the upstream milestone (both keys).
    proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1235", issue_type="Task", status="Done",
                       status_category="done"), now=110)
    proj.apply_github_pr_merged(
        GithubPrEvent(repo="ByteDeskAI/bytedesk-platform", pr_number=987,
                      head_ref="feature/BDP-1235-x", base_ref="develop"), now=111)

    refreshed = store.get_goal(goal_id=downstream.id)
    assert refreshed.dependencies[0].status == "satisfied"
    assert refreshed.activation_state == "ready"
    _ = upstream


# -- webhook fixture parsing (P2/P3) -----------------------------------------
def test_parse_github_merged_pr_fixture() -> None:
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 987,
            "merged": True,
            "head": {"ref": "feature/BDP-1235-x"},
            "base": {"ref": "develop"},
            "merge_commit_sha": "deadbeef",
        },
        "repository": {"full_name": "ByteDeskAI/bytedesk-platform"},
    }
    event = parse_github_pr_event(payload)
    assert event == GithubPrEvent(
        repo="ByteDeskAI/bytedesk-platform", pr_number=987,
        head_ref="feature/BDP-1235-x", base_ref="develop", merge_commit_sha="deadbeef")


def test_parse_github_non_merge_is_ignored() -> None:
    assert parse_github_pr_event({"action": "opened", "pull_request": {"number": 1}}) is None
    assert parse_github_pr_event(
        {"action": "closed", "pull_request": {"number": 1, "merged": False}}
    ) is None


def test_parse_jira_issue_updated_fixture() -> None:
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "BDP-1235",
            "fields": {
                "issuetype": {"name": "Task"},
                "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "parent": {"key": "BDP-1234"},
            },
        },
    }
    event = parse_jira_issue_event(payload, webhook_identifier="wh-1")
    assert event == JiraIssueEvent(
        issue_key="BDP-1235", issue_type="Task", status="Done",
        status_category="done", parent_epic_key="BDP-1234", webhook_identifier="wh-1")


def test_github_fixture_drives_projector_end_to_end(tmp_path) -> None:
    store = _store(tmp_path)
    proj = GoalDeliveryProjector(store)
    _make_goal(store)
    proj.apply_jira_issue_updated(
        JiraIssueEvent(issue_key="BDP-1235", issue_type="Task", status="Done",
                       status_category="done"), now=110)
    event = parse_github_pr_event({
        "action": "closed",
        "pull_request": {"number": 987, "merged": True,
                         "head": {"ref": "feature/BDP-1235-x"}, "base": {"ref": "develop"}},
        "repository": {"full_name": "ByteDeskAI/bytedesk-platform"},
    })
    result = proj.apply_github_pr_merged(event, now=120)
    assert result.goal_completed is True
