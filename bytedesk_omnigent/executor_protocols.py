"""Runtime-checkable Protocols for the optional executor callbacks (BDP-2339).

The harness scaffold (``omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter``)
installs three *optional* per-turn callbacks onto whatever inner
:class:`omnigent.inner.executor.Executor` it wraps, via best-effort
``getattr``/``setattr`` on private attributes:

- ``_tool_executor`` — round-trips a spec-declared tool call back through
  ``TurnContext.dispatch_tool``.
- ``_elicitation_handler`` — answers the inner SDK's permission prompt
  (``can_use_tool``) for a tool call.
- ``_policy_evaluator`` — evaluates an ``LLM_REQUEST`` / ``LLM_RESPONSE`` /
  ``TOOL_CALL`` policy phase before/after an LLM or tool call.

Because the attributes are not declared on the :class:`Executor` ABC, the
contract for each callback today lives only in scattered file-local
``TypeAlias = Callable[...]`` declarations (``claude_sdk_executor.ToolExecutor``,
``claude_sdk_executor.ElicitationHandler``, ``cursor_executor``'s inline
``Callable[[str, dict[str, Any]], Awaitable[Any]]``) plus the docstrings of the
``ExecutorAdapter._stable_*`` bridges. This module makes those three implicit
call signatures **explicit and runtime-checkable** so a wiring site can assert
``isinstance(handler, ToolExecutorProtocol)`` instead of trusting a duck-typed
``getattr``.

This is a **pure additive typing module**: it does not change how the callbacks
are installed or invoked — it only names the contracts the adapter already
relies on. Each Protocol's ``__call__`` mirrors the verified signature of the
matching ``ExecutorAdapter._stable_*`` bridge:

- :meth:`ExecutorAdapter._stable_tool_executor` ``(tool_name, args) -> dict``
- :meth:`ExecutorAdapter._stable_elicitation_handler` ``(tool_name, tool_input) -> bool``
- :meth:`ExecutorAdapter._stable_policy_evaluator` ``(phase, data) -> verdict``
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, TypeAlias, runtime_checkable

# JSON-shaped boundary aliases, mirroring the inner-executor convention
# (``omnigent.inner.executor`` / ``omnigent.inner.claude_sdk_executor``):
# heterogeneous dicts keyed by string, opaque at this layer. Isolating the
# justified ``explicit-any`` to a single place keeps callers ``object``-free.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Policy-phase event payload handed to the evaluator and the verdict it
# returns. The verdict is provider-opaque here (the adapter narrows it to
# ``PolicyVerdictPayload`` at its own boundary) — kept ``Any`` so this module
# does not import the scaffold and tighten the dependency graph.
PolicyEventData: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
PolicyVerdict: TypeAlias = Any  # type: ignore[explicit-any]


@runtime_checkable
class ToolExecutorProtocol(Protocol):
    """Contract for the executor's ``_tool_executor`` callback.

    Mirrors :meth:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter._stable_tool_executor`
    and the file-local ``ToolExecutor`` aliases in the inner executors
    (e.g. ``claude_sdk_executor.ToolExecutor``,
    ``pi_executor.ToolExecutor``). The inner SDK invokes this with the
    tool name from the LLM's call and the decoded argument dict; the
    awaited result is a dict suitable as the tool's result payload (a
    parsed JSON object, or ``{"error": ...}`` / ``{"result": ...}`` on the
    failure / non-JSON paths).
    """

    async def __call__(self, tool_name: str, args: ToolArgs) -> ToolResult: ...


@runtime_checkable
class ElicitationHandlerProtocol(Protocol):
    """Contract for the executor's ``_elicitation_handler`` callback.

    Mirrors :meth:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter._stable_elicitation_handler`
    and ``claude_sdk_executor.ElicitationHandler``. The Claude SDK invokes
    this from ``options.can_use_tool`` when it requests permission before
    executing a tool (and ``permission_mode`` is not ``"bypassPermissions"``);
    ``True`` approves the call, ``False`` denies it.
    """

    async def __call__(self, tool_name: str, tool_input: ToolArgs) -> bool: ...


@runtime_checkable
class PolicyEvaluatorProtocol(Protocol):
    """Contract for the executor's ``_policy_evaluator`` callback.

    Mirrors :meth:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter._stable_policy_evaluator`
    and the ``Callable[[str, dict[str, Any]], Awaitable[Any]]`` annotation in
    ``cursor_executor``. The inner executor invokes this before
    (``PHASE_LLM_REQUEST``) and after (``PHASE_LLM_RESPONSE``) each LLM call
    (and, in the native harnesses, around ``TOOL_CALL``) with a proto-style
    phase string and the event data dict; the awaited value is the policy
    verdict (``PolicyVerdictPayload`` at the adapter boundary).
    """

    async def __call__(self, phase: str, data: PolicyEventData) -> PolicyVerdict: ...
