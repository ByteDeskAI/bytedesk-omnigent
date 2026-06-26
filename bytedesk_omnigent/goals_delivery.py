"""GoalDeliveryProjector — webhook delivery → milestone/goal progress (ADR-0154).

A Jira **Epic** is a Goal (one row, ``payload.jiraEpicKey`` canonical); each Jira
**Task** is a milestone in ``payload.hierarchy.milestones``; each **Subtask** a
step. A milestone reaches ``done`` only under the **two-key gate** — the linked
Jira Task is Done AND the linked GitHub PR is merged to its ``baseBranch``. Order
does not matter; replays are no-ops (completion fires exactly once). Completing a
milestone satisfies dependent goals' ``milestone`` dependencies; completing every
milestone completes the goal (Epic).

Pure Adapter (ADR-0008) over :class:`SqlAlchemyGoalStore` — no new persistence
plane. Milestone state lives in ``goal.payload``; the store's ``update_goal`` /
``advance_goal`` / ``update_dependency`` emit the ``goal.changed`` deltas the UI
and triage router reconcile from. Unmatched events return 404 (never 2xx,
BDP-1419) so GitHub/Jira retry logs show the miss.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from bytedesk_omnigent.goals import SqlAlchemyGoalStore
from omnigent.db.utils import now_epoch

MILESTONE_STATUSES = ("pending", "in_progress", "awaiting_pr", "awaiting_jira", "done")
DEFAULT_BASE_BRANCH = "develop"


def compute_milestone_status(
    *, jira_done: bool, pr_merged: bool, current: str = "pending"
) -> str:
    """The two-key milestone gate (ADR-0154). ``done`` iff both keys are true."""
    if jira_done and pr_merged:
        return "done"
    if jira_done:
        return "awaiting_pr"
    if pr_merged:
        return "awaiting_jira"
    return "in_progress" if current == "in_progress" else "pending"


@dataclass(frozen=True)
class GithubPrEvent:
    """Normalized ``pull_request`` closed+merged event."""

    repo: str
    pr_number: int
    head_ref: str
    base_ref: str
    merge_commit_sha: str | None = None


@dataclass(frozen=True)
class JiraIssueEvent:
    """Normalized ``jira:issue_updated`` event."""

    issue_key: str
    issue_type: str  # Epic | Task | Subtask
    status: str
    status_category: str  # done | indeterminate | new
    parent_epic_key: str | None = None
    webhook_identifier: str | None = None


@dataclass(frozen=True)
class ProjectionResult:
    """Outcome of a delivery event — carries the HTTP status the ingress returns."""

    matched: bool
    http_status: int
    goal_id: str | None = None
    milestone_key: str | None = None
    milestone_status: str | None = None
    milestone_completed: bool = False
    goal_completed: bool = False
    detail: str | None = None


_NOT_MATCHED = ProjectionResult(
    matched=False, http_status=404, detail="no goal or milestone matched"
)


def _milestones(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    hierarchy = payload.get("hierarchy") or {}
    milestones = hierarchy.get("milestones")
    return milestones if isinstance(milestones, list) else []


def _github_spec(milestone: dict[str, Any]) -> dict[str, Any]:
    return (milestone.get("delivery") or {}).get("github") or {}


def _jira_task_key(milestone: dict[str, Any]) -> str | None:
    jira = (milestone.get("delivery") or {}).get("jira") or {}
    return jira.get("taskKey") or milestone.get("taskKey")


class GoalDeliveryProjector:
    """Map GitHub/Jira delivery events onto goal/milestone state (ADR-0154).

    ponytail: matching scans the goal backlog in-process (``list_goals``) — fine
    for the founder-scale backlog; add a payload index if the backlog grows past
    a few hundred open goals.
    """

    def __init__(self, store: SqlAlchemyGoalStore) -> None:
        self._store = store

    # -- GitHub: pull_request merged -------------------------------------
    def apply_github_pr_merged(
        self, event: GithubPrEvent, *, now: int | None = None
    ) -> ProjectionResult:
        now = now_epoch() if now is None else now
        for goal in self._store.list_goals(include_dependencies=True):
            for index, milestone in enumerate(_milestones(goal.payload)):
                if self._github_match(milestone, event):
                    return self._transition_milestone(
                        goal, index, now, set_pr=True, pr_number=event.pr_number
                    )
        # No milestone matched — try fine-grained github_pr dependencies.
        if self._satisfy_dependencies(
            lambda d: d.kind == "github_pr" and _ref_matches_pr(d.ref, event), now
        ):
            return ProjectionResult(
                matched=True, http_status=202, detail="github_pr dependency satisfied"
            )
        return _NOT_MATCHED

    # -- Jira: issue updated ---------------------------------------------
    def apply_jira_issue_updated(
        self, event: JiraIssueEvent, *, now: int | None = None
    ) -> ProjectionResult:
        now = now_epoch() if now is None else now
        is_done = event.status_category == "done"

        if event.issue_type == "Task":
            for goal in self._store.list_goals(include_dependencies=True):
                for index, milestone in enumerate(_milestones(goal.payload)):
                    if _jira_task_key(milestone) == event.issue_key:
                        return self._transition_milestone(
                            goal, index, now, set_jira=is_done
                        )
        elif event.issue_type == "Subtask":
            for goal in self._store.list_goals(include_dependencies=True):
                for index, milestone in enumerate(_milestones(goal.payload)):
                    if event.issue_key in (milestone.get("steps") or []):
                        return self._record_step(goal, index, event.issue_key, is_done, now)
        elif event.issue_type == "Epic":
            for goal in self._store.list_goals():
                if (goal.payload or {}).get("jiraEpicKey") == event.issue_key:
                    return ProjectionResult(
                        matched=True, http_status=202, goal_id=goal.id,
                        detail="epic transition is informational",
                    )

        # Fall back to fine-grained jira_issue dependencies.
        if is_done and self._satisfy_dependencies(
            lambda d: d.kind == "jira_issue" and d.ref == event.issue_key, now
        ):
            return ProjectionResult(
                matched=True, http_status=202, detail="jira_issue dependency satisfied"
            )
        return _NOT_MATCHED

    # -- internals -------------------------------------------------------
    def _github_match(self, milestone: dict[str, Any], event: GithubPrEvent) -> bool:
        gh = _github_spec(milestone)
        base = gh.get("baseBranch") or DEFAULT_BASE_BRANCH
        if event.base_ref and base and event.base_ref != base:
            return False
        repo = gh.get("repo")
        if repo and repo != event.repo:
            return False
        pr = gh.get("prNumber")
        if pr is not None and int(pr) == event.pr_number:
            return True
        branch = gh.get("branch")
        return bool(branch) and branch == event.head_ref

    def _transition_milestone(
        self,
        goal: Any,
        index: int,
        now: int,
        *,
        set_pr: bool | None = None,
        set_jira: bool | None = None,
        pr_number: int | None = None,
    ) -> ProjectionResult:
        captured: dict[str, Any] = {}

        def _mutate(payload: dict[str, Any]) -> None:
            milestones = _milestones(payload)
            if index >= len(milestones):
                # Milestone list shifted between the match read and the locked write.
                captured["missing"] = True
                return
            milestone = milestones[index]
            captured["was_done"] = milestone.get("status") == "done"
            if set_pr is not None:
                milestone["prMerged"] = bool(set_pr)
                if pr_number is not None:
                    milestone.setdefault("delivery", {}).setdefault("github", {})[
                        "prNumber"
                    ] = pr_number
            if set_jira is not None:
                milestone["jiraDone"] = bool(set_jira)
            new_status = compute_milestone_status(
                jira_done=bool(milestone.get("jiraDone")),
                pr_merged=bool(milestone.get("prMerged")),
                current=milestone.get("status", "pending"),
            )
            milestone["status"] = new_status
            captured["new_status"] = new_status
            captured["milestone_key"] = _jira_task_key(milestone)

        # Atomic read-modify-write under the store's write lock: the payload is
        # re-read inside the same transaction, so a concurrent PR-merged + Jira-Done
        # for this milestone can't lose a key (the two-key gate, BDP-2553 / ADR-0009).
        updated = self._store.mutate_payload(goal_id=goal.id, mutator=_mutate, now=now)
        if updated is None or captured.get("missing"):
            return _NOT_MATCHED

        new_status = captured["new_status"]
        milestone_key = captured["milestone_key"]
        milestone_completed = new_status == "done" and not captured["was_done"]
        goal_completed = False
        if milestone_completed:
            # Unlock dependent goals waiting on this milestone (two-key gate passed).
            self._satisfy_dependencies(
                lambda d: d.kind == "milestone" and d.ref == milestone_key, now
            )
            goal_completed = self._maybe_complete_goal(goal.id, updated.payload or {}, now)

        return ProjectionResult(
            matched=True,
            http_status=202,
            goal_id=goal.id,
            milestone_key=milestone_key,
            milestone_status=new_status,
            milestone_completed=milestone_completed,
            goal_completed=goal_completed,
        )

    def _record_step(
        self, goal: Any, index: int, step_key: str, is_done: bool, now: int
    ) -> ProjectionResult:
        captured: dict[str, Any] = {}

        def _mutate(payload: dict[str, Any]) -> None:
            milestones = _milestones(payload)
            if index >= len(milestones):
                captured["missing"] = True
                return
            milestone = milestones[index]
            done = list(milestone.get("stepsDone") or [])
            if is_done and step_key not in done:
                done.append(step_key)
            elif not is_done and step_key in done:
                done.remove(step_key)
            milestone["stepsDone"] = done
            captured["milestone_key"] = _jira_task_key(milestone)
            captured["milestone_status"] = milestone.get("status")

        # Atomic RMW so concurrent subtask updates can't drop a step (BDP-2553).
        updated = self._store.mutate_payload(goal_id=goal.id, mutator=_mutate, now=now)
        if updated is None or captured.get("missing"):
            return _NOT_MATCHED
        return ProjectionResult(
            matched=True,
            http_status=202,
            goal_id=goal.id,
            milestone_key=captured["milestone_key"],
            milestone_status=captured["milestone_status"],
            detail="step progress",
        )

    def _maybe_complete_goal(
        self, goal_id: str, payload: dict[str, Any], now: int
    ) -> bool:
        milestones = _milestones(payload)
        if not milestones or any(m.get("status") != "done" for m in milestones):
            return False
        current = self._store.get_goal(goal_id=goal_id, include_dependencies=False)
        if current is None or str(current.status) == "done":
            return False
        self._store.advance_goal(goal_id=goal_id, status="done", now=now)
        # Completing the Epic goal satisfies epic / goal dependencies elsewhere.
        epic_key = (payload or {}).get("jiraEpicKey")
        self._satisfy_dependencies(
            lambda d: (d.kind == "epic" and d.ref == epic_key)
            or (d.kind == "goal" and d.ref == goal_id),
            now,
        )
        return True

    def _satisfy_dependencies(self, predicate, now: int) -> int:
        """Mark every pending dependency matching *predicate* satisfied. Returns count."""
        count = 0
        for goal in self._store.list_goals(include_dependencies=True):
            for dep in goal.dependencies:
                if dep.status == "pending" and predicate(dep):
                    self._store.update_dependency(
                        goal_id=goal.id,
                        dependency_id=dep.id,
                        status="satisfied",
                        now=now,
                    )
                    count += 1
        return count


def normalize_delivery_contract(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make a Goals-Concierge draft projector-ready (ADR-0154 P4, BDP-2545).

    At plan approval each milestone in ``payload.hierarchy.milestones`` MUST carry
    both delivery fingerprints (Jira + GitHub) and an initial two-key gate state,
    so the GoalDeliveryProjector can later match webhooks against it. This fills
    the skeleton in place (idempotent): ``delivery.jira.taskKey`` defaults from
    ``taskKey``; ``delivery.github`` gets ``baseBranch``/``prNumber`` placeholders
    (``ship`` backfills ``branch``/``prNumber``); ``status``/``jiraDone``/
    ``prMerged``/``steps`` are initialized. A draft with no milestones is returned
    unchanged.
    """
    if not _milestones(payload):
        return payload
    out = copy.deepcopy(payload)
    for milestone in _milestones(out):
        task_key = milestone.get("taskKey") or _jira_task_key(milestone)
        delivery = milestone.setdefault("delivery", {})
        jira = delivery.setdefault("jira", {})
        if task_key and not jira.get("taskKey"):
            jira["taskKey"] = task_key
        github = delivery.setdefault("github", {})
        github.setdefault("baseBranch", DEFAULT_BASE_BRANCH)
        github.setdefault("branch", None)
        github.setdefault("prNumber", None)
        milestone.setdefault("status", "pending")
        milestone.setdefault("jiraDone", False)
        milestone.setdefault("prMerged", False)
        milestone.setdefault("steps", [])
    return out


def parse_github_pr_event(payload: dict[str, Any]) -> GithubPrEvent | None:
    """Normalize a GitHub ``pull_request`` webhook body to a merged-PR event.

    Returns ``None`` for any event that is not a *merged* close (PR opened,
    synchronized, or closed-without-merge) — the route acknowledges those as
    ignored rather than 404-ing.
    """
    if payload.get("action") != "closed":
        return None
    pr = payload.get("pull_request") or {}
    if not pr.get("merged"):
        return None
    repo = (payload.get("repository") or {}).get("full_name") or ""
    return GithubPrEvent(
        repo=repo,
        pr_number=int(pr.get("number")),
        head_ref=(pr.get("head") or {}).get("ref") or "",
        base_ref=(pr.get("base") or {}).get("ref") or "",
        merge_commit_sha=pr.get("merge_commit_sha"),
    )


def parse_jira_issue_event(
    payload: dict[str, Any], *, webhook_identifier: str | None = None
) -> JiraIssueEvent | None:
    """Normalize a Jira ``jira:issue_updated`` webhook body to an issue event."""
    issue = payload.get("issue") or {}
    key = issue.get("key")
    if not key:
        return None
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    return JiraIssueEvent(
        issue_key=key,
        issue_type=(fields.get("issuetype") or {}).get("name") or "",
        status=status.get("name") or "",
        status_category=(status.get("statusCategory") or {}).get("key") or "",
        parent_epic_key=(fields.get("parent") or {}).get("key"),
        webhook_identifier=webhook_identifier,
    )


def _ref_matches_pr(ref: str | None, event: GithubPrEvent) -> bool:
    """A ``github_pr`` dependency ``ref`` — ``owner/repo#N`` or a branch name."""
    if not ref:
        return False
    if "#" in ref:
        repo, _, num = ref.partition("#")
        return repo == event.repo and num.strip() == str(event.pr_number)
    return ref == event.head_ref
