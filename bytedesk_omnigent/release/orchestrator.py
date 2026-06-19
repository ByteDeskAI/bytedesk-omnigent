"""Omnigent-native ops-release orchestrator (BDP-2258, ADR-0142).

Re-homes release-as-workflow off the platform Office Workflow Engine. The
orchestrator does three things and then gets out of the way:

1. **Park** a durable wait ``(session, "release:{version}")`` on the signal bus
   (BDP-2248) so the release run is suspended, restart-survivably, until the
   pipeline reports back.
2. **Bind** the TeamCity webhook ``(source="teamcity", match_key=signal_id)`` to
   that signal on the ingress binding store (BDP-2249), so ``build.finished`` →
   ingress → signal ``deliver`` resumes the parked run.
3. **Trigger** the pipeline through a :class:`ReleaseExecutor` seam.

Park-before-trigger is deliberate: registering the wait + binding *before* the
trigger means a fast callback always finds a pending wait (no lost-wakeup race).
Both registrations are idempotent, so a retried start is safe.

**The production-deploy boundary stays TeamCity-only — no break-glass.** The
orchestrator only *triggers* the pipeline; it never deploys. A replayed TeamCity
callback is idempotent at the bus (``ALREADY_RESOLVED`` → ingress 409), so a
duplicate ``build.finished`` can never wake the run twice / double-deploy. The
real TeamCity-triggering :class:`ReleaseExecutor` is a **founder-gated fill-in**
that must be validated against a live pipeline; until it is wired, the default
:class:`HumanGatedReleaseExecutor` refuses to trigger. The orchestrator itself is
pure + injectable (bus + binding store + executor passed in), so the
park/bind/trigger/idempotency contract is unit-proven without a live pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bytedesk_omnigent.bus.signal_bus import PendingWait

#: The ingress ``source`` TeamCity callbacks arrive under.
TEAMCITY_SOURCE = "teamcity"


def release_signal_id(version: str) -> str:
    """Deterministic signal id for a release ``version`` (park + binding agree)."""
    return f"release:{version}"


@dataclass(frozen=True)
class ReleaseTriggerResult:
    """What a :class:`ReleaseExecutor` reports after triggering the pipeline."""

    triggered: bool
    detail: str | None = None
    #: External pipeline handle (e.g. the TeamCity build id), when known.
    external_ref: str | None = None


@runtime_checkable
class ReleaseExecutor(Protocol):
    """The prod-touching seam: *trigger* a release pipeline (does NOT deploy here).

    The real implementation calls the human-gated TeamCity trigger — TeamCity is
    the only production deploy path, no break-glass (WORKFLOWS.md). It must be
    validated against a live pipeline before being wired. Tests inject a fake.
    """

    def trigger_release(
        self, *, version: str, session_id: str
    ) -> ReleaseTriggerResult: ...


@dataclass(frozen=True)
class ReleaseParkResult:
    """Result of :meth:`ReleaseOrchestrator.start_release`."""

    signal_id: str
    wait: PendingWait
    trigger: ReleaseTriggerResult


class HumanGatedReleaseExecutor:
    """Default executor placeholder — production triggering is NOT wired.

    BDP-2258's production-deploy boundary stays TeamCity-only with no
    break-glass; the real trigger is a founder-gated fill-in that must be
    validated against a live pipeline. Until then this refuses to trigger, so an
    accidental wiring can never fire a release.
    """

    def trigger_release(
        self, *, version: str, session_id: str
    ) -> ReleaseTriggerResult:
        raise NotImplementedError(
            "Production release triggering is founder-gated (TeamCity-only, no "
            "break-glass). Wire a validated ReleaseExecutor before enabling."
        )


class ReleaseOrchestrator:
    """Coordinate park → bind → trigger for an omnigent-native release run."""

    def __init__(
        self,
        *,
        bus,
        binding_store,
        executor: ReleaseExecutor | None = None,
    ) -> None:
        self._bus = bus
        self._binding_store = binding_store
        self._executor: ReleaseExecutor = executor or HumanGatedReleaseExecutor()

    def start_release(
        self,
        *,
        version: str,
        session_id: str,
        expires_at: int | None = None,
        now: int | None = None,
    ) -> ReleaseParkResult:
        """Park the durable wait + register the TeamCity binding, THEN trigger.

        :param version: the release version (e.g. ``"1.2.3"``).
        :param session_id: the release run's session to wake on ``build.finished``.
        :param expires_at: optional epoch deadline for the wait (None = no expiry).
        :returns: the parked wait + the executor's trigger result.
        """
        signal_id = release_signal_id(version)
        wait = self._bus.register_wait(
            signal_id=signal_id,
            session_id=session_id,
            key=signal_id,
            kind="release",
            target=version,
            expires_at=expires_at,
            now=now,
        )
        self._binding_store.register_binding(
            source=TEAMCITY_SOURCE,
            match_key=signal_id,
            signal_id=signal_id,
            now=now,
        )
        trigger = self._executor.trigger_release(
            version=version, session_id=session_id
        )
        return ReleaseParkResult(signal_id=signal_id, wait=wait, trigger=trigger)
