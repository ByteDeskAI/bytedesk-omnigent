"""Pydantic models for the API layer — request/response shapes AND
SSE stream events.

This module is split into two sections, separated by a clearly marked
delineator further down:

1. Request and response body schemas for the JSON endpoints.
2. SSE event payload models — the discriminated union that every
   event the server emits over its SSE endpoints validates against.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import ConversationItem

# ── Shared ──────────────────────────────────────────────────────


class PaginatedList(BaseModel):
    """
    A paginated list response following cursor-based pagination.

    :param object: Fixed resource type, always ``"list"``.
    :param data: Page of results. Items are heterogeneous
        (``ResponseObject``, ``ConversationObject``, ``FileObject``,
        or dicts) and list is invariant, so no single concrete type
        satisfies all callers.
    :param first_id: ID of the first item in the page, or ``None``
        if the page is empty, e.g. ``"resp_abc123"``.
    :param last_id: ID of the last item in the page, or ``None``
        if the page is empty, e.g. ``"resp_xyz789"``.
    :param has_more: Whether more items exist beyond this page.
    """

    object: str = "list"
    # Any: items are heterogeneous (ResponseObject, ConversationObject,
    # FileObject, or dicts) and list is invariant, so no single concrete
    # type satisfies all callers.
    data: list[Any] = Field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


# ── Agents ──────────────────────────────────────────────────────


class MCPServerSummary(BaseModel):
    """
    Safe subset of an MCP server's configuration for API exposure.

    Secret-bearing fields (``headers``, ``env``) are intentionally
    excluded. This model is the wire shape returned inside
    :class:`AgentObject` so clients can display which MCP servers
    an agent is connected to without leaking credentials.

    :param name: Server name as declared in the agent spec,
        e.g. ``"github"``.
    :param transport: Transport type — ``"stdio"`` or ``"http"``.
    :param description: Optional free-text description from the
        spec, e.g. ``"GitHub MCP server"``. ``None`` when unset.
    :param url: HTTP(S) endpoint URL for ``transport="http"``
        servers, e.g. ``"https://mcp.example.com/sse"``. ``None``
        for stdio servers.
    :param command: Executable path for ``transport="stdio"``
        servers, e.g. ``"uvx"``. ``None`` for http servers.
    :param args: Command-line arguments for ``transport="stdio"``
        servers, e.g. ``["mcp-server-github"]``. Empty list
        when unset.
    """

    name: str
    transport: str
    description: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)


class SkillSummary(BaseModel):
    """
    Safe subset of a discovered skill for API exposure.

    Surfaces the skill name and one-line description so clients
    (e.g. the web composer's slash-command menu) can list which
    skills the session has access to. The full skill ``content``
    is intentionally omitted — it's only loaded server-side when
    the harness invokes the skill, and it can be large.

    :param name: Skill identifier as parsed from the SKILL.md
        frontmatter, e.g. ``"triage-issues"``. Lowercase
        kebab-case.
    :param description: One-line summary from the SKILL.md
        frontmatter, e.g. ``"Triage open GitHub issues in the
        repo."``.
    """

    name: str
    description: str


class PolicySummary(BaseModel):
    """
    Safe subset of a policy's spec for API exposure.

    Exposes the policy name, type, and phases so the UI can
    display which guardrails are active on an agent. The full
    policy body (prompt text, callable path, label conditions)
    is intentionally excluded — this is a summary for display,
    not a full spec.

    :param name: Policy name as declared in the agent spec,
        e.g. ``"block_long_sleep"``.
    :param type: Policy type discriminator — ``"function"``
        or ``"prompt"``.
    :param on: List of phase selectors the policy fires on,
        e.g. ``["tool_call"]`` or ``["request", "response"]``.
    :param description: Short detail string about the policy
        implementation. For function policies: the callable
        dotted path. For prompt policies: the first line of
        the prompt. ``None`` when not available.
    """

    name: str
    type: str
    on: list[str]
    description: str | None = None


class AgentObject(BaseModel):
    """
    API representation of a registered agent.

    :param id: Unique agent identifier, e.g. ``"ag_abc123"``.
    :param object: Fixed resource type, always ``"agent"``.
    :param name: Human-readable agent name,
        e.g. ``"research-agent"``.
    :param display_name: Optional human display name sourced from the
        bundle's ``params.displayName`` (e.g. ``"Maya Chen"``). The Web
        UI new-session picker prefers it over the slug ``name``. ``None``
        when the bundle sets no ``params.displayName`` or can't be loaded.
    :param version: Monotonic version counter. Starts at 1,
        incremented on each update.
    :param description: Optional free-text description of the
        agent's purpose.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last update,
        or ``None`` if never updated.
    :param harness: The agent's harness/kind, e.g. ``"codex"``,
        ``"codex-native"``, or ``"claude-native"`` for
        ``executor.type: omnigent`` agents, otherwise the executor
        type (``"claude_sdk"``, ``"agents_sdk"``). ``None`` when the
        bundle cannot be loaded. Lets the Web UI Add Agent picker
        recognise an agent's kind (Codex vs Claude) without
        hardcoding by name slug.
    :param mcp_servers: MCP servers the agent is connected to
        (secret fields omitted). Empty list when the spec
        declares no MCP servers or when the bundle cannot be
        loaded.
    :param policies: Guardrails policies declared on the agent.
        Each entry summarises the policy name, type, and
        phases. Empty list when the spec declares no policies
        or when the bundle cannot be loaded.
    :param skills: Skills bundled in the agent spec
        (``skills/<name>/SKILL.md``). Lets the Web UI's
        new-session composer offer a slash-command menu before a
        session (and its runner) exists. Host-discovered skills
        are runner-owned, so they are NOT listed here — the
        session snapshot's ``skills`` field carries the merged
        set once a runner is bound. Empty list when the spec
        bundles no skills or when the bundle cannot be loaded.
    :param terminals: Terminal names declared in the spec's
        ``terminals:`` block, in declaration order, e.g.
        ``["shell"]``. The Web UI gates its "new terminal"
        affordance on this list (creation is only offered for
        agents with terminal access) and offers these names as
        the launchable choices. Empty list when the spec
        declares no terminals or when the bundle cannot be
        loaded.
    """

    id: str
    object: str = "agent"
    name: str
    display_name: str | None = None
    version: int = 1
    description: str | None = None
    created_at: int
    updated_at: int | None = None
    harness: str | None = None
    mcp_servers: list[MCPServerSummary] = Field(default_factory=list)
    policies: list[PolicySummary] = Field(default_factory=list)
    skills: list[SkillSummary] = Field(default_factory=list)
    terminals: list[str] = Field(default_factory=list)
    # Org metadata derived from the bundle's ``params`` (FU3, ADR-0134) for the
    # params-derived org chart. ``managers`` are kept as raw
    # ``{id, displayName, title}`` objects (the platform side flattens to
    # manager-id slug strings); ``department`` / ``title`` are plain strings.
    managers: list[dict[str, Any]] = Field(default_factory=list)
    department: str | None = None
    title: str | None = None
    # True when the bundle's ``params.workflow`` is true — i.e. this is a
    # workflow/orchestrator agent (BDP-2180/2181), not a person. The platform
    # uses it to keep workflows off the org chart / roster while still listing
    # them in omnigent's own picker (BDP-2187). Defaults False (employees).
    # Kept for back-compat; now derived from ``category`` (== "workflow").
    workflow: bool = False
    # Agent tier (agent-tiering step 1): "system" | "harness" | "employee" |
    # "workflow". First-class, queryable classification from the persisted
    # entity. ``system`` = administrative system operators; ``harness`` =
    # platform launcher templates such as Claude/Codex/Pi/Grok. Defaults
    # "employee".
    category: str = "employee"


class BlueprintGraphEdge(BaseModel):
    """Static dependency edge in a blueprint graph."""

    id: str
    source: str
    target: str


class BlueprintGraphLoop(BaseModel):
    """Static loop metadata nested under a blueprint loop node."""

    max_iterations: int
    until: Any | None = None
    on_exhausted: str
    reuse_session: bool = False
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[BlueprintGraphEdge] = Field(default_factory=list)


class BlueprintGraphNode(BaseModel):
    """Static node projection for a blueprint graph."""

    id: str
    kind: str
    depends_on: list[str] = Field(default_factory=list)
    target: str | None = None
    when: Any | None = None
    input: Any | None = None
    return_: Any | None = Field(default=None, alias="return")
    output: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    loop: BlueprintGraphLoop | None = None

    model_config = ConfigDict(populate_by_name=True)


class BlueprintGraphResponse(BaseModel):
    """Static normalized blueprint graph response."""

    object: Literal["blueprint"] = "blueprint"
    agent_id: str | None = None
    agent_name: str | None = None
    name: str | None = None
    description: str | None = None
    version: int = 1
    nodes: list[BlueprintGraphNode] = Field(default_factory=list)
    edges: list[BlueprintGraphEdge] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)


class BlueprintRunNode(BaseModel):
    """Live node-state projection for a blueprint run."""

    id: str
    kind: str | None = None
    status: str | None = None
    loop_iteration: int | None = None
    child_session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: int | None = None


class BlueprintRunResponse(BaseModel):
    """Live blueprint run snapshot reconstructed from persisted events."""

    object: Literal["blueprint_run"] = "blueprint_run"
    blueprint_run_id: str | None = None
    status: str = "pending"
    nodes: list[BlueprintRunNode] = Field(default_factory=list)
    loop_iterations: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)


# ── Session Policies ───────────────────────────────────────────


class SessionPolicyObject(BaseModel):
    """
    API representation of a session-scoped policy.

    Returned by all CRUD endpoints under
    ``/v1/sessions/{session_id}/policies``.

    :param id: Opaque policy identifier, e.g. ``"spol_abc123"``.
        ``None`` for spec-declared policies that are not
        store-persisted.
    :param object: Fixed resource type, always
        ``"session.policy"``.
    :param name: Human-readable policy name,
        e.g. ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (python) or HTTP URL
        (url), e.g. ``"github_mcp_policy.block_push"`` or
        ``"https://example.com/policies/eval"``.
    :param factory_params: Dict of kwargs passed to the handler
        when it is a factory function. ``None`` for direct
        callables and ``type="url"`` handlers.
    :param enabled: Whether the engine consults this policy.
    :param source: Origin of the policy: ``"session"`` for
        CRUD-created policies, ``"spec"`` for policies
        declared in the agent YAML. Spec policies cannot be
        patched or deleted.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update, or ``None`` if never updated.
    """

    id: str | None
    object: str = "session.policy"
    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None
    enabled: bool = True
    source: str = "session"
    created_at: int
    updated_at: int | None = None


_DOTTED_PATH_RE = r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)+$"


class CreateSessionPolicyRequest(BaseModel):
    """
    Request body for ``POST /v1/sessions/{session_id}/policies``.

    :param name: Human-readable policy name. Must be unique
        within the session, e.g.
        ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (python) or HTTPS URL
        (url), e.g.
        ``"github_mcp_policy.block_non_misc_push"``
        or ``"https://example.com/policies/eval"``.
    :param factory_params: Optional dict of kwargs passed to the
        handler when it is a factory function. Only valid for
        ``type="python"``, e.g. ``{"limit": 10}``.
    """

    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_type_and_handler(self) -> CreateSessionPolicyRequest:
        """Reject unknown policy types and validate handler format.

        For ``type="url"``, requires an ``https://`` URL.
        For ``type="python"``, requires a valid dotted import path
        (at least two segments, e.g. ``"pkg.module"``).

        :returns: The validated request unchanged.
        :raises ValueError: If ``type`` is invalid, or ``handler``
            does not match the expected format for the type.
        """
        if self.type not in ("python", "url"):
            raise ValueError(f"type must be 'python' or 'url', got '{self.type}'")
        if self.type == "url":
            if not self.handler.startswith("https://"):
                raise ValueError("handler must be an https:// URL for type 'url'")
        elif self.type == "python":
            if not re.match(_DOTTED_PATH_RE, self.handler):
                raise ValueError(
                    "handler must be a valid dotted import path "
                    "(e.g. 'pkg.module.func') for type 'python'"
                )
        return self


class UpdateSessionPolicyRequest(BaseModel):
    """
    Request body for ``PATCH /v1/sessions/{session_id}/policies/{policy_id}``.

    All fields are optional; ``None`` fields are left unchanged.
    Unknown fields (including ``type``, which is immutable) are
    rejected with ``422``.

    :param name: New policy name. ``None`` leaves it unchanged.
    :param handler: New handler path or URL. ``None`` leaves it
        unchanged.
    :param enabled: New enabled flag. ``None`` leaves it
        unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    handler: str | None = None
    enabled: bool | None = None


# ── Default Policies ──────────────────────────────────────────────


class DefaultPolicyObject(BaseModel):
    """
    API representation of a server-wide default policy.

    Returned by all CRUD endpoints under ``/v1/policies``.

    :param id: Opaque policy identifier, e.g. ``"dpol_abc123"``.
    :param object: Fixed resource type, always
        ``"default_policy"``.
    :param name: Human-readable policy name,
        e.g. ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (python) or HTTP URL
        (url), e.g. ``"github_mcp_policy.block_push"`` or
        ``"https://example.com/policies/eval"``.
    :param factory_params: Dict of kwargs passed to the handler
        when it is a factory function. ``None`` for direct
        callables and ``type="url"`` handlers.
    :param enabled: Whether the engine consults this policy.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update, or ``None`` if never updated.
    :param created_by: User ID of the admin who created this
        policy, or ``None`` in single-user mode.
    """

    id: str
    object: str = "default_policy"
    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None
    enabled: bool = True
    created_at: int
    updated_at: int | None = None
    created_by: str | None = None


class CreateDefaultPolicyRequest(BaseModel):
    """
    Request body for ``POST /v1/policies``.

    :param name: Human-readable policy name. Must be globally
        unique, e.g. ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"``, ``"url"``,
    :param handler: Dotted import path (python) or HTTPS URL
        (url), e.g.
        ``"github_mcp_policy.block_non_misc_push"``
        or ``"https://example.com/policies/eval"``.
    :param factory_params: Optional dict of kwargs passed to the
        handler when it is a factory function. Only valid for
        ``type="python"``, e.g. ``{"limit": 10}``.
    """

    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_type_and_handler(self) -> CreateDefaultPolicyRequest:
        """Reject unknown policy types and validate handler format.

        Same validation rules as :class:`CreateSessionPolicyRequest`.

        :returns: The validated request unchanged.
        :raises ValueError: If ``type`` is invalid, or ``handler``
            does not match the expected format for the type.
        """
        if self.type not in ("python", "url"):
            raise ValueError(f"type must be 'python' or 'url', got '{self.type}'")
        if self.type == "url":
            if not self.handler.startswith("https://"):
                raise ValueError("handler must be an https:// URL for type 'url'")
        elif self.type == "python":
            if not re.match(_DOTTED_PATH_RE, self.handler):
                raise ValueError(
                    "handler must be a valid dotted import path "
                    "(e.g. 'pkg.module.func') for type 'python'"
                )
        return self


class UpdateDefaultPolicyRequest(BaseModel):
    """
    Request body for ``PATCH /v1/policies/{policy_id}``.

    All fields are optional; ``None`` fields are left unchanged.
    Unknown fields (including ``type``, which is immutable) are
    rejected with ``422``.

    :param name: New policy name. ``None`` leaves it unchanged.
    :param handler: New handler path or URL. ``None`` leaves it
        unchanged.
    :param enabled: New enabled flag. ``None`` leaves it
        unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    handler: str | None = None
    enabled: bool | None = None


# ── Files ───────────────────────────────────────────────────────


class FileObject(BaseModel):
    """
    API representation of an uploaded file.

    :param id: Unique file identifier, e.g. ``"file_abc123"``.
    :param object: Fixed resource type, always ``"file"``.
    :param filename: Original filename, e.g. ``"report.pdf"``.
    :param bytes: File size in bytes.
    :param created_at: Unix epoch timestamp of upload.
    """

    id: str
    object: str = "file"
    filename: str
    bytes: int
    created_at: int


# ── Session Resources ───────────────────────────────────────────


class SessionResourceObject(BaseModel):
    """
    API representation of a session-scoped resource handle.

    :param id: Opaque resource identifier, e.g. ``"default"`` or
        ``"terminal_bash_s1"``.
    :param object: Fixed resource type, always ``"session.resource"``.
    :param type: Resource kind, initially ``"environment"``,
        ``"terminal"``, or ``"file"``.
    :param session_id: Owning session/conversation id.
    :param name: Human-readable display name. Not required to be
        globally unique.
    :param metadata: Resource-type-specific metadata.
    :param environment: For terminal resources, the environment id the
        terminal actually runs in. Omitted for non-terminal resources.
    """

    id: str
    object: Literal["session.resource"]
    type: Literal["environment", "terminal", "file"]
    session_id: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment: str | None = None

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionResourceListPage(BaseModel):
    """Strict runner resource-list wire contract."""

    object: Literal["list"]
    data: list[SessionResourceObject]
    first_id: str | None
    last_id: str | None
    has_more: bool

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionResourcePaginatedList(BaseModel):
    """Public paginated list of session resources."""

    object: Literal["list"] = "list"
    data: list[SessionResourceObject] = Field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


# ── Conversations ───────────────────────────────────────────────


class ConversationObject(BaseModel):
    """
    API representation of a conversation.

    :param id: Unique conversation identifier,
        e.g. ``"conv_abc123"``.
    :param object: Fixed resource type, always
        ``"conversation"``.
    :param title: Optional user-assigned conversation title.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update, e.g. ``1774118400``.
    :param labels: Session-scoped guardrails labels, mirroring
        the runtime ``Conversation.labels`` dict. Empty dict when
        the PolicyEngine hasn't written any labels yet. Exposed so
        the REPL's Ctrl+O debug overlay can render them at parity
        with the legacy ``omnigent run`` Ctrl+G overview.
    """

    id: str
    object: str = "conversation"
    title: str | None = None
    created_at: int
    updated_at: int
    labels: dict[str, str] = Field(default_factory=dict)


class ConversationDeleted(BaseModel):
    """
    Confirmation payload returned after deleting a conversation.

    :param id: ID of the deleted conversation,
        e.g. ``"conv_abc123"``.
    :param object: Fixed resource type, always
        ``"conversation.deleted"``.
    :param deleted: Always ``True``.
    """

    id: str
    object: str = "conversation.deleted"
    deleted: bool = True


class ConversationRef(BaseModel):
    """
    Lightweight reference to a conversation, used in request and
    response bodies where only the conversation ID is needed.

    :param id: Conversation identifier, e.g. ``"conv_abc123"``.
    """

    id: str


class ChildSessionSummary(BaseModel):
    """
    Summary of a sub-agent (child) session under a parent session.

    Powers ``GET /v1/sessions/{id}/child_sessions``. Lets the web /
    REPL debug surface enumerate sub-agent calls spawned from a
    parent session without parsing parent ``function_call_output``
    JSON handles (the legacy TUI Ctrl+O path). The endpoint is the
    canonical "historical truth" source; the existing transient
    ``session.created`` SSE event handles live incremental updates.

    Fields are derived from the child :class:`Conversation` plus its
    latest :class:`Task` (newest by ``created_at``).

    :param id: Child conversation/session identifier,
        e.g. ``"conv_child123"``.
    :param object: Fixed resource type, always
        ``"child_session"``.
    :param parent_session_id: Parent conversation id (echo of the
        route's ``session_id`` path parameter), e.g.
        ``"conv_parent987"``. Stable join key for clients that
        cache child rows across multiple parents.
    :param title: Sub-agent title, ``"{agent_type}:{session_name}"``
        as written by :func:`omnigent.tools.builtins.spawn._spawn_one`,
        e.g. ``"researcher:auth"``. ``None`` only for legacy /
        malformed rows; the spawn path always sets it.
    :param tool: UI-facing sub-agent label. For Omnigent-spawned
        children this is derived from the prefix of ``title`` before
        the first ``":"``, e.g. ``"researcher"``. For Codex-native
        children this is the Codex-assigned ``agent_nickname`` when
        available, then ``agent_role``, then ``"Codex"``. Falls back
        to the raw title for legacy / malformed rows; ``None`` only
        when ``title`` itself is ``None`` or empty.
    :param session_name: Sub-agent instance name, the suffix of
        ``title`` after the first ``":"``, e.g. ``"auth"``. ``None``
        if ``title`` is ``None`` or missing a colon.
    :param kind: Conversation kind discriminator, always
        ``"sub_agent"`` for rows surfaced by this endpoint.
    :param created_at: Unix epoch timestamp of child creation.
    :param updated_at: Unix epoch timestamp of the child's most
        recent update.
    :param agent_id: Agent id recorded on the latest task,
        e.g. ``"ag_abc123"``. ``None`` if the child has no tasks
        yet (rare — ``_spawn_one`` creates a task atomically with
        the conversation).
    :param agent_name: Agent type recorded on the latest task,
        e.g. ``"researcher"``. Mirrors the ``tool`` prefix in
        ``title`` and is provided alongside it because the title
        is a denormalized string while ``agent_name`` is the
        durable per-task value.
    :param current_task_id: Latest task id for the child
        (newest by ``created_at``), e.g. ``"task_abc123"``.
        ``None`` if no tasks exist.
    :param current_task_status: Status of the latest task,
        e.g. ``"completed"``, ``"in_progress"``, ``"failed"``.
        ``None`` if no tasks exist.
    :param busy: ``True`` when the child's session loop is live.
        Mirrors the algorithm used by ``GET /v1/sessions/{id}`` to
        compute ``status``: read the live in-memory cache first
        (``"running"``/``"waiting"`` → busy), and fall back to the
        latest task's status on cache miss (``"queued"`` /
        ``"in_progress"`` → busy). For NO_DBOS sessions the tasks
        table is not populated during active runs, so the cache
        consult is what keeps the rail's "Working" badge correct.
    :param labels: Session-scoped guardrails labels on the child
        conversation (mirrors :class:`ConversationObject.labels`).
    :param last_task_error: Error details from the child's most recent
        failed run, e.g.
        ``{"code": "required_terminal_exited", "message": "..."}``.
        ``None`` when the child has no durable failure detail. This is
        the typed projection of runner-owned failure labels; clients
        should not parse those labels directly.
    :param last_message_preview: Single-line preview of the most
        recent message item in the child's conversation, truncated
        to ~150 chars with a trailing ellipsis when longer. ``None``
        when the child has no message items yet (rare — the spawn
        tool immediately commits a user message). Lets the UI
        render a real-time "what's the sub-agent saying right now"
        line without fetching the child's full item history.
    :param pending_elicitations_count: Number of approval / input
        prompts the child is currently blocked on, read from the
        server's :mod:`omnigent.runtime.pending_elicitations`
        index. ``> 0`` means the sub-agent is parked awaiting user
        input — the Agents rail renders an "awaiting input" badge so
        a fanned-out sub-agent that needs attention is visible
        without opening its chat. Mirrors
        :attr:`SessionListItem.pending_elicitations_count`.
    """

    id: str
    object: str = "child_session"
    parent_session_id: str
    title: str | None = None
    tool: str | None = None
    session_name: str | None = None
    kind: str = "sub_agent"
    created_at: int
    updated_at: int
    agent_id: str | None = None
    agent_name: str | None = None
    current_task_id: str | None = None
    current_task_status: str | None = None
    busy: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    last_task_error: dict[str, str] | None = None
    last_message_preview: str | None = None
    pending_elicitations_count: int = 0


# ── Responses ───────────────────────────────────────────────────


class UsageDetails(BaseModel):
    """
    Breakdown of output token usage.

    :param reasoning_tokens: Number of tokens consumed by
        chain-of-thought reasoning.
    """

    reasoning_tokens: int = 0


class Usage(BaseModel):
    """
    Token usage statistics for a response.

    :param input_tokens: Number of input (prompt) tokens consumed.
    :param output_tokens: Number of output (completion) tokens
        generated.
    :param output_tokens_details: Breakdown of output token usage
        (e.g. reasoning tokens).
    :param total_tokens: Sum of input and output tokens across all
        LLM sub-calls for this turn (billing total).
    :param context_tokens: Context-fill estimate for the next turn —
        set only by executors that make multiple LLM sub-calls per
        turn (e.g. ``openai-agents``).  For single-call executors
        this is absent and ``total_tokens`` serves the same purpose.
        The toolbar context ring and ``/context`` command use this
        field when present, falling back to ``total_tokens``.
    :param cache_read_input_tokens: Prompt tokens served from a
        provider prompt cache (cache hit), billed at a reduced rate.
        Reported by Anthropic-style providers as a count *separate*
        from ``input_tokens`` (which carries only the non-cached
        portion); ``0`` when the provider does not break out cache
        usage. Consumed by the cache-aware server-side cost path.
    :param cache_creation_input_tokens: Prompt tokens written to the
        provider prompt cache (cache creation), billed at a premium
        rate. Like ``cache_read_input_tokens``, this is separate from
        ``input_tokens``; ``0`` when not reported.
    :param model: The LLM model the harness actually used for this
        turn, e.g. ``"claude-opus-4-8"`` or ``"databricks-gpt-5-5"``.
        Reported by relay executors so the server-side cost path can
        price the turn even when the agent spec pins no ``llm.model``
        (e.g. supervisors that delegate / use the harness default).
        ``None`` when the executor doesn't report it; the cost path
        then falls back to the session override / spec model.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    output_tokens_details: UsageDetails = Field(default_factory=UsageDetails)
    total_tokens: int = 0
    context_tokens: int | None = None
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model: str | None = None


class ErrorDetail(BaseModel):
    """
    Machine-readable error information attached to a failed response.

    :param code: Error code string, e.g. ``"server_error"``,
        ``"invalid_input"``.
    :param message: Human-readable error description.
    """

    code: str
    message: str


class IncompleteDetails(BaseModel):
    """
    Details explaining why a response is incomplete.

    :param reason: Reason the response stopped early, e.g.
        ``"max_output_tokens"``, ``"max_tool_calls"``.
    """

    reason: str


class CreateResponseRequest(BaseModel):
    """
    Internal request body the harness scaffold builds for each turn.

    Originally the ``POST /v1/responses`` request schema; that route
    was removed but the harness scaffold still synthesizes this shape
    internally to drive an executor turn.

    :param model: Agent name to invoke, e.g.
        ``"research-agent"``. Must match a registered agent.
    :param input: User input — either a plain string (converted
        to a single ``input_text`` block) or a list of content
        blocks, e.g.
        ``[{"type": "input_text", "text": "Hello"}]``.
    :param stream: If ``True``, return an SSE stream instead of
        blocking until completion.
    :param background: If ``True``, the task runs in the
        background and the caller may poll for results.
    :param store: Must be ``True`` (persisted responses). The
        server rejects ``False``.
    :param instructions: Per-request system instructions that
        override the agent's default instructions.
    :param previous_response_id: ID of the prior response in the
        conversation thread, e.g. ``"resp_abc123"``. Enables
        multi-turn continuation and steering.
    :param conversation: Explicit conversation reference for
        fork validation. Must match the conversation that owns
        ``previous_response_id``.
    :param reasoning: Reasoning configuration,
        e.g. ``{"effort": "medium"}``.
    :param model_override: Optional per-request LLM model override,
        e.g. ``"openai/gpt-5.4-mini"``. Distinct from ``model``
        (agent name). Substitutes for the spec's ``llm.model`` for
        this single request. Drives the REPL's ``/model`` command.
    :param context_management: Compaction strategy objects,
        e.g. ``[{"type": "compaction", ...}]``.
    :param temperature: Ignored — agent controls this. Silently
        dropped.
    :param top_p: Ignored — agent controls this. Silently
        dropped.
    :param tools: Optional list of client-specified tools in standard
        OpenAI function format. When the LLM invokes one, the
        ``function_call`` output items are returned to the caller (the
        response completes) rather than being executed server-side. The
        caller handles execution and continues via
        ``previous_response_id``. Returns 400 if any entry is malformed
        or missing ``function.name``, e.g.
        ``[{"type": "function", "function": {"name": "get_weather",
        "description": "...", "parameters": {...}}}]``.
    :param tool_choice: Ignored — agent controls this. Silently
        dropped.
    :param max_output_tokens: Ignored — agent controls this.
        Silently dropped.
    :param frequency_penalty: Ignored — agent controls this.
        Silently dropped.
    :param presence_penalty: Ignored — agent controls this.
        Silently dropped.
    :param parallel_tool_calls: Ignored — agent controls this.
        Silently dropped.
    :param max_tool_calls: Ignored — agent controls this.
        Silently dropped.
    :param top_logprobs: Ignored — agent controls this. Silently
        dropped.
    """

    # Optional when previous_response_id is set; server resolves the agent
    # from the prior task. Required for fresh conversations (no prior task).
    model: str | None = None
    # Heterogeneous content blocks (input_text, input_image, input_file)
    # or a plain string shorthand; shape varies by block type.
    input: str | list[dict[str, Any]]
    stream: bool = False
    background: bool = False
    store: bool = True
    instructions: str | None = None
    previous_response_id: str | None = None
    # Correlation id for a mid-turn injection (RUNNER_MESSAGE_INGEST.md
    # Part B). Stamped by the runner when it forwards a buffered message
    # as a live injection; echoed back by the executor adapter in an
    # ``injection.consumed`` marker once the executor actually consumes
    # the message, so the runner can drop the buffered copy and not
    # re-deliver it in a continuation turn. ``None`` for fresh turns.
    injection_id: str | None = None
    conversation: ConversationRef | None = None
    # Reasoning config, e.g. {"effort": "low"|"medium"|"high"}
    reasoning: dict[str, str] | None = None
    # Per-request LLM model override (distinct from ``model``, which
    # carries the agent name). See class docstring for semantics.
    model_override: str | None = None
    # Compaction strategy objects, e.g. [{"type": "compaction", ...}]
    context_management: list[dict[str, Any]] | None = None
    # Ignored fields — agent controls these; silently dropped.
    # Typed loosely because we only need to accept and discard them.
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | str | None = None
    max_output_tokens: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    parallel_tool_calls: bool | None = None
    max_tool_calls: int | None = None
    top_logprobs: int | None = None

    @model_validator(mode="after")
    def _require_model_for_new_conversations(self) -> CreateResponseRequest:
        """
        Enforce that ``model`` is provided when starting a fresh conversation.

        When ``previous_response_id`` is not set the server has no prior task
        from which to resolve the agent, so ``model`` is required. Omitting it
        produces a 422 at the API boundary rather than a cryptic runtime error
        deep in the route handler.

        :returns: ``self`` unchanged when the invariant holds.
        :raises ValueError: When ``model`` is ``None`` and
            ``previous_response_id`` is not set.
        """
        if self.model is None and not self.previous_response_id:
            raise ValueError("model is required when previous_response_id is not set")
        return self


class ResponseObject(BaseModel):
    """
    API representation of a response (task execution result).

    :param id: Unique response identifier, e.g.
        ``"resp_abc123"``.
    :param object: Fixed resource type, always ``"response"``.
    :param status: Lifecycle status, one of ``"queued"``,
        ``"in_progress"``, ``"completed"``, ``"failed"``,
        ``"incomplete"``, ``"cancelled"``.
    :param model: Agent name that produced this response,
        e.g. ``"research-agent"``.
    :param created_at: Unix epoch timestamp of creation.
    :param completed_at: Unix epoch timestamp of completion, or
        ``None`` if not yet complete.
    :param output: Heterogeneous output items (messages,
        reasoning, function_calls) serialized as dicts; shape
        varies by item type. Empty for non-completed responses.
    :param background: Whether this response was created as a
        background task.
    :param store: Whether this response is persisted. Always
        ``True``.
    :param usage: Token usage statistics, or ``None`` if not
        yet available.
    :param previous_response_id: ID of the prior response in
        the conversation thread, or ``None`` for the first turn.
    :param conversation: Reference to the owning conversation.
    :param instructions: Per-request system instructions
        override, or ``None``.
    :param reasoning: Reasoning configuration,
        e.g. ``{"effort": "medium"}``.
    :param error: Error details if the response failed.
    :param incomplete_details: Details if the response is
        incomplete (e.g. hit token limit).
    """

    id: str
    object: str = "response"
    status: str
    model: str
    created_at: int
    completed_at: int | None = None
    # Heterogeneous output items (messages, reasoning, function_calls);
    # shape varies by item type.
    output: list[dict[str, Any]] = Field(default_factory=list)
    background: bool = False
    store: bool = True
    usage: Usage | None = None
    previous_response_id: str | None = None
    conversation: ConversationRef | None = None
    instructions: str | None = None
    reasoning: dict[str, str] | None = None
    error: ErrorDetail | None = None
    incomplete_details: IncompleteDetails | None = None


class ToolResult(BaseModel):
    """
    A single tool result submitted by the client via PATCH.

    :param call_id: The tool call ID that this result
        corresponds to, e.g. ``"call_abc123"``.
    :param output: The tool's string output,
        e.g. ``'["paper1.pdf", "paper2.pdf"]'``.
    """

    call_id: str
    output: str


class ElicitationResult(BaseModel):
    """
    Consumer reply to an outstanding elicitation.

    Field names + semantics mirror MCP's ``ElicitResult`` verbatim.
    Omnigent clients deliver this shape inside the session-scoped
    ``approval`` event body, alongside the ``elicitation_id``
    correlation key.

    :param action: User action per MCP semantics. ``"accept"`` =
        approved (form submitted / confirmation given).
        ``"decline"`` = explicit refusal. ``"cancel"`` = dismissed
        without an explicit choice (also the verdict the server
        synthesizes on elicitation timeout).
    :param content: Form data when ``action == "accept"`` and the
        ``requestedSchema`` had fields. ``None`` (or omitted) for
        binary approve/reject elicitations and for ``decline`` /
        ``cancel`` actions. Values are restricted to JSON scalars
        and string lists per the MCP spec.
    """

    action: Literal["accept", "decline", "cancel"]
    # ``str | int | float | bool | list[str] | None`` mirrors MCP's
    # ElicitResult.content value type — keep them aligned so an MCP
    # client can bridge to our endpoint without translation.
    content: dict[str, str | int | float | bool | list[str] | None] | None = None


# ── Sessions (/v1/sessions) ────────────────────────────────────


class SessionEventInput(BaseModel):
    """
    A single client-submitted event/input item for a session.

    Used both as an element of ``initial_items`` on session
    creation and as the body of ``POST /v1/sessions/{id}/events``.
    Carries a discriminator (``type``) and a free-form ``data``
    payload whose shape is interpreted by the route layer based
    on ``type`` (e.g. user message, function-call output,
    approval, interrupt).

    :param model_override: Optional per-event LLM model override
        used when this event starts a fresh agent turn. Distinct
        from the session's bound agent; it substitutes for the
        agent spec's ``llm.model`` for that turn.
    :param type: Discriminator for the event/input kind, e.g.
        ``"message"``, ``"function_call_output"``, ``"interrupt"``.
    :param data: Type-specific payload. Shape varies by ``type``;
        for ``"message"`` this looks like
        ``{"role": "user", "content": [{"type": "input_text",
        "text": "Hello"}]}``. For ``"interrupt"`` this is
        typically ``{}``.
    :param tools: Optional OpenAI function-tool dicts registered
        when this event creates a new task. Mirrors
        :attr:`CreateResponseRequest.tools`, e.g. ``[{"type":
        "function", "function": {"name": "get_weather",
        "description": "...", "parameters": {...}}}]``. Ignored
        when the event steers into an active task: that task's
        tools are fixed at start time.
    """

    type: str
    # Heterogeneous payload; route layer validates the shape per ``type``.
    # Defaults to {} for payload-less control events (interrupt,
    # stop_session); item-typed events still fail loud per-type.
    data: dict[str, Any] = Field(default_factory=dict)
    model_override: str | None = None
    tools: list[dict[str, Any]] | None = None


class SessionGitOptions(BaseModel):
    """
    Git worktree options for ``POST /v1/sessions``.

    When present, the server creates a git worktree on the host for a
    new branch and starts the runner in that worktree instead of the
    picked directory. Requires ``host_id`` to be set (and therefore
    ``workspace``, which is interpreted as the source repository
    directory). See designs/SESSION_GIT_WORKTREE.md.

    :param branch_name: Name of the new branch to create and check
        out in the worktree, e.g. ``"feature/login"``. Validated
        against git ref-format rules; invalid names fail with
        ``invalid_input``.
    :param base_branch: Optional base ref to branch from, e.g.
        ``"main"`` or ``"origin/main"``. ``None`` branches from the
        source repository's current ``HEAD``.
    """

    branch_name: str
    base_branch: str | None = None


class SessionCreateRequest(BaseModel):
    """
    JSON request body for ``POST /v1/sessions``.

    Creates a new session bound to an existing agent (looked up by
    durable agent ID or template-agent name) and optionally seeds its
    input queue.

    The Alpha runner-state bundled create flow adds a multipart shape
    to the same endpoint; this JSON body remains the existing
    session-create contract for clients that already uploaded an agent.

    :param agent_id: Durable identifier or template-agent name to
        bind, e.g. ``"ag_abc123"`` or ``"chief-of-staff"``.
        Must match a registered agent.
    :param initial_items: Initial queued events/inputs, typically a
        single user ``"message"``.
    :param title: Optional human-readable title for the session,
        e.g. ``"debugging auth flow"``.
    :param labels: Initial guardrails labels to set on the session.
    :param parent_session_id: Parent session for sub-agent spawns.
        When set, the server inherits the parent's ``runner_id``
        affinity and sets ``parent_conversation_id`` on the child
        conversation. ``None`` for top-level sessions.
    :param sub_agent_name: For sub-agent sessions, the sub-agent
        type name within the parent's spec tree, e.g.
        ``"summarizer"``. The runner uses this to load the correct
        sub-spec instead of the parent's. ``None`` for top-level.
    :param host_type: How the session's host is obtained.
        ``"external"`` (the default, and the pre-existing behavior):
        the session runs on a host the caller manages — either a
        host they registered via ``omnigent host`` (pass
        ``host_id``) or a caller-managed runner (no ``host_id``).
        ``"managed"``: the SERVER provisions a sandbox host from its
        ``sandbox:`` config and binds the session to it —
        ``host_id`` and ``workspace`` must NOT be set (the server
        chooses both). Provisioning happens in the BACKGROUND: the
        create returns immediately with ``host_id`` / ``workspace``
        still null, and they appear on the session snapshot once
        the sandbox host registers. A message posted before then
        waits for the launch to settle instead of failing with
        "no runner bound".
    :param host_id: Optional host to launch the runner on, e.g.
        ``"host_a1b2c3d4..."``. When set, the server triggers the
        host launch flow (generate binding token, write runner_id,
        send launch frame). ``None`` for CLI-initiated sessions.
        Must be ``None`` when ``host_type`` is ``"managed"``.
    :param workspace: Where the session works. For external hosts:
        an absolute path on the host where the runner should start,
        e.g. ``"/Users/corey/universe/src/foo"``. Required when
        ``host_id`` is set; the server validates that the path
        exists, falls within the agent's ``os_env.cwd`` boundary,
        and contains any subdirectory the agent expects (per
        designs/SESSION_WORKSPACE_SELECTION.md). Tilde paths
        (``~/foo``) and relative paths are rejected — the server
        does not expand ``~``. Optional for CLI-initiated sessions
        that record their starting cwd for display. For
        ``host_type: "managed"``: optionally a git repository URL
        with a ``#<branch>`` fragment, e.g.
        ``"https://github.com/org/repo#main"`` or
        ``"git@github.com:org/repo.git"`` — the server clones it
        inside the sandbox and the cloned directory becomes the
        stored session workspace (paths are rejected; ``None``
        gives an empty server-created workspace).
    :param git: Optional git worktree options. When set, the server
        creates a worktree for a new branch on the host and starts
        the runner in it; ``workspace`` is then interpreted as the
        source repository directory. Requires ``host_id``. ``None``
        starts the runner directly in ``workspace``. See
        designs/SESSION_GIT_WORKTREE.md.
    :param terminal_launch_args: Optional pass-through CLI args for a
        native terminal wrapper (claude / codex), e.g.
        ``["--permission-mode", "bypassPermissions"]`` (the web UI's
        permission-mode selector). Set at create-time so the runner has
        them on the session row before it auto-launches the terminal.
        Bounds (count / length) are validated server-side. ``None`` for
        non-native sessions. Mirrors the multipart create path
        (:class:`SessionCreateMetadata`). See
        designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param model_override: Optional per-session LLM model override to
        persist at create time, e.g. ``"databricks-claude-sonnet-4-6"``.
        Set by ``sys_session_send``'s per-dispatch ``model`` arg so the
        value is on the session row before the runner launches the
        harness (native CLIs read it as ``--model`` at terminal launch;
        SDK harnesses via the spawn env). Validated server-side against
        a conservative model-id charset. ``None`` = harness default.
    :param cost_control_mode_override: Optional per-session
        cost-control switch to persist at create time: ``"on"``
        activates the spec's configured cost-control mode, ``"off"``
        disables cost control for this session. ``None`` (the
        default) defers to the spec default. Set by the web UI's
        new-session "Cost Optimized" option; read by the cost-control
        advisor pipeline at turn start.
    :param harness_override: Optional per-session brain-harness
        override to persist at create time, e.g. ``"pi"`` or
        ``"openai-agents"``. Set by the web UI's new-chat harness
        picker; the runner uses it instead of the agent spec's
        ``executor.config.harness`` when spawning the harness for
        this session. Validated server-side: must canonicalize into
        ``OMNIGENT_HARNESSES`` and the bound agent must be an
        ``executor.type: omnigent`` spec. ``None`` (the default) uses
        the spec's declared harness. Create-time only — there is no
        PATCH path, since the harness process spawns on the first
        turn.
    """

    agent_id: str
    initial_items: list[SessionEventInput] = Field(default_factory=list)
    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    parent_session_id: str | None = None
    sub_agent_name: str | None = None
    host_type: Literal["external", "managed"] = "external"
    host_id: str | None = None
    workspace: str | None = None
    git: SessionGitOptions | None = None
    terminal_launch_args: list[str] | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None
    harness_override: str | None = None
    # Bind-or-resume / idempotency correlation key (BDP-2390, ADR-0149).
    # When set, a repeat create with the same key returns the existing
    # session instead of creating a duplicate. May also be supplied via
    # the ``Idempotency-Key`` request header. ``None`` = no correlation.
    external_key: str | None = None

    @model_validator(mode="after")
    def _check_git_requires_host(self) -> SessionCreateRequest:
        """
        Reject ``git`` without ``host_id`` at validation time.

        Worktree creation runs on a host (the server has no
        filesystem), so ``git`` is meaningless without ``host_id``.
        Failing here returns 422 instead of letting the request reach
        the worktree path and fail late.

        :returns: The validated instance.
        :raises ValueError: If ``git`` is set but ``host_id`` is not.
        """
        if self.git is not None and self.host_id is None:
            raise ValueError("git worktree creation requires host_id")
        return self

    @model_validator(mode="after")
    def _check_managed_host_fields(self) -> SessionCreateRequest:
        """
        Enforce the per-``host_type`` workspace and host-id contract.

        A managed session's host is chosen by the server (sandbox
        provisioning), so a caller-supplied ``host_id`` is a
        contradiction. Its ``workspace``, when given, must be a git
        repository URL (optionally ``#<branch>``) the server clones
        into the sandbox — a path points at nothing in a sandbox that
        doesn't exist yet. Conversely, a repository-URL workspace on
        an external host is rejected: there, ``workspace`` is an
        absolute path on the host. Failing at validation returns a
        422 with the field named instead of silently ignoring the
        caller's intent.

        :returns: The validated instance.
        :raises ValueError: On ``"managed"`` + ``host_id``, a managed
            workspace that isn't a valid repository URL, or an
            external repository-URL workspace.
        """
        # Lazy import: schemas is imported by nearly every module, so
        # pulling the (FastAPI/click-importing) managed-hosts module in
        # at module scope would risk import cycles.
        from omnigent.server.managed_hosts import is_repo_workspace, parse_repo_workspace

        if self.host_type == "managed":
            if self.host_id is not None:
                raise ValueError(
                    "host_type 'managed' lets the server provision the host; "
                    "host_id must not be set"
                )
            if self.workspace is not None:
                try:
                    parse_repo_workspace(self.workspace)
                except ValueError as exc:
                    raise ValueError(
                        "host_type 'managed' takes a git repository URL "
                        f"(optionally '#<branch>') as workspace: {exc}"
                    ) from exc
        elif self.workspace is not None and is_repo_workspace(self.workspace):
            raise ValueError(
                "a repository-URL workspace requires host_type 'managed' — "
                "external hosts take an absolute path on the host"
            )
        return self


class SessionCreateMetadata(BaseModel):
    """
    Metadata JSON part for multipart ``POST /v1/sessions``.

    The uploaded agent tarball supplies the agent spec. This JSON
    part carries only session-level metadata so request metadata
    cannot disagree with the agent bundle.

    :param title: Optional human-readable title for the session,
        e.g. ``"debugging auth flow"``.
    :param labels: Initial guardrails labels to set on the
        session. Empty dict (the default) starts with no labels.
    :param reasoning_effort: Optional per-session reasoning-effort
        hint. Accepted metadata values are ``"none"``,
        ``"minimal"``, ``"low"``, ``"medium"``, ``"high"``,
        ``"xhigh"``, and ``"max"``. Provider-specific support is
        validated when a turn executes. ``None`` means use the agent
        default.
    :param host_id: Optional host to launch the runner on, e.g.
        ``"host_a1b2c3d4..."``. When set, the server generates a
        binding token, writes the expected runner_id to the session
        row, and sends a ``host.launch_runner`` frame to the host.
        ``None`` for CLI-initiated sessions where the caller
        manages runner spawning.
    :param workspace: Absolute path on the host where the runner
        should start, e.g. ``"/Users/corey/universe/src/foo"``.
        Required when ``host_id`` is set; validated against the
        uploaded agent's ``os_env.cwd`` boundary at session create
        (per designs/SESSION_WORKSPACE_SELECTION.md). Optional
        otherwise.
    :param terminal_launch_args: Optional pass-through CLI args for a
        native terminal wrapper (claude / codex), e.g.
        ``["--dangerously-skip-permissions"]``. Set at create-time so
        the runner has them before it boots. Bounds (count / length)
        are validated server-side. ``None`` for non-native sessions.
        See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param parent_session_id: Optional parent session id, e.g.
        ``"conv_abc123"``. When set, the new session is created as a
        sub-agent child of that session (``kind="sub_agent"``) and
        inherits the parent's runner binding for co-location. The
        caller must have READ access to the parent. ``None``
        creates a top-level session.
    """

    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str | None = None
    host_id: str | None = None
    workspace: str | None = None
    terminal_launch_args: list[str] | None = None
    parent_session_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class CreatedSessionResponse(BaseModel):
    """
    Response body for multipart ``POST /v1/sessions``.

    :param session_id: Identifier of the newly created session,
        e.g. ``"conv_abc123"``.
    :param agent_id: Identifier of the session-scoped agent created
        from the uploaded bundle, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded bundle's
        spec, e.g. ``"code-assistant"``.
    """

    session_id: str
    agent_id: str
    agent_name: str


class SessionLabelsResponse(BaseModel):
    """
    Lightweight response body for ``GET /v1/sessions/{id}/labels``.

    :param id: Session identifier, e.g. ``"conv_abc123"``.
    :param labels: Session-scoped guardrails labels. Empty dict when
        no labels have been written.
    """

    id: str
    labels: dict[str, str] = Field(default_factory=dict)


# Stages of a managed-sandbox launch, in pipeline order: the sandbox
# is provisioned, the repository workspace is cloned into it (skipped
# when the session has no repo workspace), the in-sandbox host starts
# and registers, and the agent runner is launched on it. ``ready`` and
# ``failed`` are terminal.
SandboxLaunchStage = Literal[
    "provisioning",
    "cloning",
    "starting",
    "connecting",
    "ready",
    "failed",
]


class SandboxStatus(BaseModel):
    """
    Managed-sandbox launch progress for a ``host_type="managed"`` session.

    Carried on the session snapshot only while the session's
    background sandbox launch is in flight or has failed; ``None``
    for sessions without a managed launch and once the launch
    succeeds (the session then looks like any host-bound session).

    :param stage: Current launch stage, e.g. ``"provisioning"`` —
        one of :data:`SandboxLaunchStage`, in pipeline order:
        ``provisioning`` (creating the sandbox) → ``cloning``
        (cloning the repository workspace; skipped when the session
        has none) → ``starting`` (starting the in-sandbox host) →
        ``connecting`` (launching the agent runner) → ``ready`` /
        ``failed``.
    :param error: Failure detail when ``stage == "failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` otherwise.
    """

    stage: SandboxLaunchStage
    error: str | None = None


class ModelUsage(BaseModel):
    """
    Cumulative token/cost usage attributed to a single LLM model.

    One value in the ``usage_by_model`` map on :class:`SessionResponse` /
    :class:`SessionUsageEvent`, keyed by the raw harness-reported model id
    (e.g. ``"claude-sonnet-4-6"``, ``"databricks-gpt-5-5"``). Counts are
    summed over the session's subtree (itself + sub-agent descendants), so a
    parent folds in sub-agents that ran a different model. Token buckets
    mirror the flat per-session breakdown.

    :param input_tokens: Cumulative non-cached input (prompt) tokens for this
        model over the subtree, e.g. ``12000``. ``None`` when not recorded.
    :param output_tokens: Cumulative output (completion) tokens, e.g.
        ``3400``. ``None`` when not recorded.
    :param total_tokens: Cumulative total tokens (counts cache buckets too,
        as the harness reports), e.g. ``15400``. ``None`` when not recorded.
    :param cache_read_input_tokens: Cumulative tokens read from the prompt
        cache, e.g. ``8000``. ``None`` when not recorded.
    :param cache_creation_input_tokens: Cumulative tokens written to the
        prompt cache, e.g. ``2000``. ``None`` when not recorded.
    :param total_cost_usd: Cumulative USD spend attributed to this model,
        e.g. ``0.42``. Present **only when this model's turns were priced**
        (same "priced ⟺ key present" contract as the session total); ``None``
        when the model is unpriced, so the sum of priced per-model costs
        equals the session ``total_cost_usd``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: float | None = None


class SessionResponse(BaseModel):
    """
    API representation of a session.

    Returned by ``POST /v1/sessions``, ``GET /v1/sessions/{id}``,
    and ``PATCH /v1/sessions/{id}``.

    :param id: Unique session identifier (also the underlying
        conversation ID), e.g. ``"conv_abc123"``.
    :param agent_id: Durable identifier of the bound agent,
        e.g. ``"ag_abc123"``. Stable across renames of the
        agent.
    :param agent_name: Human-readable name of the bound agent,
        e.g. ``"research-agent"``. Loaded from the agent row at
        snapshot-build time. ``None`` when the agent row cannot
        be found (deleted or orphaned session).
    :param status: Session lifecycle status. One of
        ``"idle"`` (no loop running), ``"running"`` (loop
        executing), or ``"failed"`` (terminal failure).
    :param created_at: Unix epoch seconds of creation.
    :param title: Optional human-readable title, e.g.
        ``"debugging auth flow"``. ``None`` when unset.
    :param labels: Session-scoped guardrails labels. Empty dict
        when no labels have been written.
    :param runner_id: Runner currently bound to this session, e.g.
        ``"runner_abc123"``. ``None`` until a client binds one via
        ``PATCH /v1/sessions/{id}``.
    :param host_id: Host that launched (or should launch) the
        runner for this session, e.g. ``"host_a1b2c3d4..."``.
        ``None`` for CLI-initiated sessions.
    :param tenant_id: Tenant this session belongs to, resolved from
        the request Principal at create time (ADR-0149, BDP-2388).
        ``None`` for single-org / local sessions (today's default).
    :param runner_online: Strict runner liveness — ``True`` iff a
        runner tunnel is currently registered for this session.
        This is the sole reachability signal: ``True`` means the
        client can chat normally. It does **not** fold in
        host-relaunch optimism (a dead runner on a live host reads
        ``False`` here, not ``True``) — the open-session view pairs
        it with ``host_online`` to decide what to show. ``None``
        when the server has no runner liveness lookup wired.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within the host liveness TTL).
        ``None`` when the session has no ``host_id`` (CLI/local).
        Used only to choose what the open view shows when
        ``runner_online`` is ``False`` — host alive ⇒ "send a
        message to wake the runner"; host dead ⇒ "reconnect /
        fork". Never participates in the reachability decision.
    :param reasoning_effort: Per-session reasoning-effort hint.
        Accepted metadata values are ``"none"``, ``"minimal"``,
        ``"low"``, ``"medium"``, ``"high"``, ``"xhigh"``, and
        ``"max"``. Provider-specific support is validated when a
        turn executes. ``None`` means use the agent default.
    :param items: Committed conversation items in chronological
        order. Empty for a freshly created session.
    :param sub_agent_name: For sub-agent sessions, the sub-agent
        type name within the parent's spec tree, e.g.
        ``"summarizer"``. ``None`` for top-level sessions.
    :param parent_session_id: For sub-agent sessions, the parent
        conversation's id, e.g. ``"conv_parent987"``. ``None`` for
        top-level sessions. Lets clients identify a session as a
        child and link back to its parent without an extra
        round-trip — the same conversation row exposes this via
        ``parent_conversation_id`` internally.
    :param root_conversation_id: The id of this session's spawn-tree
        root, e.g. ``"conv_root1"``. Equals ``id`` for top-level
        sessions; for sub-agents it points at the top-level ancestor.
        Lets orchestration tools (e.g. ``sys_session_close``) confirm
        a target shares the caller's spawn tree over the REST path.
        ``None`` only when the underlying row predates the
        ``root_conversation_id`` column (not expected post-migration).
    :param permission_level: The requesting user's numeric
        permission level on this session: ``1`` = read, ``2`` =
        edit, ``3`` = manage. ``None`` when permissions are
        disabled (single-user mode without a permission store).
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when the agent has no explicit ``llm:`` block or
        the agent cannot be looked up.
    :param harness: The bound agent's canonical harness, e.g.
        ``"claude-sdk"`` or ``"openai-agents"``. Lets the client
        render the active credential for the correct provider
        family instead of inferring it from the model string (which
        is wrong when the agent declares no model). ``None`` when
        the agent cannot be looked up.
    :param model_override: Per-session LLM model override,
        e.g. ``"claude-opus-4-7"``. ``None`` means no override is
        active (the agent's ``llm_model`` applies). Set via
        ``PATCH /v1/sessions/{id}`` or the REPL's ``/model``
        command; both write the same column so the ap-web UI and
        the TUI stay in sync.
    :param cost_control_mode_override: Per-session cost-control
        switch: ``"on"`` activates the spec's configured cost-control
        mode, ``"off"`` disables cost control for this session.
        ``None`` means no override is active (the spec default
        applies). Set at create time or via
        ``PATCH /v1/sessions/{id}`` (the web "Cost Optimized"
        toggle); read by the cost-control advisor pipeline.
    :param context_window: The model's context window size in tokens
        as looked up server-side from litellm's registry (or from the
        ``AP_CONTEXT_WINDOW_OVERRIDE`` env var), e.g. ``200_000``.
        ``None`` when the model is not in litellm's registry and no
        override is set.
    :param last_total_tokens: Total token count (input + output) from
        the most recently completed task's ``usage``, e.g. ``45231``.
        ``None`` when no task has completed yet. Lets clients seed
        their context-ring on conversation resume without waiting for
        the next ``response.completed`` SSE event.
    :param total_cost_usd: Cumulative LLM spend for this session in
        USD, e.g. ``0.42``. ``None`` when the session is **unpriced**
        — no turn has been priced yet (the model is absent from the
        pricing catalog, or no usage has been recorded) — so clients
        render "—" rather than a misleading ``$0.00``. Server-computed
        (cache-aware for relay/codex, exact billing for claude-native),
        the same total the cost-budget policy gates on. Lets clients
        seed their cost indicator on resume without waiting for the
        next ``session.usage`` SSE event.
    :param usage_by_model: Per-model breakdown of the same subtree usage,
        keyed by the raw harness model id, e.g.
        ``{"claude-sonnet-4-6": ModelUsage(input_tokens=12000, ...)}``.
        ``None`` when no per-model usage has been recorded (older sessions
        recorded before this field existed, or before the first turn). Lets
        the UI show which models a session spent its tokens / budget on.
    :param last_task_error: Error details from the most recently
        failed task. Only present when ``status == "failed"`` and
        the task stored an error. Lets clients display the failure
        reason on historical load without relying on the transient
        ``response.error`` SSE event (which may have been emitted
        before the web client subscribed). Format mirrors the
        ``RetryErrorDetail`` SSE shape:
        ``{"code": "executor_error", "message": "..."}``.
        ``None`` in all other cases.
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. a Claude Code session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Populated by the wrapper bridge.
    :param terminal_launch_args: Pass-through CLI args the native
        terminal wrapper (claude / codex) was launched with, e.g.
        ``["--dangerously-skip-permissions"]``. ``None`` for
        non-native sessions or a native session launched with none.
        Lets the launcher reproduce the command on resume.
    :param pending_elicitations: Outstanding approval prompts on
        this session at the moment the snapshot was built — the
        original ``response.elicitation_request`` event dicts.
        Lets the UI render the ApprovalCard on cold load, since
        the live SSE stream has no replay and a prompt emitted
        before the user opened the chat would otherwise vanish.
        Empty list when no prompts are outstanding. Sourced from
        the Omnigent server's in-memory
        :mod:`omnigent.runtime.pending_elicitations` index.
    :param pending_inputs: Un-consumed web-composer user messages on
        native-terminal (claude-native / codex-native) sessions at
        snapshot time, each ``{"pending_id", "content"}``. Native
        sessions don't persist a web message at POST time (the
        transcript forwarder is the single writer), so a client that
        posted then navigated away / rebound would lose its optimistic
        bubble; replaying these re-hydrates it. Empty list otherwise.
        Sourced from the in-memory
        :mod:`omnigent.runtime.pending_inputs` index.
    :param workspace: Absolute path on disk where the runner cd's,
        e.g. ``"/Users/corey/universe/src/foo"``. Set when the
        session was bound to a host workspace at create-time, or
        when the CLI captured ``os.getcwd()`` at session-create.
        Always ``None`` when not yet validated against a host. When a
        git worktree was created for the session, this is the
        worktree directory path.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree;
        ``None`` otherwise. The Web UI uses a non-``None`` value to
        offer the "delete local branch" cleanup checkbox on session
        delete. See designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are hidden from the default sidebar listing and
        surface only behind the "Show archived" toggle. ``False``
        for normal sessions. Toggled via ``PATCH /v1/sessions/{id}``.
    :param todos: Current Claude Code todo list items for
        ``omnigent claude`` sessions, as raw dicts from Claude's
        todo JSON file. Each dict has ``content``, ``status``,
        and ``activeForm`` keys. Empty list for non-claude-native
        sessions or when no todos have been reported yet. Sourced
        from the Omnigent server's in-memory ``_session_todos_cache``.
    :param skills: Skills the bound agent has access to — the
        merged result of the agent spec's bundled ``skills``
        and the host-scope skills discovered along the agent
        workdir / ``~/.claude/skills/`` (subject to the spec's
        ``skills_filter``). Mirrors what the TUI passes to the
        runner at startup. Empty list when the agent spec
        cannot be loaded, or when bundled + host discovery
        yields nothing.
    :param terminal_pending: ``True`` while the runner is auto-creating
        a terminal-first session's terminal (claude-native /
        codex-native), so the Web UI shows a spinner on the Terminal
        pill instead of a silent greyed-out button. Cleared to
        ``False`` once the terminal lands or auto-create fails; from
        then on the client relies purely on whether a terminal resource
        exists. Sourced from the Omnigent server's in-memory
        ``_session_terminal_pending_cache`` at snapshot build time, so a
        client connecting mid-spin-up still sees the spinner.
    :param sandbox_status: Managed-sandbox launch progress while the
        session's background sandbox launch is in flight or has
        failed — see :class:`SandboxStatus`. ``None`` for sessions
        without a managed launch and once the launch succeeds.
        Sourced from the Omnigent server's in-memory
        ``_session_sandbox_status_cache`` at snapshot build time, so
        a client opening the session mid-launch sees the current
        stage.
    """

    id: str
    agent_id: str
    agent_name: str | None = None
    status: Literal["idle", "running", "failed"]
    created_at: int
    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    runner_id: str | None = None
    host_id: str | None = None
    tenant_id: str | None = None
    external_key: str | None = None
    runner_online: bool | None = None
    host_online: bool | None = None
    reasoning_effort: str | None = None
    items: list[ConversationItem] = Field(default_factory=list)
    permission_level: int | None = None
    sub_agent_name: str | None = None
    parent_session_id: str | None = None
    root_conversation_id: str | None = None
    llm_model: str | None = None
    harness: str | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    total_cost_usd: float | None = None
    usage_by_model: dict[str, ModelUsage] | None = None
    last_task_error: dict[str, str] | None = None
    external_session_id: str | None = None
    terminal_launch_args: list[str] | None = None
    pending_elicitations: list[dict[str, Any]] = Field(default_factory=list)
    # Un-consumed web-composer user messages on native-terminal
    # sessions at snapshot time, each ``{"pending_id", "content"}``.
    # Replayed so a client that posted a message then navigated away /
    # rebound re-hydrates the optimistic bubble (the live SSE stream
    # has no replay). Empty for non-native sessions, which persist the
    # message at POST time and thus already carry it in ``items``.
    # Source: :mod:`omnigent.runtime.pending_inputs`.
    pending_inputs: list[dict[str, Any]] = Field(default_factory=list)
    workspace: str | None = None
    git_branch: str | None = None
    archived: bool = False
    todos: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[SkillSummary] = Field(default_factory=list)
    terminal_pending: bool = False
    sandbox_status: SandboxStatus | None = None


class UpdateSessionRequest(BaseModel):
    """
    Request body for ``PATCH /v1/sessions/{id}``.

    The Alpha runner-state pivot makes this endpoint the mutable
    session affinity primitive when ``runner_id`` is provided. The
    server validates that the runner is online, then replaces
    ``conversations.runner_id``. Existing session metadata updates
    remain supported for clients that update title, labels, or
    reasoning effort through the sessions API.

    :param runner_id: Identifier of a registered runner, e.g.
        ``"runner_abc123"``. ``None`` leaves runner binding
        unchanged.
    :param title: New title, e.g. ``"debugging auth flow"``.
        ``None`` leaves unchanged.
    :param labels: Guardrails labels to upsert. Merges with existing
        labels; keys not present are left untouched.
    :param reasoning_effort: Per-session reasoning-effort hint.
        Accepted metadata values are ``"none"``, ``"minimal"``,
        ``"low"``, ``"medium"``, ``"high"``, ``"xhigh"``, and
        ``"max"``. Provider-specific support is validated when a
        turn executes. Clear aliases such as ``"default"`` remove
        the session override. ``None`` leaves unchanged.
    :param model_override: Per-session LLM model override, e.g.
        ``"claude-opus-4-7"``. The value is forwarded as-is to the
        executor at turn start; the server does not enumerate valid
        models. Clear aliases such as ``"default"``, ``"off"``, or
        ``"reset"`` remove the override (matching the REPL's
        ``/model`` semantics). ``None`` leaves unchanged.
    :param cost_control_mode_override: Per-session cost-control
        switch: ``"on"`` activates the spec's configured cost-control
        mode, ``"off"`` disables cost control for this session.
        Explicit JSON ``null`` clears the override back to the spec
        default; omitting the field leaves it unchanged (``"off"`` is
        a real value here, so the field's *presence* — not a clear
        alias — is the clear signal, unlike ``model_override``).
    :param external_session_id: Runtime-native session id captured
        by a wrapper bridge (e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions). Idempotent on same-value
        writes; the server rejects attempts to overwrite an
        already-set different value with ``invalid_input`` to
        surface programmer errors. ``None`` leaves unchanged.
    :param terminal_launch_args: Per-session native-terminal
        pass-through args, e.g. ``["--dangerously-skip-permissions"]``.
        A list (including ``[]``) replaces the stored value wholesale
        — resume is last-write-wins, never an append. Bounds (count /
        length) are validated server-side. ``None`` leaves unchanged.
    :param silent: When ``True``, persist metadata changes but skip
        the runner-side side effects — specifically the
        claude-native ``/effort`` and ``/model`` slash-command
        forwards into the tmux pane. Used by automatic bind-time
        handoffs (ap-web's sticky-pref apply on session switch, the
        REPL's pre-create ``/model`` snapshot) where injecting a
        visible slash command into a freshly-spawned pane would
        render as an unexpected "Command model X" item before the
        user has sent anything. Default ``False`` preserves the
        user-driven picker / ``/model`` behaviour where the live
        forward IS the desired feedback.
    :param archived: New archived state. ``True`` archives (hides the
        session from the default sidebar listing), ``False`` unarchives,
        ``None`` leaves unchanged. Owner-only (unlike ``title``, which
        needs only edit access).
    """

    runner_id: str | None = None
    title: str | None = None
    labels: dict[str, str] | None = None
    reasoning_effort: str | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None
    external_session_id: str | None = None
    terminal_launch_args: list[str] | None = None
    archived: bool | None = None
    silent: bool = False

    model_config = ConfigDict(extra="forbid")


class SessionForkRequest(BaseModel):
    """
    Request body for ``POST /v1/sessions/{source_id}/fork``.

    Creates a deep copy of an existing session's items into a new
    session. All fields are optional.

    :param title: Title for the forked session. When ``None``, the
        server derives ``"Fork of <source_title>"``.
    :param agent_id: Built-in agent id or name to bind the fork to,
        switching it away from the source's agent/harness (e.g. fork a
        Claude session into a Codex one, or a Claude-SDK session into
        Claude Code). When ``None``, the fork keeps the source's agent.
        Must be a built-in agent (one listed by ``GET /v1/agents``).
    :param up_to_response_id: Truncation point for the copied history,
        e.g. ``"resp_abc123"``. When set, only items up to and including
        the last item of that response are copied — items after it are
        dropped from the fork. When ``None`` (default), the full history
        is copied.
    """

    title: str | None = None
    agent_id: str | None = None
    up_to_response_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class SessionSwitchAgentRequest(BaseModel):
    """
    Request body for ``POST /v1/sessions/{id}/switch-agent``.

    Rebinds an existing session in place to a different agent/harness,
    keeping the same session (transcript, comments, files, workspace).
    Unlike fork, no new session is created.

    :param agent_id: Built-in agent id or name to switch the session
        to, e.g. ``"ag_builtin_codex"`` or ``"codex-native-ui"``.
        Must be a built-in agent (one listed by ``GET /v1/agents``)
        and different from the session's current agent.
    """

    agent_id: str

    model_config = ConfigDict(extra="forbid")


class SessionListItem(BaseModel):
    """
    Lightweight session summary for ``GET /v1/sessions`` list responses.

    Same shape as :class:`SessionResponse` minus ``items``.

    :param id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param agent_id: Durable identifier of the bound agent.
    :param agent_name: Human-readable name of the bound agent,
        e.g. ``"research-agent"``. ``None`` when the agent row
        cannot be found.
    :param agent_display_name: Human display name of the bound agent
        from its bundle ``params.displayName`` (e.g. ``"Maya Chen"``),
        so session-bound surfaces render the person's name, not the
        slug. ``None`` when the bundle sets none or can't be loaded.
    :param status: Derived session lifecycle status.
    :param created_at: Unix epoch seconds of creation.
    :param updated_at: Unix epoch seconds of last update.
    :param title: Optional human-readable title.
    :param labels: Session-scoped guardrails labels.
    :param runner_id: Runner currently bound to the session.
    :param host_id: Host that launched the runner for this session.
    :param runner_online: Strict runner liveness — ``True`` iff a
        runner tunnel is currently registered for this session.
        Matches ``GET /health``'s ``runner_online`` value. Strict:
        a dead runner on a live host reads ``False`` here (no
        host-relaunch optimism folded in), unlike the legacy
        conflated value. ``None`` when the server has no runner
        liveness lookup wired.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within the host liveness TTL).
        ``None`` when the session has no ``host_id`` (CLI/local).
        Distinguishes "runner down but host can relaunch" from
        "host offline" for the open-session view; not used by the
        sidebar.
    :param reasoning_effort: Per-session reasoning-effort hint.
    :param permission_level: The requesting user's numeric
        permission level on this session: ``1`` = read, ``2`` =
        edit, ``3`` = manage. ``None`` when permissions are
        disabled.
    :param owner: The user_id of the session owner, or ``None``
        when permissions are disabled. Included so the sidebar
        can display the owner without a separate API call.
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. a Claude Code session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Lets the sidebar / picker render
        a runtime badge without a follow-up GET.
    :param pending_elicitations_count: Number of approval prompts
        currently waiting on this session. Powers the sidebar's
        "needs attention" badge so a user with several sessions
        running can tell which ones are blocked on them without
        opening each chat. Sourced from the Omnigent server's in-memory
        :mod:`omnigent.runtime.pending_elicitations` index,
        which mirrors every ``response.elicitation_request`` event
        passing through ``session_stream`` and decrements when a
        verdict is dispatched. ``0`` when the session has no
        outstanding elicitations.
    :param workspace: Absolute path on disk where the runner cd's,
        e.g. ``"/Users/corey/universe/src/foo"``. ``None`` for
        sessions that haven't been bound to a host workspace.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree;
        ``None`` otherwise. The Web UI uses a non-``None`` value to
        offer the "delete local branch" cleanup checkbox on session
        delete. See designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are returned by ``GET /v1/sessions`` only when the
        request passes ``include_archived=true``; the sidebar groups
        them into a dedicated "Archived" section. ``False`` for
        normal sessions.
    :param comments_count: Total number of review comments (any
        status) on this session. Together with
        ``comments_updated_at`` it forms a change fingerprint: an
        add or edit bumps the timestamp, a delete changes the count,
        so the web client can invalidate its cached comment list
        when either field changes in a ``WS /v1/sessions/updates``
        frame. ``0`` when the session has no comments or the server
        has no comment store wired.
    :param comments_updated_at: Unix epoch **microseconds** of the
        most recently mutated comment on this session (max
        ``updated_at`` across its comments). Microsecond precision
        keeps back-to-back mutations within one second
        distinguishable while staying an exact integer in JavaScript;
        clients only compare it for change. ``None`` when the session
        has no comments or the server has no comment store wired.
    """

    id: str
    agent_id: str
    agent_name: str | None = None
    agent_display_name: str | None = None
    status: Literal["idle", "running", "failed"]
    created_at: int
    updated_at: int
    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    runner_id: str | None = None
    host_id: str | None = None
    runner_online: bool | None = None
    host_online: bool | None = None
    reasoning_effort: str | None = None
    permission_level: int | None = None
    owner: str | None = None
    external_session_id: str | None = None
    pending_elicitations_count: int = 0
    workspace: str | None = None
    git_branch: str | None = None
    archived: bool = False
    comments_count: int = 0
    comments_updated_at: int | None = None


# ── Permissions ────────────────────────────────────────────────────


class GrantPermissionRequest(BaseModel):
    """
    Request body for ``PUT /v1/sessions/{id}/permissions``.

    :param user_id: The user to grant access to, e.g.
        ``"alice@example.com"`` or ``"__public__"`` for public
        read access.
    :param level: Numeric permission level: ``1`` = read,
        ``2`` = edit, ``3`` = manage.
    """

    user_id: str
    level: int = Field(ge=1, le=3)


class PermissionObject(BaseModel):
    """
    API representation of a session permission grant.

    :param user_id: The grantee, e.g. ``"alice@example.com"``.
    :param conversation_id: The session, e.g.
        ``"conv_abc123"``.
    :param level: Numeric permission level (1=read, 2=edit,
        3=manage).
    :param version: Optimistic-concurrency ETag — send back as
        ``If-Match`` on the next grant update (BDP-2412).
    """

    user_id: str
    conversation_id: str
    level: int
    version: int = 1


# ── Data-surface read models (Phase 9a, BDP-2444, ADR-0152) ──────────
#
# Additive, read-only projections over existing internal omnigent state
# that no other endpoint exposed: long-term memory, per-session/per-user
# cost, the spawn tree, pending elicitations, and fleet health. Mounted by
# ``omnigent.server.routes.data_surfaces``.


class MemoryObject(BaseModel):
    """
    One durable, weighted, decaying memory (FU1, ADR-0132).

    Projection of a :class:`omnigent.db.db_models.SqlMemory` row scoped to a
    session via ``source_conversation_id``. Pure read — no decay is applied
    here; ``weight`` is the stored salience.

    :param id: Memory identifier, e.g. ``"mem_abc123"``.
    :param object: Fixed resource type, always ``"memory"``.
    :param content: The remembered text.
    :param weight: Stored salience (pre-decay), e.g. ``1.0``.
    :param salience: Optional capture-time salience score, e.g. ``0.8``.
        ``None`` when not recorded.
    :param confidence: Optional capture-time confidence score. ``None`` when
        not recorded.
    :param created_at: Unix epoch seconds the memory was first written.
    :param last_accessed_at: Unix epoch seconds of the last reinforcement
        (drives the decay clock).
    :param access_count: Number of times the memory has been recalled.
    :param archived: Whether the memory has been evicted below the archive
        floor (excluded from recall).
    :param source_conversation_id: Session the memory was captured from, e.g.
        ``"conv_abc123"``. ``None`` for memories with no recorded source.
    """

    id: str
    object: str = "memory"
    content: str
    weight: float
    salience: float | None = None
    confidence: float | None = None
    created_at: int
    last_accessed_at: int
    access_count: int
    archived: bool
    source_conversation_id: str | None = None


class MemoryListResponse(BaseModel):
    """
    Page of session-scoped memories.

    Returned by ``GET /v1/sessions/{id}/memories``.

    :param object: Fixed resource type, always ``"list"``.
    :param data: The memories captured from this session, newest first.
    :param has_more: Whether more memories exist beyond ``limit``.
    """

    object: str = "list"
    data: list[MemoryObject] = Field(default_factory=list)
    has_more: bool = False


class UsageSummary(BaseModel):
    """
    Cumulative token + cost usage for a session subtree.

    Returned by ``GET /v1/sessions/{id}/usage/summary``. Counts are summed
    over the session and its sub-agent descendants (same subtree total the
    snapshot's ``usage`` field uses), so a parent folds in its sub-agents.

    :param input_tokens: Cumulative non-cached input (prompt) tokens.
    :param output_tokens: Cumulative output (completion) tokens.
    :param cache_read_input_tokens: Cumulative tokens served from the prompt
        cache (cache hits).
    :param cache_creation_input_tokens: Cumulative tokens written to the
        prompt cache (cache creation).
    :param total_tokens: Cumulative total tokens (billing total).
    :param total_cost_usd: Cumulative USD spend. ``None`` when no turn in the
        subtree was priced (same "priced ⟺ key present" contract as the
        snapshot).
    :param usage_by_model: Per-model breakdown keyed by the raw harness model
        id. ``None`` when no per-model usage was recorded.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float | None = None
    usage_by_model: dict[str, ModelUsage] | None = None


class DailyCostSummary(BaseModel):
    """
    A user's accumulated LLM spend for one UTC day.

    Returned by ``GET /v1/users/{user_id}/cost/daily``. Projection of the
    :class:`omnigent.db.db_models.SqlUserDailyCost` rollup row.

    :param date_utc: The UTC calendar day, ``"YYYY-MM-DD"``, e.g.
        ``"2026-06-24"``.
    :param cost_usd: Accumulated USD spend for the user on this day; ``0.0``
        when no row exists.
    :param ask_approved_usd: Highest soft cost-checkpoint (USD) the user has
        already approved continuing past this day; ``0.0`` when none. (The
        underlying column is ``ask_approved_usd`` — a USD checkpoint, not a
        count.)
    """

    date_utc: str
    cost_usd: float = 0.0
    ask_approved_usd: float = 0.0


class SpawnTreeMetadata(BaseModel):
    """
    Descriptive metadata for a node in a session spawn tree.

    :param sub_agent_name: For sub-agent nodes, the sub-agent type name within
        the parent's spec tree, e.g. ``"researcher"``. ``None`` for the root.
    :param title: The node's stored title, e.g. ``"researcher:auth"``. ``None``
        when untitled.
    :param created_at: Unix epoch seconds the node was created.
    :param last_activity_at: Unix epoch seconds the node was last updated.
    """

    sub_agent_name: str | None = None
    title: str | None = None
    created_at: int
    last_activity_at: int


class SpawnTree(BaseModel):
    """
    A session and its sub-agent descendants as a recursive tree.

    Returned by ``GET /v1/sessions/{id}/spawn-tree``. Walks
    ``parent_conversation_id`` within the shared ``root_conversation_id``.

    :param session_id: This node's session/conversation id.
    :param object: Fixed resource type, always ``"spawn_tree"``.
    :param agent_type: The node's sub-agent type, e.g. ``"researcher"``;
        ``"root"`` for the top-level session.
    :param status: Coarse lifecycle status — the live in-memory status when
        known (``"running"`` / ``"waiting"`` / ``"idle"`` / ``"failed"``),
        else ``"archived"`` / ``"closed"`` from durable markers, else
        ``"active"``.
    :param metadata: Descriptive metadata for this node.
    :param children: Sub-agent children, newest-first by ``created_at``.
        Empty for a leaf, or when ``depth`` cut off further descent.
    """

    session_id: str
    object: str = "spawn_tree"
    agent_type: str
    status: str
    metadata: SpawnTreeMetadata
    children: list[SpawnTree] = Field(default_factory=list)


class PendingElicitationItem(BaseModel):
    """
    A compact projection of one outstanding elicitation prompt.

    :param elicitation_id: Correlates the prompt to its reply, e.g.
        ``"elicit_abc123"``.
    :param prompt: The human-facing message, e.g. ``"Approve running 'rm'?"``.
        ``None`` when the payload carried no message.
    :param fields: For form-mode elicitations, the requested field names.
        ``None`` for non-form prompts.
    """

    elicitation_id: str | None = None
    prompt: str | None = None
    fields: list[str] | None = None


class SessionPendingElicitations(BaseModel):
    """
    Outstanding elicitations for one session.

    :param conversation_id: The session, e.g. ``"conv_abc123"``.
    :param pending_count: Number of outstanding prompts on this session.
    :param oldest_created_at: Unix epoch seconds of the oldest outstanding
        prompt. Always ``None`` — the in-memory pending index does not record
        per-prompt timestamps.
    :param elicitations: The compact prompt projections.
    """

    conversation_id: str
    pending_count: int
    oldest_created_at: int | None = None
    elicitations: list[PendingElicitationItem] = Field(default_factory=list)


class PendingElicitationsSummary(BaseModel):
    """
    Pending elicitations across the caller's accessible sessions.

    Returned by ``GET /v1/elicitations/pending``.

    :param total_count: Total outstanding prompts across the listed sessions.
    :param by_session: One entry per session with at least one outstanding
        prompt (scoped to sessions the caller can read).
    """

    total_count: int = 0
    by_session: list[SessionPendingElicitations] = Field(default_factory=list)


class FleetHealth(BaseModel):
    """
    Aggregate health of the hosts the caller can see.

    Returned by ``GET /v1/hosts/health``. Scoped to the same host set the
    caller would get from ``GET /v1/hosts`` (owner / visibility-scope filtered).

    :param total_hosts: Number of hosts in the caller's scope.
    :param online_hosts: Hosts online and seen within the liveness window.
    :param offline_hosts: ``total_hosts - online_hosts``.
    :param hosts_by_sandbox_provider: Count of hosts per sandbox provider; the
        ``"external"`` key counts user-connected (non-managed) hosts.
    :param avg_last_seen_seconds_ago: Mean age (seconds) of the hosts'
        last-seen timestamps. ``None`` when there are no hosts.
    """

    total_hosts: int = 0
    online_hosts: int = 0
    offline_hosts: int = 0
    hosts_by_sandbox_provider: dict[str, int] = Field(default_factory=dict)
    avg_last_seen_seconds_ago: float | None = None


# ─────────────────────────────────────────────────────────────────────
