"""Contract test pinning the structural invariants the abstraction-spine handoff
plan (``docs/architecture/abstraction-spine-handoff.md``, BDP-2327→2331) builds on.

The handoff plan is a sequential, parity-gated refactor of the *contended*
``omnigent/server/app.py`` factory behind three new abstractions (ServiceRegistry,
HarnessProvider, StoreBootstrapper) plus a tool-exec context and a tool-dispatch
registry. Each phase's diff lands on top of specific anchors in core files. If one of
those anchors silently moves (an ``app.state`` key renamed, a harness registration
site relocated, the dispatch registry reshaped, the extension seam getter renamed),
the plan's line-number claims rot and a phase rebases onto the wrong hunk.

This test freezes the anchors the plan quotes so any drift fails *here* — pointing the
implementer at the exact paragraph of the plan to re-pin — rather than surfacing as a
silent merge collision while building app.py. It is a pure source/AST scan: no FastAPI
app is booted, no DB is touched, so it is fast and deterministic on both SQLite and
Postgres CI matrices.

When a phase intentionally moves an anchor, update the matching constant here in the
same PR (don't delete the assert) so the plan and the code stay in lockstep.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Repo root = three parents up from tests/extensions/<thisfile>.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_PY = _REPO_ROOT / "omnigent" / "server" / "app.py"
_APP_CONTEXT = _REPO_ROOT / "omnigent" / "server" / "app_context.py"
_HARNESSES_INIT = _REPO_ROOT / "omnigent" / "runtime" / "harnesses" / "__init__.py"
_OMNIGENT_COMPAT = _REPO_ROOT / "omnigent" / "spec" / "_omnigent_compat.py"
_LIFESPAN_PHASES = _REPO_ROOT / "omnigent" / "kernel" / "lifespan_phases.py"
_TOOL_DISPATCH = _REPO_ROOT / "omnigent" / "runner" / "tool_dispatch" / "_dispatch.py"
_TOOL_DISPATCHER_REGISTRY = _REPO_ROOT / "omnigent" / "runner" / "tool_dispatcher_registry.py"
# Canonical kernel location post-BDP-2515 (omnigent/extensions.py is now a
# strangler re-export shim; the extension_*() getter defs live in the kernel).
_EXTENSIONS = _REPO_ROOT / "omnigent" / "kernel" / "extensions.py"
_HANDOFF_DOC = _REPO_ROOT / "docs" / "architecture" / "abstraction-spine-handoff.md"

# ── Phase 1 anchor: the typed app context's state projection. ──
# ServerAppContext owns the app-wide graph; bind_server_app_context(app, context)
# projects exactly these keys onto app.state for compatibility. A typo here silently
# breaks any router that reads request.app.state.<key>.
_EXPECTED_LEGACY_APP_STATE_KEYS = frozenset(
    {
        "runner_control_registry",
        "runner_router",
        # BDP-2623: reconcile pre-existing drift — app.py writes
        # ``app.state.runner_credential_store`` in the factory body but this pinned set
        # had never been updated to include it (the anchor was already stale on HEAD).
        "runner_credential_store",
        "auth_provider",
        "host_registry",
        "host_store",
        "sandbox_config",
        "agent_store",
        "file_store",
        "conversation_store",
        "artifact_store",
        "agent_cache",
        "comment_store",
        "policy_store",
        "permission_store",
        "runner_tunnel_tokens",
        "runner_exit_reports",
        "server_mcp_pool",
        "session_liveness_lookup",
        "push_subscription_store",
        "managed_launches",
        "server_metrics",
        "server_metrics_otel",
        # BDP-2424 / MCP acting-identity: optional HMAC signer for assertion tokens.
        "assertion_signer",
        # DI composition root (always-on; AST scan sees the source-level assignment).
        "di_container",
        # BDP-2623: the Phase 1 / ServiceRegistry dual-write sidecar
        # (OMNIGENT_USE_SERVICE_REGISTRY + omnigent/kernel/service_registry.py) was
        # deleted — never flipped in prod, zero consumers — so "service_registry" is no
        # longer written in the create_app body and is intentionally absent here.
    }
)
_EXPECTED_BODY_APP_STATE_KEYS = _EXPECTED_LEGACY_APP_STATE_KEYS | {"server_app_context"}
# ── Phase 3 anchor: the one app.state key set inside _lifespan (app.py:915). ──
# ``app_inst.state.harness_process_manager`` is assigned at lifespan-startup, NOT in
# the factory body — so it belongs to Phase 3 (lifespan phases), not Phase 1
# (ServiceRegistry). Keeping the two write-sites separate is part of WHY Phase 1 and
# Phase 3 don't collide on the same app.state hunk.
_EXPECTED_LIFESPAN_APP_STATE_KEYS = frozenset({"harness_process_manager"})
# Full set the AST scan sees across the whole module (body + lifespan).
_EXPECTED_ALL_APP_STATE_KEYS = _EXPECTED_BODY_APP_STATE_KEYS | _EXPECTED_LIFESPAN_APP_STATE_KEYS

# ── Phase 5 anchor: canonical dispatch registry order. ──
_EXPECTED_DISPATCHER_NAMES = (
    "mcp",
    "os_env",
    "rest",
    "file",
    "terminal",
    "async_inbox",
    "subagent",
    "list_models",
    "session_create",
    "session_query",
    "web_fetch",
    "timer",
    "task_lifecycle",
    "skill",
    "comment",
    "agent",
    "policy",
    "skill_acq",
    "spec_builtin",
    "local_python",
    "uc_function",
    "spec_callable",
)

# ── Phase 3/lifespan + Phase 1/harness anchors. ──
_EXPECTED_EXTENSION_GETTERS = frozenset(
    {
        "extension_tool_factories",
        "extension_policy_modules",
        "extension_secret_backends",
        "extension_background_factories",
    }
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assigned_app_state_keys(source: str, *, root_names: set[str]) -> set[str]:
    """Return every ``<root>.state.<key> = ...`` key assigned in *source* via AST.

    *root_names* selects the assignment owner: ``{"app"}`` for the synchronous
    ``create_app`` body and ``{"ctx"}`` for phase-owned ``ctx.app.state`` writes.
    AST (not regex) so a reformatted assignment block still resolves the same keys.
    """
    keys: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            # Match `<root>.state.<key>` →
            # Attribute(attr=key, value=Attribute(attr=state, value=Name(id=root))).
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "state"
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id in root_names
            ):
                keys.add(target.attr)
            # Match `<root>.app.state.<key>` as used by lifespan phases.
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "state"
                and isinstance(target.value.value, ast.Attribute)
                and target.value.value.attr == "app"
                and isinstance(target.value.value.value, ast.Name)
                and target.value.value.value.id in root_names
            ):
                keys.add(target.attr)
    return keys


def _context_projected_app_state_keys(source: str) -> set[str]:
    """Return the app.state keys projected by app_context.py."""
    tree = ast.parse(source)
    legacy_keys: set[str] = set()
    context_key: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "LEGACY_APP_STATE_KEYS"
            for target in targets
        ):
            if isinstance(value, ast.Tuple):
                legacy_keys = {
                    elt.value
                    for elt in value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
        if (
            any(
                isinstance(target, ast.Name) and target.id == "SERVER_APP_CONTEXT_STATE_KEY"
                for target in targets
            )
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            context_key = value.value
    assert context_key is not None, "SERVER_APP_CONTEXT_STATE_KEY literal not found"
    return legacy_keys | {context_key}


# ── Phase 1: ServiceRegistry binds exactly the app.state singletons ──


def test_app_state_body_singleton_key_set_is_pinned():
    """Phase 1 anchor — ServerAppContext projects the legacy app.state surface."""
    keys = _context_projected_app_state_keys(_read(_APP_CONTEXT))
    assert keys == _EXPECTED_BODY_APP_STATE_KEYS, (
        "ServerAppContext app.state projection drifted from the abstraction-spine "
        "contract. Update _EXPECTED_BODY_APP_STATE_KEYS and the architecture doc in "
        f"the same PR. Got: {sorted(keys)}"
    )
    assert _assigned_app_state_keys(_read(_APP_PY), root_names={"app"}) == set(), (
        "create_app should bind app.state via ServerAppContext, not scatter direct "
        "factory-body app.state writes."
    )


def test_app_state_lifespan_key_is_separate_from_body():
    """Phase 3 anchor — harness_process_manager is set by the lifespan phase.

    This separation is load-bearing for the 'why sequential' argument: Phase 1
    (ServiceRegistry) and Phase 3 (lifespan phases) touch DIFFERENT app.state write
    sites, so the spine never collides two phases on a single app.state hunk.
    """
    lifespan_keys = _assigned_app_state_keys(_read(_APP_PY), root_names={"app_inst"})
    lifespan_keys |= _assigned_app_state_keys(_read(_LIFESPAN_PHASES), root_names={"ctx"})
    assert lifespan_keys == _EXPECTED_LIFESPAN_APP_STATE_KEYS, (
        f"_lifespan app.state keys drifted (Phase 3). Got: {sorted(lifespan_keys)}"
    )
    # The two write sets are disjoint — the structural guarantee the plan relies on.
    assert _EXPECTED_BODY_APP_STATE_KEYS.isdisjoint(_EXPECTED_LIFESPAN_APP_STATE_KEYS)
    # And together they are the complete app.state surface (no third writer slipped in).
    all_keys = _context_projected_app_state_keys(_read(_APP_CONTEXT))
    all_keys |= _assigned_app_state_keys(_read(_APP_PY), root_names={"app_inst"})
    all_keys |= _assigned_app_state_keys(_read(_LIFESPAN_PHASES), root_names={"ctx"})
    assert all_keys == _EXPECTED_ALL_APP_STATE_KEYS, (
        "an unexpected app.state writer appeared (neither ServerAppContext nor the "
        f"_lifespan closure). Got: {sorted(all_keys)}"
    )


# ── Phase 1: HarnessProvider's backing registry + the 4 registration sites ──


def test_harness_modules_registry_is_the_provider_source_of_truth():
    """_HARNESS_MODULES is the dict HarnessProvider's default impl returns verbatim."""
    namespace: dict[str, object] = {}
    exec(  # executing first-party module source to read its constant
        compile(_read(_HARNESSES_INIT), str(_HARNESSES_INIT), "exec"),
        namespace,
    )
    modules = namespace["_HARNESS_MODULES"]
    assert isinstance(modules, dict)
    # Every value is a dotted module path that must export create_app() — the plan's
    # HarnessProvider.modules() returns this mapping unchanged.
    assert all(isinstance(v, str) and "." in v for v in modules.values())
    # The four registration sites the plan names are all present and consistent:
    # (1) at least one core harness, (2) at least one bytedesk_omnigent harness.
    assert any(v.startswith("omnigent.inner.") for v in modules.values())
    assert any(v.startswith("bytedesk_omnigent.") for v in modules.values())


def test_omnigent_compat_allowlist_consumes_the_same_harness_names():
    """OMNIGENT_HARNESSES (validator allowlist) must not drift from _HARNESS_MODULES.

    The plan's risk note calls this out: HarnessProvider has TWO readers
    (_lifespan start + the _omnigent_compat validator allowlist). Both read the same
    registry, so the provider's default impl returning _HARNESS_MODULES verbatim keeps
    the allowlist correct. Here we assert the allowlist is a frozenset of names that
    are a subset of the canonicalized harness universe (every allowlisted name resolves
    to a registered module or a documented alias).
    """
    hm_ns: dict[str, object] = {}
    exec(
        compile(_read(_HARNESSES_INIT), str(_HARNESSES_INIT), "exec"),
        hm_ns,
    )
    registered = set(hm_ns["_HARNESS_MODULES"].keys())  # type: ignore[union-attr]

    compat_src = _read(_OMNIGENT_COMPAT)
    # Pull the OMNIGENT_HARNESSES frozenset literal names out of the source by AST so we
    # don't import the module (which would pull heavy deps).
    tree = ast.parse(compat_src)
    allowlist: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "OMNIGENT_HARNESSES" for t in node.targets
        ):
            # frozenset({ "a", "b", ... }) → Call(args=[Set(elts=[Constant,...])]).
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    allowlist.add(sub.value)
    assert allowlist, "OMNIGENT_HARNESSES literal not found — _omnigent_compat.py moved"
    # Every allowlisted harness must be a registered key (the provider's invariant).
    # 'open-responses' is registered lazily by the executor factory, not in the dict,
    # so allow it as the single documented exception named in _omnigent_compat.py.
    missing = allowlist - registered - {"open-responses"}
    assert not missing, (
        "OMNIGENT_HARNESSES names harnesses absent from _HARNESS_MODULES "
        f"{sorted(missing)} — HarnessProvider would expose a name the validator "
        "rejects. Reconcile both, then update the plan's harness-provider row."
    )


# ── Phase 4/5: the canonical dispatch registry shape ──────────────────────


def test_dispatch_registry_order_is_pinned():
    src = _read(_TOOL_DISPATCHER_REGISTRY)
    communication_dispatchers = {
        "RestDispatcher": "rest",
        "FileDispatcher": "file",
        "InboxDispatcher": "async_inbox",
        "SessionSendDispatcher": "subagent",
        "SessionCreateDispatcher": "session_create",
        "SessionQueryDispatcher": "session_query",
        "AgentDispatcher": "agent",
        "PolicyDispatcher": "policy",
        "SkillAcquisitionDispatcher": "skill_acq",
    }
    names: list[str] = []
    for match in re.finditer(
        r'name="([^"]+)"|registry\.register\((\w+Dispatcher)\(',
        src,
    ):
        if match.group(1) is not None:
            names.append(match.group(1))
        elif match.group(2) in communication_dispatchers:
            names.append(communication_dispatchers[match.group(2)])
    names = tuple(names)
    assert names == _EXPECTED_DISPATCHER_NAMES, (
        f"dispatch registry order drifted: {names}. Add/remove the dispatcher in "
        "the canonical precedence position and re-pin _EXPECTED_DISPATCHER_NAMES."
    )

    # Predicate tails the registry enumerates: spec-builtin, spec-local-python,
    # UC-function, catch-all fallback.
    assert "_is_spec_builtin_tool(ctx.tool_name, ctx.agent_spec)" in src
    assert "_is_spec_local_python_tool(ctx.tool_name, ctx.agent_spec)" in src
    assert "_is_uc_function_tool(ctx.tool_name, ctx.agent_spec)" in src
    assert "_execute_spec_callable_tool(ctx.tool_name, args, agent_spec=ctx.agent_spec)" in src


def test_execute_tool_uses_registry_without_tool_dispatch_flags():
    src = _read(_TOOL_DISPATCH)
    assert "dispatch_via_registry" in src
    assert "OMNIGENT_USE_TOOL_EXECUTION_CONTEXT" not in src
    assert "OMNIGENT_USE_TOOL_DISPATCHER_REGISTRY" not in src


# ── Phase 1/3: the generic extension seam getters the plan mirrors ──


def test_extension_seam_getters_exist_and_keep_their_names():
    """The plan's three new spine abstractions mirror omnigent/extensions.py exactly.

    Phase 3 also re-uses extension_background_factories() for the lifespan bg-task
    cancel path. Pinning these names means the plan's seam references stay valid.
    """
    src = _read(_EXTENSIONS)
    defined = {node.name for node in ast.walk(ast.parse(src)) if isinstance(node, ast.FunctionDef)}
    missing = _EXPECTED_EXTENSION_GETTERS - defined
    assert not missing, (
        f"extension seam getters missing {sorted(missing)} — the abstraction-spine "
        "plan mirrors these conventions and Phase 3 calls "
        "extension_background_factories(). Update the plan if intentional."
    )
    # The discover/install split the plan's new abstractions copy must still exist.
    assert "discover_extensions" in defined
    assert "install_extensions" in defined


# ── The handoff doc itself stays internally consistent with the pinned numbers ──


def test_handoff_doc_quotes_the_pinned_branch_count():
    doc = _read(_HANDOFF_DOC)
    # The doc must reference the 20-branch chain (16 + 4).
    assert "20-branch" in doc
    assert "16 set-family" in doc or "16 set-family branches" in doc
    # And it must name all five sequential phase tickets in order.
    for key in ("BDP-2327", "BDP-2328", "BDP-2329", "BDP-2330", "BDP-2331"):
        assert key in doc, f"handoff doc is missing phase ticket {key}"
    # The five per-phase feature flags must each be documented.
    for flag in (
        "OMNIGENT_SPINE_SERVICES",
        "OMNIGENT_SPINE_STORES",
        "OMNIGENT_SPINE_LIFESPAN",
        "OMNIGENT_SPINE_TOOLCTX",
        "OMNIGENT_SPINE_TOOLREGISTRY",
    ):
        assert flag in doc, f"handoff doc is missing feature flag {flag}"
