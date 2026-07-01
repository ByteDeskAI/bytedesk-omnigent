"""Class-driven child-session delegation services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from omnigent.communications.commands import ChatActor, DelegateToAgentCommand

ChildDelegationStatus = Literal["completed", "waiting", "failed"]
CreateChildSession = Callable[[DelegateToAgentCommand], Awaitable[str]]
PostChildPrompt = Callable[[str, str, ChatActor], Awaitable[None]]
ReadChildOutput = Callable[[str], Awaitable[str]]
RecordChildReturn = Callable[[DelegateToAgentCommand, str, str], Awaitable[None]]
ChildOutputParser = Callable[
    [DelegateToAgentCommand, str],
    tuple[Literal["completed", "failed"], Any, str | None],
]
RunnerUnavailablePredicate = Callable[[BaseException], bool]


@dataclass(frozen=True, slots=True)
class ChildDelegationOutcome:
    """Result of creating and optionally driving a delegated child session."""

    status: ChildDelegationStatus
    child_session_id: str
    output: Any = None
    error: str | None = None
    raw_output: str = ""


class ChildSessionDelegationService:
    """Coordinate child-session create, first-message send, output read, and return record."""

    def __init__(
        self,
        *,
        create_child_session: CreateChildSession,
        post_child_prompt: PostChildPrompt,
        read_child_output: ReadChildOutput,
        record_child_return: RecordChildReturn | None = None,
        parse_child_output: ChildOutputParser | None = None,
        is_runner_unavailable: RunnerUnavailablePredicate | None = None,
    ) -> None:
        self._create_child_session = create_child_session
        self._post_child_prompt = post_child_prompt
        self._read_child_output = read_child_output
        self._record_child_return = record_child_return
        self._parse_child_output = parse_child_output or _text_child_output
        self._is_runner_unavailable = is_runner_unavailable or _never_runner_unavailable

    async def delegate(self, command: DelegateToAgentCommand) -> ChildDelegationOutcome:
        """Create/reuse a child session, send its prompt, and read its output."""
        child_session_id = await self._create_child_session(command)
        try:
            await self._post_child_prompt(child_session_id, command.prompt, command.actor)
        except Exception as exc:
            if self._is_runner_unavailable(exc):
                return ChildDelegationOutcome(
                    status="waiting",
                    child_session_id=child_session_id,
                    output={"prompt": command.prompt},
                )
            raise

        raw_output = await self._read_child_output(child_session_id)
        if self._record_child_return is not None:
            await self._record_child_return(command, child_session_id, raw_output)
        status, output, error = self._parse_child_output(command, raw_output)
        return ChildDelegationOutcome(
            status=status,
            child_session_id=child_session_id,
            output=output,
            error=error,
            raw_output=raw_output,
        )


def _text_child_output(
    command: DelegateToAgentCommand,
    raw_output: str,
) -> tuple[Literal["completed", "failed"], str, None]:
    del command
    return "completed", raw_output, None


def _never_runner_unavailable(exc: BaseException) -> bool:
    del exc
    return False


__all__ = [
    "ChildDelegationOutcome",
    "ChildDelegationStatus",
    "ChildSessionDelegationService",
]
