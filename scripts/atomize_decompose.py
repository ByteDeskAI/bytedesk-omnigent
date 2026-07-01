#!/usr/bin/env python3
"""Structural decomposition of large Omnigent Python monoliths."""

from __future__ import annotations

import ast
import re
import shutil
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_lines(path: Path) -> list[str]:
    return path.read_text().splitlines(keepends=True)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def facade_init(pkg_name: str, helper_modules: list[str], extra_imports: list[str]) -> str:
    """Build __init__.py that re-exports public *and* private names from submodules."""
    lines = [
        f'"""{pkg_name} — thin re-export facade."""\n',
        "from __future__ import annotations\n",
        "\nimport importlib\n",
        "\n_SUBMODULES = (\n",
    ]
    for mod in ["_constants", "_state", *[f"_{g}" for g in helper_modules]]:
        lines.append(f'    "{mod}",\n')
    lines.append(")\n\n")
    for stmt in extra_imports:
        lines.append(stmt + "\n")
    lines.append("\n_FACADE_SKIP = frozenset({\"_SUBMODULES\", \"_export_submodule\", \"importlib\", \"_FACADE_SKIP\"})\n\n")
    lines.append("def _export_submodule(name: str) -> None:\n")
    lines.append("    mod = importlib.import_module(f\".{name}\", __name__)\n")
    lines.append("    for key, value in mod.__dict__.items():\n")
    lines.append("        if key.startswith(\"__\") or key in _FACADE_SKIP:\n")
    lines.append("            continue\n")
    lines.append("        globals()[key] = value\n\n\n")
    lines.append("for _name in _SUBMODULES:\n")
    lines.append("    _export_submodule(_name)\n")
    return "".join(lines)


def thin_facade_init(pkg_name: str, submodules: list[str], extra_imports: list[str] | None = None) -> str:
    """Facade without _constants/_state — for further splits of existing helper modules."""
    lines = [
        f'"""{pkg_name} — thin re-export facade."""\n',
        "from __future__ import annotations\n",
        "\nimport importlib\n",
        "\n_SUBMODULES = (\n",
    ]
    for mod in submodules:
        lines.append(f'    "{mod}",\n')
    lines.append(")\n\n")
    for stmt in extra_imports or []:
        lines.append(stmt + "\n")
    lines.append("\n_FACADE_SKIP = frozenset({\"_SUBMODULES\", \"_export_submodule\", \"importlib\", \"_FACADE_SKIP\"})\n\n")
    lines.append("def _export_submodule(name: str) -> None:\n")
    lines.append("    mod = importlib.import_module(f\".{name}\", __name__)\n")
    lines.append("    for key, value in mod.__dict__.items():\n")
    lines.append("        if key.startswith(\"__\") or key in _FACADE_SKIP:\n")
    lines.append("            continue\n")
    lines.append("        globals()[key] = value\n\n\n")
    lines.append("for _name in _SUBMODULES:\n")
    lines.append("    _export_submodule(_name)\n")
    return "".join(lines)


def preamble_end(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if line.startswith("_logger = "):
            return i + 1
    for i, line in enumerate(lines):
        if re.match(r"^(def |async def |class )", line):
            return i
    return 0


def ast_top_level_chunks(
    source: str, lines: list[str], start_line: int = 1, end_line: int | None = None
) -> list[tuple[str, list[str]]]:
    """Split module into top-level definitions using AST line numbers."""
    mod = ast.parse(source)
    end_line = end_line or len(lines)
    chunks: list[tuple[str, list[str]]] = []
    for node in mod.body:
        if not hasattr(node, "lineno"):
            continue
        if node.lineno < start_line or node.lineno > end_line:
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        name = node.name
        end = getattr(node, "end_lineno", node.lineno)
        chunk = lines[node.lineno - 1 : end]
        chunks.append((name, chunk))
    return chunks


def classify(name: str, groups: list[tuple[str, str]]) -> str:
    for group, pat in groups:
        if re.search(pat, name, re.I):
            return group
    return "helpers"


def collect_module_level_assigns(
    source: str, lines: list[str], pre: int, end_line: int
) -> tuple[list[str], list[str]]:
    mod = ast.parse(source)
    constants: list[str] = []
    state: list[str] = []
    for node in mod.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        if not hasattr(node, "lineno") or node.lineno <= pre or node.lineno > end_line:
            continue
        end = getattr(node, "end_lineno", node.lineno)
        text = "".join(lines[node.lineno - 1 : end])
        if any(x in text for x in (": dict", ": set", "LRUCache", "weakref", "Task[", "Lock", "None = None")):
            state.append(text + ("\n" if not text.endswith("\n") else ""))
        else:
            constants.append(text + ("\n" if not text.endswith("\n") else ""))
    return constants, state


def split_helper_module(
    src: Path,
    groups: list[tuple[str, str]],
    constants_import: str,
    state_import: str,
    facade_name: str,
) -> dict:
    """Split a helper module file into a same-named package with thin __init__ facade."""
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)
    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])

    helper_groups: dict[str, list[str]] = {}
    for name, chunk in ast_top_level_chunks(source, lines, start_line=pre + 1):
        group = classify(name, groups)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    pkg_dir = src.parent / src.stem
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir()

    binding_import = (
        "def _import_parent_bindings() -> None:\n"
        "    from .. import _constants as _parent_constants\n"
        "    from .. import _state as _parent_state\n"
        "    g = globals()\n"
        "    for _mod in (_parent_constants, _parent_state):\n"
        "        for _key, _value in _mod.__dict__.items():\n"
        "            if not _key.startswith(\"__\"):\n"
        "                g[_key] = _value\n\n\n"
        "_import_parent_bindings()\n"
    )

    submodules = sorted(helper_groups)
    for group in submodules:
        write(
            pkg_dir / f"_{group}.py",
            preamble + binding_import + "\n" + "".join(helper_groups[group]),
        )

    init = thin_facade_init(facade_name, [f"_{g}" for g in submodules])
    write(pkg_dir / "__init__.py", init)

    src.unlink()

    return {
        "before": before,
        "after": len(read_lines(pkg_dir / "__init__.py")),
        "modules": len(list(pkg_dir.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg_dir.rglob("*.py"))},
    }


SESSIONS_ROUTE_MARKERS = [
    (13114, 13558, "session_create"),
    (13559, 13662, "session_get"),
    (13663, 13871, "session_list"),
    (13872, 14214, "session_updates_ws"),
    (14215, 14533, "session_patch"),
    (14534, 14730, "session_fork"),
    (14731, 14976, "session_switch_agent"),
    (14977, 15253, "session_permission_hook"),
    (15254, 15470, "session_policy_evaluate"),
    (15471, 15541, "session_codex_elicitation_hook"),
    (15542, 15601, "session_items"),
    (15602, 15680, "session_child_sessions"),
    (15681, 17031, "session_resources"),
    (17032, 18492, "session_events"),
    (18493, 18600, "session_await"),
    (18601, 18839, "session_stream"),
    (18840, 18878, "session_global_events"),
    (18879, 19550, "session_delete"),
    (19551, 19662, "session_mcp"),
]

SESSIONS_HELPER_GROUPS: list[tuple[str, str]] = [
    ("mcp", r"tool_intercept|_handle_mcp|_mcp_|_mint_acting"),
    ("elicitation", r"elicitation|_native_ask|_harness_|_structured_ask"),
    ("runner", r"runner|relay|heal|keepalive|_ensure_runner|_forward_event|_dispatch_session|_flush_relay|_relay_|_stop_session"),
    ("native", r"native|claude|codex|terminal|MirroredToolCall|NativeTerminal"),
    ("policy", r"policy|_evaluate_|_build_actor|_build_evaluation|_apply_pending_policy|_intercept_tool|PendingPolicyAsk"),
    ("publish", r"publish|_format_sse|_stream_live|_resilient_stream|_parse_last_event|SessionLiveness"),
    ("usage", r"usage|cost|_model_usage|_priced_cost|_accumulate_session|_record_daily|_utc_day"),
    ("managed_launch", r"managed|_launch_|_provision_|_bind_and_launch|cancel_managed|HostLaunchAttempt"),
    ("external_events", r"external|_persist_external|_parse_external"),
    ("create", r"create_session|bundled|worktree|_multipart|_resolve_subagent|_derive_terminal|_notify_runner_of_bundled|_register_policy"),
    ("list_updates", r"session_list|liveness|discovery|_announce_session|_build_session_list|_apply_liveness|_discovery_key"),
    ("access", r"permission|tenant|_enforce_tenant|_session_list_accessible|_owner_from_grants|_permission_level"),
    ("subagent", r"subagent|child_session|_wake_parent|configure_subagent|_descendant|_ancestor"),
    ("snapshot", r"snapshot|_build_session_response|_get_session_snapshot|_fetch_runner_skills|_load_runner_skills|_child_session"),
    ("resources", r"resource|_stored_file|_attachment|_proxy_get_session_resources|_validate_terminal_launch"),
    ("skills", r"skill|_parse_skill_slash|_dispatch_skill"),
]


def decompose_sessions() -> dict:
    src = ROOT / "omnigent/server/routes/sessions.py"
    if not src.exists():
        src.write_text((ROOT / ".git").exists() and __import__("subprocess").check_output(
            ["git", "show", "HEAD:omnigent/server/routes/sessions.py"], cwd=ROOT, text=True
        ) or "")
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)

    pkg = ROOT / "omnigent/server/routes/sessions"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])
    router_line = 13040
    post_router_line = 19665

    constants, state = collect_module_level_assigns(source, lines, pre, router_line - 1)

    helper_groups: dict[str, list[str]] = {}
    for name, chunk in ast_top_level_chunks(source, lines, start_line=pre + 1, end_line=router_line - 1):
        group = classify(name, SESSIONS_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    for name, chunk in ast_top_level_chunks(source, lines, start_line=post_router_line):
        group = classify(name, SESSIONS_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    write(pkg / "_constants.py", preamble + "".join(constants) + "\n")
    write(pkg / "_state.py", preamble + "from ._constants import *\n\n" + "".join(state) + "\n")

    helper_modules = sorted(helper_groups)
    for group in helper_modules:
        write(
            pkg / f"_{group}.py",
            preamble + "from ._constants import *\nfrom ._state import *\n\n" + "".join(helper_groups[group]),
        )

    routes = pkg / "routes"
    routes.mkdir()
    write(routes / "__init__.py", "")

    sig_end = router_line - 1
    while sig_end < len(lines) and ") -> APIRouter:" not in lines[sig_end]:
        sig_end += 1
    sig_end += 1

    register_names = []
    for start, end, name in SESSIONS_ROUTE_MARKERS:
        register_names.append(name)
        chunk = lines[start - 1 : end]
        imports = (
            preamble
            + "from .._constants import *\nfrom .._state import *\n"
            + "\n".join(f"from .._{g} import *  # noqa: F403" for g in helper_modules)
            + "\n\n"
        )
        params = (
            "router,\n"
            "*,\n"
            "conversation_store,\n"
            "agent_store,\n"
            "file_store,\n"
            "artifact_store,\n"
            "runner_router,\n"
            "auth_provider,\n"
            "permission_store,\n"
            "agent_cache,\n"
            "liveness_lookup,\n"
            "comment_store,\n"
            "runner_tunnel_tokens,\n"
            "runner_exit_reports,\n"
        )
        body = imports + f"def register_{name}(\n{textwrap.indent(params, '    ')}\n):\n" + textwrap.indent("".join(chunk), "    ")
        write(routes / f"{name}.py", body)

    route_imports = "\n".join(f"from .routes.{n} import register_{n}" for n in register_names)
    helper_star = "\n".join(f"from ._{g} import *  # noqa: F403" for g in helper_modules)

    router_body = (
        preamble
        + "from fastapi import APIRouter\n"
        + "from ._constants import *\nfrom ._state import *\n"
        + helper_star
        + "\n"
        + route_imports
        + "\n\n"
        + "".join(lines[router_line - 1 : sig_end])
        + '    """Factory that builds the sessions router."""\n'
        + "    router = APIRouter()\n\n"
    )
    register_block = "\n".join(
        f"    register_{n}(\n"
        f"        router,\n"
        f"        conversation_store=conversation_store,\n"
        f"        agent_store=agent_store,\n"
        f"        file_store=file_store,\n"
        f"        artifact_store=artifact_store,\n"
        f"        runner_router=runner_router,\n"
        f"        auth_provider=auth_provider,\n"
        f"        permission_store=permission_store,\n"
        f"        agent_cache=agent_cache,\n"
        f"        liveness_lookup=liveness_lookup,\n"
        f"        comment_store=comment_store,\n"
        f"        runner_tunnel_tokens=runner_tunnel_tokens,\n"
        f"        runner_exit_reports=runner_exit_reports,\n"
        f"    )"
        for n in register_names
    )
    router_body += register_block + "\n\n    return router\n"
    write(pkg / "router.py", router_body)

    init = facade_init(
        "Routes for the Sessions API (``/v1/sessions``)",
        helper_modules,
        ["from .router import create_sessions_router"],
    )
    write(pkg / "__init__.py", init)

    if src.exists():
        src.unlink()

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


RUNNER_HELPER_GROUPS: list[tuple[str, str]] = [
    ("terminals", r"terminal|tmux|claude|codex|pi_native|grok|repl|CodexNativeLaunch|PiNativeLaunch"),
    ("subagents", r"subagent|child_session|wake|SubagentWork|SubagentDelivery|ChildParentMeta"),
    ("timers", r"timer"),
    ("harness", r"ResolvedSpec|harness|spawn_env|agent_start|unwrap_resolved|resolved_spec_workdir|build_spawn|HARNESS_MODEL"),
    ("dispatch", r"TurnDispatch|overflow|advisor|message_event|forwarded_message|SessionSnapshot|spec_with_workdir"),
    ("tools", r"mcp|schema|client_tools|tool_locally|inject_mcp"),
    ("policy", r"policy|evaluate_policy"),
    ("streaming", r"sse|encode_sse|response_failed|forward_harness"),
    ("forwarders", r"auto_forwarder"),
]


def decompose_runner() -> dict:
    src = ROOT / "omnigent/runner/app.py"
    if not src.exists():
        src.write_text(__import__("subprocess").check_output(
            ["git", "show", "HEAD:omnigent/runner/app.py"], cwd=ROOT, text=True
        ))
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)

    pkg = ROOT / "omnigent/runner/app"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])
    factory_start = 4233
    factory_end = 12693

    constants, state = collect_module_level_assigns(source, lines, pre, factory_start - 1)

    helper_groups: dict[str, list[str]] = {}
    for name, chunk in ast_top_level_chunks(source, lines, start_line=pre + 1, end_line=factory_start - 1):
        group = classify(name, RUNNER_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    for name, chunk in ast_top_level_chunks(source, lines, start_line=factory_end):
        group = classify(name, RUNNER_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    helper_modules = sorted(helper_groups)
    write(pkg / "_constants.py", preamble + "".join(constants) + "\n")
    write(pkg / "_state.py", preamble + "from ._constants import *\n\n" + "".join(state) + "\n")
    for group in helper_modules:
        write(
            pkg / f"_{group}.py",
            preamble + "from ._constants import *\nfrom ._state import *\n\n" + "".join(helper_groups[group]),
        )

    factory_imports = (
        preamble
        + "from fastapi import FastAPI\n"
        + "from ._constants import *\nfrom ._state import *\n"
        + "\n".join(f"from ._{g} import *  # noqa: F403" for g in helper_modules)
        + "\n\n"
    )
    write(pkg / "factory.py", factory_imports + "".join(lines[factory_start - 1 : factory_end]))

    init = facade_init(
        "Runner FastAPI app",
        helper_modules,
        ["from .factory import create_runner_app, create_runner_app_from_env"],
    )
    write(pkg / "__init__.py", init)

    if src.exists():
        src.unlink()

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


# ---------------------------------------------------------------------------
# Phase 2 decompositions (cli, factory routes, tool_dispatch, runner helpers)
# ---------------------------------------------------------------------------

CLI_HELPER_GROUPS: list[tuple[str, str]] = [
    ("config", r"config|_GLOBAL_CONFIG|_CONFIG_|_load_.*config|_save_.*config|_effective_global|_parse_config|_ConfigGroup|config_grp"),
    ("daemon", r"daemon|_HostDaemon|_ensure_host|_spawn_host|_terminate_host|_host_daemon|_normalize_daemon|_reuse_existing_daemon|_persist_spawned|_foreground_daemon|_live_daemon|_claim_foreground|_restore_replaced|_load_or_create_host|_read_host_pid|_build_host_daemon|_delete_daemon|_write_daemon|_read_daemon|_list_daemon|_find_daemon|_record_from_json|_legacy_daemon|_update_daemon|_load_existing_host|_daemon_tunnel|_daemon_host_identity|_local_daemon|_DaemonReuse|_SpawnedDaemon"),
    ("runner_proc", r"_CliRunner|_start_cli_runner|_stop_cli_runner|_adopt_cli_runner|_runner_loopback"),
    ("server", r"server|_ensure_backend|_default_db|_create_artifact|_preregister|_server_uvicorn|_maybe_prompt_first_admin|_assert_server_port|_stop_local_server|_discover_local|_exit_for_auth|_ensure_databricks|_databricks_workspace"),
    ("deploy", r"bundle|deploy|_Deploy|_expand_config|_LLMDeploy|_BuiltinEntry|_ToolsDeploy|_ExecutorDeploy|_materialize_bundled|_materialize_internal|_resolve_bundle"),
    ("run", r"_dispatch_run|_ResumeChoice|_validate_harness|_require_live|_resolve_attach|_split_resume|_build_resume|_default_harness|_materialize_harness|_missing_run_agent|_run_bundled"),
    ("host_ui", r"_host_|_HostGroup|_daemon_status|_sessions_for_daemon|_HostPayload|_HostSession|_HostHttp|_HostJson|_runner_online_map|_annotate_sessions|_stop_session_on_server|_stop_daemon_sessions|_echo_daemon|_add_host|_add_daemon|_base_daemon|_selected_daemon|_daemon_base|_resolve_host|_host_group|_prompt_stop_local|_count_running_sessions|_wait_for_local"),
    ("pane", r"pane|_strip_resume|_strip_one_shot|_RESUME_|_ONE_SHOT"),
    ("version", r"_format_version|_print_version|_should_skip_update"),
    ("first_run", r"first_run|_FirstRunPlan|_pick_first_run|_resolve_first_run|_peek_default|_resolve_default_agent|_bundled_example"),
    ("auth", r"login|_databricks|store_token|store_databricks"),
]

CLI_COMMAND_MARKERS: list[tuple[int, str]] = [
    (2687, "server"),
    (3267, "stop"),
    (3378, "upgrade"),
    (3823, "claude"),
    (3974, "codex"),
    (4092, "pi"),
    (4201, "polly"),
    (4226, "debby"),
    (4251, "resume"),
    (4867, "attach"),
    (4949, "run"),
    (5254, "host"),
    (6299, "version"),
    (6463, "config"),
    (8638, "setup"),
    (8748, "debug"),
    (9323, "login"),
    (9542, "pane_split"),
    (9623, "pane_picker"),
]


def decompose_cli() -> dict:
    src = ROOT / "omnigent/cli.py"
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)

    pkg = ROOT / "omnigent/cli"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])
    cli_group_line = 1111
    commands_start = CLI_COMMAND_MARKERS[0][0]

    constants, state = collect_module_level_assigns(source, lines, pre, cli_group_line - 1)

    helper_groups: dict[str, list[str]] = {}
    for name, chunk in ast_top_level_chunks(source, lines, start_line=pre + 1, end_line=cli_group_line - 1):
        group = classify(name, CLI_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    # Helpers between cli() definition and first command decorator
    for name, chunk in ast_top_level_chunks(source, lines, start_line=1155, end_line=commands_start - 1):
        group = classify(name, CLI_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    # Tail helpers after last command
    last_cmd_start = CLI_COMMAND_MARKERS[-1][0]
    for name, chunk in ast_top_level_chunks(source, lines, start_line=9702):
        group = classify(name, CLI_HELPER_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    helper_modules = sorted(helper_groups)
    write(pkg / "_constants.py", preamble + "".join(constants) + "\n")
    write(pkg / "_state.py", preamble + "from ._constants import *\n\n" + "".join(state) + "\n")
    for group in helper_modules:
        write(
            pkg / f"_{group}.py",
            preamble
            + "from ._constants import *\nfrom ._state import *\n\n"
            + "".join(helper_groups[group]),
        )

    # Core: cli group, main, and helpers embedded in command sections land in commands
    core_imports = (
        preamble
        + "from ._constants import *\nfrom ._state import *\n"
        + "\n".join(f"from ._{g} import *  # noqa: F403" for g in helper_modules)
        + "\n\n"
    )
    core_imports_minimal = (
        '"""CLI core — click group and console entry point."""\n'
        "from __future__ import annotations\n\n"
        "import os\nimport sys\n\n"
        "import click\n\n"
        "from ._helpers import _migrate_legacy_state_dir\n"
        "from ._version import _print_version_callback, _should_skip_update_check\n\n"
        "def _import_package_bindings() -> None:\n"
        "    from . import _constants as _pkg_constants\n"
        "    from . import _state as _pkg_state\n"
        "    g = globals()\n"
        "    for _mod in (_pkg_constants, _pkg_state):\n"
        "        for _key, _value in _mod.__dict__.items():\n"
        "            if not _key.startswith(\"__\"):\n"
        "                g[_key] = _value\n\n\n"
        "_import_package_bindings()\n\n"
    )
    core_body = (
        "".join(lines[cli_group_line - 1 : 1153])
        + "\n\n"
        + "".join(lines[1179 : 1302])
        + "\n\n"
        + "".join(lines[1303 : 1388])
    )
    write(pkg / "_core.py", core_imports_minimal + core_body)

    commands_dir = pkg / "commands"
    commands_dir.mkdir()
    cmd_imports = (
        "from __future__ import annotations\n\n"
        + "from .._constants import *\nfrom .._state import *\n"
        + "from .._core import cli\n"
        + "\n".join(f"from .._{g} import *  # noqa: F403" for g in helper_modules)
        + "\n\n"
    )
    command_names: list[str] = []
    for idx, (start, name) in enumerate(CLI_COMMAND_MARKERS):
        end = CLI_COMMAND_MARKERS[idx + 1][0] - 1 if idx + 1 < len(CLI_COMMAND_MARKERS) else 9700
        command_names.append(name)
        write(commands_dir / f"{name}.py", cmd_imports + "".join(lines[start - 1 : end]))

    # Sandbox group registration lives in setup command tail — also import commands
    cmd_init = "\n".join(f"from . import {n}  # noqa: F401" for n in command_names) + "\n"
    write(commands_dir / "__init__.py", cmd_init)

    init_imports = [
        "from ._core import cli, main",
        "from . import commands  # noqa: F401 — register click commands",
    ]
    init = facade_init("CLI entry point for omnigent", helper_modules, init_imports)
    write(pkg / "__init__.py", init)

    src.unlink()

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


def _factory_route_markers(lines: list[str]) -> list[tuple[int, str]]:
    markers: list[tuple[int, str]] = []
    for i, line in enumerate(lines, 1):
        m = re.match(r"\s+@app\.(get|post|put|patch|delete|websocket)\(", line)
        if not m:
            continue
        name = ""
        for j in range(i, min(i + 6, len(lines) + 1)):
            dm = re.match(r"\s+async def (\w+)", lines[j - 1])
            if dm:
                name = dm.group(1)
                break
        markers.append((i, name or f"route_{len(markers)}"))
    return markers


def decompose_factory_routes() -> dict:
    src = ROOT / "omnigent/runner/app/factory.py"
    lines = read_lines(src)
    before = len(lines)

    # Locate create_runner_app body
    func_start = next(i for i, l in enumerate(lines) if l.startswith("def create_runner_app("))
    body_start = None
    for i in range(func_start, len(lines)):
        if lines[i].strip().endswith('"""') and i > func_start and '"""' in lines[i]:
            body_start = i + 1
            break
    if body_start is None:
        for i in range(func_start, len(lines)):
            if lines[i].startswith("    import hmac"):
                body_start = i
                break
    assert body_start is not None

    return_line = next(i for i, l in enumerate(lines) if l.strip() == "return app")
    markers = _factory_route_markers(lines)
    first_route = markers[0][0]

    header = "".join(lines[: func_start + 1])
    # Reconstruct signature through docstring
    sig_end = body_start
    signature_block = "".join(lines[func_start + 1 : sig_end])

    setup_block = "".join(lines[sig_end : first_route - 1])
    tail_block = "".join(lines[markers[-1][0] - 1 : return_line])
    # tail starts at last route — we need from end of last route handler
    last_route_start = markers[-1][0]
    # find end of last route handler (next route would be EOF before tail helpers)
    # tail is _catch_up_scan which starts before return app
    tail_start = None
    for i in range(last_route_start, return_line):
        if "async def _catch_up_scan" in lines[i]:
            tail_start = i
            break
    if tail_start is None:
        tail_start = return_line - 1
        while tail_start > last_route_start and lines[tail_start].strip() != "":
            tail_start -= 1

    routes_dir = src.parent / "routes"
    if routes_dir.exists():
        shutil.rmtree(routes_dir)
    routes_dir.mkdir()

    route_names: list[str] = []
    for idx, (start, name) in enumerate(markers):
        end = markers[idx + 1][0] - 1 if idx + 1 < len(markers) else tail_start - 1
        route_names.append(name)
        write(routes_dir / f"{name}.py", "".join(lines[start - 1 : end]))

    write(routes_dir / "__init__.py", "")

    loader = (
        "\n\n"
        + "def _exec_runner_route_chunks(ns: dict) -> None:\n"
        + '    """Execute route registration chunks in a shared closure namespace."""\n'
        + "    import importlib.resources as _res\n"
        + "    from pathlib import Path as _Path\n"
        + "    _routes_pkg = _Path(__file__).resolve().parent / \"routes\"\n"
        + "    for _fname in (\n"
        + "".join(f'        "{n}.py",\n' for n in route_names)
        + "    ):\n"
        + "        _code = (_routes_pkg / _fname).read_text()\n"
        + '        exec(compile(_code, str(_routes_pkg / _fname), "exec"), ns)\n'
    )

    new_factory = (
        header
        + signature_block
        + setup_block
        + loader
        + "    _exec_runner_route_chunks(locals().copy())\n\n"
        + "".join(lines[tail_start:return_line])
        + "    return app\n"
    )
    write(src, new_factory)

    return {
        "before": before,
        "after": len(read_lines(src)),
        "modules": len(list(routes_dir.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted([src, *routes_dir.rglob("*.py")])},
    }


TOOL_DISPATCH_GROUPS: list[tuple[str, str]] = [
    ("types", r"^AgentSpecLike|^ActionRequired|^SubagentSend|^SubagentInbox|^SessionSnapshot|^TypedDict|^Protocol"),
    ("predicates", r"^is_action_required|^get_tool|^should_dispatch|^should_relay|_is_spec_|_is_uc_function"),
    ("builtin_exec", r"_execute_spec_builtin|_execute_local_python|_resolve_spec_callable|_execute_spec_callable|_execute_uc_function|_resolve_uc_profile"),
    ("subagent", r"subagent|_SubagentLabel|_child|_publish_child|_list_child|_find_existing_child|_subagent_|_post_child|_send_to_existing|_build_session_create|_finalize_created|_execute_session_create|_bundle_local|_upload_config|_session_create_from"),
    ("session_api", r"session_|_session_|_fetch_close|_close_tree|_PeekMeta|_fetch_peek|_ParsedTitle|_parse_session|_truncate_activity|_text_from_api|_project_api|_execute_session_query|_runner_online"),
    ("agent_api", r"agent_|_agent_|_execute_agent|_execute_list_models|_execute_web_fetch|_scan_local_agent|_skill_scope|_skill_body|_execute_skill"),
    ("timer", r"timer|_execute_timer"),
    ("comment_policy", r"comment|policy|_execute_comment|_execute_policy|_execute_list_policies|_execute_add_policy"),
    ("dispatch", r"^execute_tool|^dispatch_tool_locally|_build_tool_execution|_maybe_signal"),
    ("os_env", r"os_env|_clone_os_env|_runner_default_os|_effective_runner_os|_seed_os_env|_execute_os_env"),
    ("rest_file", r"_execute_rest_tool|_execute_file_tool"),
    ("terminal", r"terminal|_execute_terminal|_emit_terminal|_publish_terminal|_format_terminal"),
    ("async_inbox", r"async_inbox|_execute_async_inbox|_spawn_async|_cancel_async|_execute_task_lifecycle|_cancel_subagent|_drain_inbox|_evaluate_subagent|_subagent_tool_result|_post_subagent_policy|_apply_subagent|_cleanup_drained|_format_async|_truncate_inbox"),
    ("skills", r"_inject_orchestrator|_execute_skill_tool"),
]


def decompose_tool_dispatch() -> dict:
    src = ROOT / "omnigent/runner/tool_dispatch.py"
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)

    pkg = ROOT / "omnigent/runner/tool_dispatch"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])

    helper_groups: dict[str, list[str]] = {}
    for name, chunk in ast_top_level_chunks(source, lines, start_line=pre + 1):
        group = classify(name, TOOL_DISPATCH_GROUPS)
        helper_groups.setdefault(group, []).extend(chunk)
        helper_groups[group].append("\n")

    constants, state = collect_module_level_assigns(source, lines, pre, len(lines))

    helper_modules = sorted(helper_groups)
    write(pkg / "_constants.py", preamble + "".join(constants) + "\n")
    write(pkg / "_state.py", preamble + "from ._constants import *\n\n" + "".join(state) + "\n")
    for group in helper_modules:
        write(
            pkg / f"_{group}.py",
            preamble + "from ._constants import *\nfrom ._state import *\n\n" + "".join(helper_groups[group]),
        )

    init = facade_init("Runner-local tool dispatch", helper_modules, [])
    write(pkg / "__init__.py", init)
    src.unlink()

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


SESSIONS_RUNNER_GROUPS: list[tuple[str, str]] = [
    ("keepalive", r"keepalive|Keepalive|_RelayHandle"),
    ("client", r"_get_runner_client|_wait_for_runner|_runner_client_ready|set_server_runner_router|_registered_runner_id"),
    ("launch", r"launch|bind_and_launch|_launch_runner|_ensure_runner_session"),
    ("heal", r"heal|unavailable|recover|_publish_runner_recovered"),
    ("relay", r"relay|_forward_event|_dispatch_session|_flush_relay|_relay_|_ensure_runner_relay|_instruction_fragments"),
    ("resources", r"resource|proxy_get|_reset_runner_resources|_get_runner_client_for_resource"),
    ("native", r"native_terminal|_extract_claude|_forward_session_change"),
    ("skills", r"skill|runner_skills|_fetch_runner|_load_runner|_resolve_skill|_dispatch_skill"),
    ("stop", r"stop_session|_stop_session"),
    ("bundled", r"bundled|_notify_runner|_authorize_bundled|_forward_approval"),
]


def decompose_sessions_runner() -> dict:
    src = ROOT / "omnigent/server/routes/sessions/_runner.py"
    return split_helper_module(
        src,
        SESSIONS_RUNNER_GROUPS,
        "from .._constants import *",
        "from .._state import *",
        "Sessions runner relay/heal/launch helpers",
    )


TERMINALS_GROUPS: list[tuple[str, str]] = [
    ("codex", r"codex|CodexNative|_codex"),
    ("claude", r"claude|Claude|_claude"),
    ("pi", r"pi_native|PiNative|_pi_native|_auto_create_pi"),
    ("grok", r"grok|_auto_create_grok"),
    ("repl", r"repl|REPL|_auto_create_repl"),
    ("shared", r"publish_terminal|lookup_miss|terminal_start_error|tmux_target|mark_subagent|_publish_tmux|_terminal_lookup|_log_terminal|_native_terminal|_build_claude_native_base|_is_runner_owned"),
]


def decompose_runner_terminals() -> dict:
    src = ROOT / "omnigent/runner/app/_terminals.py"
    return split_helper_module(
        src,
        TERMINALS_GROUPS,
        "from .._constants import *",
        "from .._state import *",
        "Runner terminal auto-create helpers",
    )


def run_phase2() -> dict[str, dict]:
    results = {}
    results["cli"] = decompose_cli()
    results["factory_routes"] = decompose_factory_routes()
    results["tool_dispatch"] = decompose_tool_dispatch()
    results["sessions_runner"] = decompose_sessions_runner()
    results["runner_terminals"] = decompose_runner_terminals()
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "phase2":
        for name, stats in run_phase2().items():
            print(f"{name}:", stats)
    else:
        print("sessions:", decompose_sessions())
        print("runner:", decompose_runner())