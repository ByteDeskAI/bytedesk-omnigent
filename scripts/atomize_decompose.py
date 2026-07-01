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


def fix_preamble_relative_imports(preamble: str, pkg_dir: Path) -> str:
    """Rewrite ``from ._sibling`` imports that now live in the parent package."""
    parent_pkg = pkg_dir.parent

    def replacer(match: re.Match[str]) -> str:
        mod = match.group(1)
        if (pkg_dir / f"{mod}.py").exists() or (pkg_dir / mod).is_dir():
            return match.group(0)
        if (parent_pkg / f"{mod}.py").exists() or (parent_pkg / mod).is_dir():
            return f"from ..{mod}"
        return match.group(0)

    return re.sub(r"from \.([\w]+)", replacer, preamble)


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
        start = node.lineno
        if node.decorator_list:
            start = node.decorator_list[0].lineno
        end = getattr(node, "end_lineno", node.lineno)
        chunk = lines[start - 1 : end]
        chunks.append((name, chunk))
    return chunks


def fix_orphaned_dataclass_decorators(path: Path) -> int:
    """Drop ``@dataclass`` decorators stranded without a following ``class`` in the same file."""
    lines = read_lines(path)
    new_lines: list[str] = []
    fixed = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("@dataclass"):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j >= len(lines) or not lines[j].lstrip().startswith("class "):
                fixed += 1
                i += 1
                continue
        new_lines.append(lines[i])
        i += 1
    if fixed:
        write(path, "".join(new_lines))
    return fixed


PKG_BINDING_IMPORT = (
    "def _import_package_bindings() -> None:\n"
    "    from . import _constants as _pkg_constants\n"
    "    from . import _state as _pkg_state\n"
    "    g = globals()\n"
    "    for _mod in (_pkg_constants, _pkg_state):\n"
    "        for _key, _value in _mod.__dict__.items():\n"
    "            if not _key.startswith(\"__\"):\n"
    "                g[_key] = _value\n\n\n"
    "_import_package_bindings()\n"
)

HELPER_BINDING_IMPORT = (
    "def _import_helper_bindings() -> None:\n"
    "    from . import _helpers as _pkg_helpers\n"
    "    g = globals()\n"
    "    for _key, _value in _pkg_helpers.__dict__.items():\n"
    "        if not _key.startswith(\"__\"):\n"
    "            g[_key] = _value\n\n\n"
    "_import_helper_bindings()\n"
)

API_BINDING_IMPORT = (
    "def _import_api_bindings() -> None:\n"
    "    from . import _api as _pkg_api\n"
    "    g = globals()\n"
    "    for _key, _value in _pkg_api.__dict__.items():\n"
    "        if not _key.startswith(\"__\"):\n"
    "            g[_key] = _value\n\n\n"
    "_import_api_bindings()\n"
)


def sibling_wire_block(submodules: list[str], self_mod: str) -> str:
    """Late-bind names from sibling helper submodules (avoids ``import *`` underscore drop)."""
    others = [m for m in submodules if m != self_mod and m != "__init__" and m != "_bootstrap"]
    if not others:
        return ""
    imports = "".join(
        f"    from . import {m} as _sib_{m.lstrip('_')}\n" for m in others
    )
    copies = ""
    for m in others:
        alias = f"_sib_{m.lstrip('_')}"
        copies += (
            f"    for _key, _value in {alias}.__dict__.items():\n"
            f"        if not _key.startswith(\"__\"):\n"
            f"            g.setdefault(_key, _value)\n"
        )
    return (
        "\ndef _wire_sibling_modules() -> None:\n"
        "    g = globals()\n"
        f"{imports}"
        f"{copies}\n"
        "_wire_sibling_modules()\n"
    )


def append_sibling_wiring(pkg_dir: Path, submodules: list[str]) -> None:
    submodules = [m for m in submodules if m != "_bootstrap"]
    for mod in submodules:
        path = pkg_dir / f"{mod}.py"
        text = read_lines(path)
        if isinstance(text, list):
            content = "".join(text)
        else:
            content = text
        if "_wire_sibling_modules" in content:
            continue
        write(path, content + sibling_wire_block(submodules, mod))


def fix_dataclass_decorators_in_tree(pkg_dir: Path) -> int:
    total = 0
    for py in sorted(pkg_dir.rglob("*.py")):
        total += fix_orphaned_dataclass_decorators(py)
    return total


def classify(name: str, groups: list[tuple[str, str]]) -> str:
    for group, pat in groups:
        if re.search(pat, name, re.I):
            return group
    return "helpers"


def _assign_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    if isinstance(node, ast.AnnAssign):
        return [node.target.id] if isinstance(node.target, ast.Name) else []
    names: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _assign_value_name_refs(node: ast.Assign | ast.AnnAssign) -> set[str]:
    refs: set[str] = set()
    value = node.value
    if value is None:
        return refs
    for child in ast.walk(value):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            refs.add(child.id)
    return refs


def collect_module_level_assigns(
    source: str, lines: list[str], pre: int, end_line: int
) -> tuple[list[str], list[str]]:
    mod = ast.parse(source)
    top_level_names = {
        node.name
        for node in mod.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and hasattr(node, "lineno")
        and pre < node.lineno <= end_line
    }
    assign_nodes: list[ast.Assign | ast.AnnAssign] = []
    for node in mod.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        if not hasattr(node, "lineno") or node.lineno <= pre or node.lineno > end_line:
            continue
        assign_nodes.append(node)

    constants: list[str] = []
    state: list[str] = []
    bootstrap: list[str] = []
    defined: set[str] = set()
    for node in assign_nodes:
        defined.update(_assign_target_names(node))

    for node in assign_nodes:
        end = getattr(node, "end_lineno", node.lineno)
        text = "".join(lines[node.lineno - 1 : end])
        if not text.endswith("\n"):
            text += "\n"
        targets = _assign_target_names(node)
        refs = _assign_value_name_refs(node)
        module_refs = refs - {"True", "False", "None"}
        is_subscript_target = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Subscript) for target in node.targets
        )
        needs_bootstrap = (
            is_subscript_target
            or bool(module_refs & defined)
            or bool(module_refs & top_level_names)
        )
        if needs_bootstrap:
            bootstrap.append(text)
            defined.update(targets)
            continue
        if any(
            x in text
            for x in (": dict", ": set", "LRUCache", "weakref", "Task[", "Lock", "None = None")
        ):
            state.append(text)
        else:
            constants.append(text)
        defined.update(targets)

    return constants, state, bootstrap


def _helper_module_name(group: str) -> str:
    """Avoid helper groups clobbering reserved ``_constants`` / ``_state`` slots."""
    if group in {"constants", "state"}:
        return f"{group}_helpers"
    return group


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

    constants, state, _bootstrap = collect_module_level_assigns(source, lines, pre, router_line - 1)

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

    constants, state, _bootstrap = collect_module_level_assigns(source, lines, pre, factory_start - 1)

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

    constants, state, _bootstrap = collect_module_level_assigns(source, lines, pre, cli_group_line - 1)

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

    constants, state, _bootstrap = collect_module_level_assigns(source, lines, pre, len(lines))

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


# ---------------------------------------------------------------------------
# Phase 3 decompositions (schemas, chat, parser, delete_session, sqlalchemy_store)
# ---------------------------------------------------------------------------


def decompose_ast_monolith(
    src: Path,
    facade_doc: str,
    helper_groups: list[tuple[str, str]],
    extra_init_imports: list[str] | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """Generic AST split: _constants, _state, grouped _*.py helpers, thin __init__ facade."""
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)
    pre = preamble_end(lines)
    pkg_dir = src.parent / src.stem
    preamble = fix_preamble_relative_imports("".join(lines[:pre]), pkg_dir)
    body_start = start_line or (pre + 1)
    body_end = end_line or len(lines)

    constants, state, bootstrap = collect_module_level_assigns(source, lines, pre, body_end)

    helper_groups_map: dict[str, list[str]] = {}
    for name, chunk in ast_top_level_chunks(source, lines, start_line=body_start, end_line=body_end):
        group = classify(name, helper_groups)
        helper_groups_map.setdefault(group, []).extend(chunk)
        helper_groups_map[group].append("\n")

    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir()

    write(pkg_dir / "_constants.py", preamble + "".join(constants) + "\n")
    write(pkg_dir / "_state.py", preamble + "from ._constants import *\n\n" + "".join(state) + "\n")

    helper_modules = sorted(helper_groups_map)
    helper_module_names = [_helper_module_name(g) for g in helper_modules]
    for group, mod_name in zip(helper_modules, helper_module_names, strict=True):
        write(
            pkg_dir / f"_{mod_name}.py",
            preamble + PKG_BINDING_IMPORT + "\n" + "".join(helper_groups_map[group]),
        )

    if bootstrap:
        name_to_module: dict[str, str] = {}
        for group, chunks in helper_groups_map.items():
            mod_name = _helper_module_name(group)
            for text in chunks:
                for match in re.finditer(r"^(?:async )?def (\w+)|^class (\w+)", text, re.M):
                    name = match.group(1) or match.group(2)
                    if name:
                        name_to_module[name] = mod_name
        bootstrap_text = "".join(bootstrap)
        bootstrap_refs = set(re.findall(r"\b([A-Za-z_]\w*)\b", bootstrap_text))
        helper_imports = sorted(
            {
                f"from . import _{mod} as _boot_{mod}"
                for name, mod in name_to_module.items()
                if name in bootstrap_refs
            }
        )
        promote = []
        for name, mod in sorted(name_to_module.items()):
            if name not in bootstrap_refs:
                continue
            promote.append(
                f"    for _key, _value in _boot_{mod}.__dict__.items():\n"
                f"        if _key == {name!r}:\n"
                f"            g[_key] = _value\n"
            )
        bootstrap_body = (
            PKG_BINDING_IMPORT
            + "\n"
            + "\n".join(helper_imports)
            + "\n\n"
            + "def _promote_bootstrap_bindings() -> None:\n"
            + "    g = globals()\n"
            + "".join(promote)
            + "\n\n_promote_bootstrap_bindings()\n\n"
            + bootstrap_text
        )
        if not bootstrap_body.endswith("\n"):
            bootstrap_body += "\n"
        write(pkg_dir / "_bootstrap.py", preamble + bootstrap_body)

    init = facade_init(facade_doc, helper_module_names, extra_init_imports or [])
    if bootstrap:
        init += (
            "from . import _bootstrap\n\n"
            "for _key, _value in _bootstrap.__dict__.items():\n"
            "    if _key.startswith(\"__\") or _key in _FACADE_SKIP:\n"
            "        continue\n"
            "    globals()[_key] = _value\n"
        )
    write(pkg_dir / "__init__.py", init)

    submodule_names = [f"_{m}" for m in helper_module_names]
    append_sibling_wiring(pkg_dir, submodule_names)

    for py in sorted(pkg_dir.rglob("*.py")):
        text = py.read_text()
        fixed = fix_preamble_relative_imports(text, pkg_dir)
        if fixed != text:
            write(py, fixed)

    if src.exists():
        src.unlink()

    fix_dataclass_decorators_in_tree(pkg_dir)

    return {
        "before": before,
        "after": len(read_lines(pkg_dir / "__init__.py")),
        "modules": len(list(pkg_dir.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg_dir.rglob("*.py"))},
    }


def decompose_schemas() -> dict:
    """Split REST API schemas and SSE event models into submodules."""
    src = ROOT / "omnigent/server/schemas.py"
    lines = read_lines(src)
    before = len(lines)
    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])
    sse_start = next(i for i, line in enumerate(lines) if "STREAM EVENTS" in line and line.startswith("#"))

    pkg = ROOT / "omnigent/server/schemas"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    write(pkg / "_api.py", preamble + "".join(lines[pre:sse_start]))
    write(
        pkg / "_sse.py",
        preamble + API_BINDING_IMPORT + "\n" + "".join(lines[sse_start:]),
    )
    init = thin_facade_init(
        "Pydantic models for the API layer and SSE stream events",
        ["_api", "_sse"],
    )
    write(pkg / "__init__.py", init)
    src.unlink()
    fix_dataclass_decorators_in_tree(pkg)

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


CHAT_HELPER_GROUPS: list[tuple[str, str]] = [
    ("entry", r"^run_chat|^run_prompt|^run_attach|^_default_cli_model"),
    ("types", r"^ChatOverrides|^LocalServer|^_SessionToolAdapter|^_AttachSessionInfo|^_DaemonChatSession|^_DatabricksTokenAuth"),
    ("remote", r"^_is_url|^_remote_headers|^_stored_databricks|^_server_headers|^_server_auth|^_chat_with_server"),
    ("native", r"^_is_claude_native|^_redirect_native|^_finish_native|^_run_.*native|^_wrapper_label"),
    ("daemon", r"^_await_accounts|^_prepare_chat_session|^_chat_via_daemon|^_wait_for_remote|^_poll_remote"),
    ("local", r"^_bundle_agent|^_chat_local|^_run_local_headless|^_run_headless"),
    ("sessions", r"^_query_sessions|^_sessions_tool|^_response_output|^_persisted_turn|^_resolve_resume|^_assert_resume|^_run_picker|^_resolve_latest|^_attach_session|^_pick_agent"),
    ("overrides", r"^_materialize_override|^_cleanup_materialized|^_load_yaml|^_spec_declares|^_should_materialize|^_effective_openai|^_inject_openai|^_apply_overrides|^_apply_harness|^_validate_agent|^_extract_agent|^_merge_host|^_fallback_label|^_canonicalize"),
    ("server_proc", r"^_find_free_port|^_omnigent_log|^_omnigent_persistent|^_start_local_server|^_wait_for_server|^_raise_server_failed|^_stop_server|^_stop_local"),
    ("repl", r"^_spec_used_families|^_run_repl|^_run_one_shot|^_load_tool_handler"),
]


def decompose_chat() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/chat.py",
        "Implementation of the ``omnigent chat`` command",
        CHAT_HELPER_GROUPS,
    )


PARSER_HELPER_GROUPS: list[tuple[str, str]] = [
    ("core", r"^parse$|^_ConfigYamlLoader|^expand_env|^check_unresolved"),
    ("llm", r"^_parse_llm|^_parse_interaction|^_parse_compaction|^parse_server_llm"),
    ("tools", r"^_parse_tools|^_parse_sandbox|^_parse_builtin|^_parse_retry|^_parse_executor|^_parse_blueprint|^_parse_supervisor"),
    ("os_env", r"^_parse_os_env|^_parse_terminals|^_parse_os_env_sandbox|^_parse_cwd|^_parse_env_passthrough|^_parse_egress"),
    ("credentials", r"^_Credential|^_parse_credential|^_normalize|^_resolve_credential|^_format_validation"),
    ("skills", r"^discover_host|^_discover_skills|^_parse_skill|^_parse_skills_filter|^_read_contained|^_resolve_instructions"),
    ("mcp", r"^_parse_inline_mcp|^_discover_mcp|^_parse_http_mcp|^_parse_stdio_mcp|^_reject_wrong|^_parse_tool_allowlist"),
    ("discover", r"^_discover_local|^_discover_sub"),
    ("guardrails", r"^_parse_guardrails|^_parse_label|^_coerce_label|^_validate_label"),
    ("policies", r"^_parse_policies|^_parse_policy|^parse_default_policies|^_parse_function|^_parse_on|^_parse_condition|^_parse_action|^_parse_writable|^_parse_phase"),
    ("capabilities", r"^_parse_capabilities"),
]


def decompose_parser() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/spec/parser.py",
        "Parse an agent image directory into an AgentSpec",
        PARSER_HELPER_GROUPS,
    )


DELETE_SESSION_GROUPS: list[tuple[str, str]] = [
    ("history", r"_load_history|_convert_raw|_extract_last|_serialize_messages"),
    ("compact", r"_proactive_compact"),
    ("cancellation", r"_append_cancellation|_persist_cancellation"),
    ("native", r"_handle_.*native|_is_native|_wake_parent|_teardown_session|_inject_codex|_native_cost|_repop_pending"),
    ("turn_lifecycle", r"_on_proxy_stream_end|_cancel_active|_cancel_inprocess|_check_and_start|_publish_turn_status|_session_harness_name"),
    ("subagent", r"_post_subagent|_schedule_subagent|_rewake_parent|_mark_subagent|_recover_sub_agent"),
    ("advisor", r"_run_turn_advisor|_apply_advisor|_advisor_spec"),
    ("comment_relay", r"_ensure_comment_relay"),
    ("turn_execution", r"_run_turn_bg|_drain_streaming|_stream_message"),
]


def _closure_def_chunks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split indented closure definitions (runner factory exec chunks)."""
    pat = re.compile(r"^    (?:@app\.\w+\(|async def (\w+)|def (\w+))")
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        name = m.group(1) or m.group(2) or "delete_session"
        starts.append((i, name))
    chunks: list[tuple[str, list[str]]] = []
    for idx, (start, name) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        chunks.append((name, lines[start:end]))
    return chunks


def decompose_delete_session() -> dict:
    """Split runner delete_session route chunk into helper submodules."""
    src = ROOT / "omnigent/runner/app/routes/delete_session.py"
    lines = read_lines(src)
    before = len(lines)

    pkg = src.parent / "delete_session"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    groups: dict[str, list[str]] = {}
    route_chunk: list[str] | None = None
    for name, chunk in _closure_def_chunks(lines):
        if name == "delete_session":
            route_chunk = chunk
            continue
        group = classify(name, DELETE_SESSION_GROUPS)
        groups.setdefault(group, []).extend(chunk)
        groups[group].append("\n")

    submodules = sorted(groups)
    for group in submodules:
        write(pkg / f"_{group}.py", "".join(groups[group]))

    if route_chunk is None:
        raise RuntimeError("delete_session route chunk not found")
    write(pkg / "_route.py", "".join(route_chunk))
    write(pkg / "__init__.py", '"""Runner session-turn helpers split from delete_session route chunk."""\n')

    src.unlink()

    factory = ROOT / "omnigent/runner/app/factory.py"
    factory_lines = read_lines(factory)
    new_factory: list[str] = []
    i = 0
    while i < len(factory_lines):
        line = factory_lines[i]
        if line.strip() == '"delete_session.py",':
            for group in submodules:
                new_factory.append(f'        "delete_session/_{group}.py",\n')
            new_factory.append('        "delete_session/_route.py",\n')
            i += 1
            continue
        new_factory.append(line)
        i += 1
    write(factory, "".join(new_factory))

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


def decompose_sqlalchemy_store() -> dict:
    """Split conversation store helpers and SqlAlchemyConversationStore class."""
    src = ROOT / "omnigent/stores/conversation_store/sqlalchemy_store.py"
    lines = read_lines(src)
    before = len(lines)
    source = "".join(lines)
    pre = preamble_end(lines)
    preamble = "".join(lines[:pre])

    pkg = src.parent / "sqlalchemy_store"
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir()

    helper_chunks: list[str] = []
    store_chunk: list[str] | None = None
    for name, chunk in ast_top_level_chunks(source, lines, start_line=pre + 1):
        if name == "SqlAlchemyConversationStore":
            store_chunk = chunk
        else:
            helper_chunks.extend(chunk)
            helper_chunks.append("\n")

    write(pkg / "_helpers.py", preamble + "".join(helper_chunks))
    write(
        pkg / "_store.py",
        preamble + HELPER_BINDING_IMPORT + "\n" + "".join(store_chunk or []),
    )
    init = thin_facade_init(
        "SQLAlchemy-backed conversation store",
        ["_helpers", "_store"],
    )
    write(pkg / "__init__.py", init)
    src.unlink()
    fix_dataclass_decorators_in_tree(pkg)

    return {
        "before": before,
        "after": len(read_lines(pkg / "__init__.py")),
        "modules": len(list(pkg.rglob("*.py"))),
        "files": {str(p.relative_to(ROOT)): len(read_lines(p)) for p in sorted(pkg.rglob("*.py"))},
    }


def run_phase3() -> dict[str, dict]:
    results = {}
    results["schemas"] = decompose_schemas()
    results["chat"] = decompose_chat()
    results["parser"] = decompose_parser()
    results["delete_session"] = decompose_delete_session()
    results["sqlalchemy_store"] = decompose_sqlalchemy_store()
    return results


# ---------------------------------------------------------------------------
# Phase 4 decompositions (repl, native bridges/forwarders, claude_sdk_executor)
# ---------------------------------------------------------------------------

REPL_HELPER_GROUPS: list[tuple[str, str]] = [
    ("adapter", r"_SessionsChatReplAdapter|_server_event_to_sdk_event|_elicitation_resolve"),
    ("entry", r"^run_repl$"),
    (
        "commands",
        r"^_cmd_|^handle_slash_command$|^register_skill|^unregister_skill|^_SlashCommandCompleter|^_consume_pending_local_skill|^_cmd$",
    ),
    (
        "overview",
        r"overview|_TerminalInfo|_terminal_|_tmux_|_parse_sub_agent|_collect_overview|_open_terminal|_reconstruct_terminal|_build_terminal|_list_all_conversation",
    ),
    ("approval", r"Approval|elicitation|_make_elicitation|_build_elicitation"),
    (
        "render",
        r"render|history|_plan_output|_TurnProseTracker|_OutputItemRenderPlan|_extract_message|_extract_function|TimedFormatter|_failed_status",
    ),
    (
        "context",
        r"context|_refresh_session|_update_context|_items_for_context|_fetch_context|_ContextItems|compact|_start_new_conversation|_attach_to_conversation|switch",
    ),
    (
        "startup",
        r"startup|_load_startup|_StartupHeader|_build_startup|_render_startup|_display_cwd|_summarize|_header_glyph|_humanize_agent|_is_remote_server|_maybe_write_session|_clear_screen",
    ),
    ("model", r"model|effort|_set_session_reasoning"),
]


CODEX_FORWARDER_GROUPS: list[tuple[str, str]] = [
    (
        "fwd_state",
        r"^_CodexForwarderState|^_CodexToolCall|^_PartialTextBuffer|^_ForwarderTarget|^_CodexTurnStatusEdge|^_Delta|^_SessionUsageCoalescer|^_PendingCodexElicitation|^_CodexElicitationTaskTracker",
    ),
    (
        "supervisor",
        r"^supervise_forwarder|^_maybe_rotate|^_create_thread_replacement|^_fetch_session_snapshot|^_subscribe_until_ready|^_event_indicates|^_is_thread_not_ready",
    ),
    (
        "events",
        r"^_handle_event|^_resolve_event|^_event_targets|^_maybe_handle_codex_request|^_maybe_handle_turn|^_maybe_handle_delta|^_handle_completed_event|^_handle_terminal_turn|^_handle_collab|^_handle_agent_message|^_handle_plan_delta|^_handle_usage|^_handle_turn_plan|^_handle_turn_started|^_handle_terminal_turn_event|^_handle_completed_item|^_claim_completed",
    ),
    (
        "elicitation",
        r"elicitation|plan_implementation|^_pending_elicitation|^_codex_elicitation|^_post_codex_elicitation|^_post_external_elicitation|^_note_native_plan",
    ),
    (
        "collab",
        r"collab|_child_session|_ensure_child|_register_child|_extract_child|_backfill_child|_resume_child|_codex_child|_thread_spawn|_post_collab",
    ),
    ("posting", r"^_post_|^_log_post|^_should_retry_post|^_post_retry|^_log_failed"),
    (
        "resume",
        r"resume|_replay_resume|_refresh_model|_sync_model|_wait_for_thread|_thread_started|_thread_id|_parent_thread|_find_turn_user",
    ),
    (
        "deltas",
        r"delta|_OutputTextDelta|_record_partial|_claim_partial|_try_recover_active|_is_active_turn_delta|_streaming_message|_item_id_from_delta|_maybe_persist_interrupted",
    ),
    (
        "turn",
        r"^_turn_|^_params_with|^_user_message|^_plan_|^_json_string|^_is_codex_skill|^_response_id|^_source_id|^_completed_item|^_command_execution|^_file_change|^_web_search|^_codex_tool_call|^_sleep$|^_ensure_user_message",
    ),
]


CLAUDE_BRIDGE_GROUPS: list[tuple[str, str]] = [
    (
        "types",
        r"^ClaudeTranscriptItem|^TranscriptReadResult|^ClaudeHookRecord|^HookReadResult|^_Jsonl|ClaudeMessageDelta|^MessageDeltaReadResult|^ClaudeNativeToolRelay",
    ),
    (
        "bridge_io",
        r"^bridge_dir|^prepare_bridge|^build_claude_native_spawn|^ensure_claude|^read_active|^read_launch|^read_bridge|^write_active|^read_permission|^build_mcp_config|^build_hook_settings|^_atomic_write|^read_transcript_path|^read_claude_session|^read_seen|^write_tmux|^_ensure_secure|^_absolute_syntactic|^_trusted_parent",
    ),
    (
        "transcript_read",
        r"^read_transcript|^read_assistant|^count_transcript|^transcript_has|^_transcript_timestamp|^_sample_transcript|^_local_command_name",
    ),
    (
        "transcript_convert",
        r"^_user_transcript|^_assistant_transcript|^_attachment_transcript|^_local_command_transcript|^_terminal_command|^_transcript_items_from|^_SlashCommandPayload|^_parse_slash_command|^_assistant_message_item|^_tool_result_output|^_transcript_source|^_parent_or_record|^_response_id_from_source|^_source_id",
    ),
    ("hooks", r"^read_hook|^count_hook|^stop_hook|^_hook_record|^_read_complete_jsonl|^record_hook"),
    ("inject", r"^inject_|^kill_session|^display_cost|^post_tools"),
    (
        "tmux",
        r"^_run_tmux|^_capture_pane|^_claude_prompt|^_submit_needle|^_draft_in|^_wait_for_claude_prompt|^_paste_payload|^_wait_for_tmux",
    ),
    (
        "mcp",
        r"^_mcp_|^_call_mcp|^_call_relay|^_build_tools|^_write_jsonrpc|^_stdio_jsonrpc|^_handle_mcp|^_tool_relay|^_run_relay|^_notification_writer|^main$|^_parse_args|^_serve_mcp|^_start_http|^_handler_factory|start_tool_relay|^_mcp_error|^_normalize_relay|^_empty_object",
    ),
    (
        "cost",
        r"^_transcript_model_pricing|^compute_transcript_cumulative|^_usage_from_transcript|^_model_from_transcript|^read_claude_context|^read_claude_status|^read_user_status|^read_user_effort|^_assistant_text_from",
    ),
    (
        "args",
        r"^augment_claude|^url_component|^_merge_disallowed|^_wait_for_server_info|^_read_json_file|^_write_json_file",
    ),
]


CLAUDE_NATIVE_GROUPS: list[tuple[str, str]] = [
    ("entry", r"^run_claude_native$|^resolve_native_claude_config|^build_native_claude_terminal_env"),
    ("types", r"^PreparedClaudeTerminal|^ClaudeNativeUcodeConfig|^_ResumeWorkspace|^_AttachOutcome|^_ClaudeTerminalTmux"),
    (
        "resume_ui",
        r"resume_workspace|^_prompt_resume|^_pick_resume|^_bind_resume|^_append_resume|^_resume_workspace|^_has_running_event_loop|^_stream_is_tty",
    ),
    (
        "transcript",
        r"transcript|^_claude_transcript|^_synthetic_claude|^_claude_user_content|^_claude_assistant|^_claude_text_blocks|^_json_object_from_string|^_redirect_claude|^_copy_transcript|^_clone_claude|^_find_claude|^_claude_project|^_sanitize_claude|^_fetch_external",
    ),
    ("config", r"^_ucode_config|^_provider_config|^_native_claude_config|^_materialize_claude"),
    ("local_server", r"^_run_with_local_server|^_mark_startup"),
    ("remote_server", r"^_run_with_remote_server"),
    (
        "terminal",
        r"^_prepare_claude_terminal|^_launch_claude|^_create_claude_session|^_ensure_claude_terminal|^_wait_for_claude|^_close_claude|^_find_running_claude|^_read_claude_terminal|^_claude_terminal_request|^_merge_default_model|attach_local_terminal|^_tmux_profile|^_can_attach_direct|^_attach_direct|^_attach_with_transcript|^_attach_with_reconnect|^_is_terminal|^_close_ws|^_sleep$",
    ),
    (
        "cwd",
        r"^_align_working_directory|^_switch_to_recorded|^_resolve_session_id_for_resume|^_record_launch|^_strip_resume|^_preflight_local",
    ),
    (
        "cold_resume",
        r"^_resolve_cold_resume|^_ensure_local_claude_resume|^_fetch_all_session_items_for_claude|^_fetch_claude_session_labels",
    ),
]


CODEX_NATIVE_GROUPS: list[tuple[str, str]] = [
    ("entry", r"^run_codex_native$|^codex_terminal_resource_id$"),
    ("types", r"^LaunchedCodexTerminal|^PreparedCodexTerminal|^_ResumeWorkspaceActionOption"),
    (
        "resume_ui",
        r"resume_workspace|^_prompt_codex_resume|^_codex_resume_workspace|^_switch_to_recorded|^_resolve_session_id_for_resume|^_align_working_directory|^_record_launch|^_update_startup",
    ),
    (
        "rollout",
        r"rollout|^_codex_rollout|^_codex_event_msg|^_codex_response|^_codex_message_payload|^_codex_function_call|^_codex_content_blocks|^_codex_turn_id|^_copy_rollout|^_clone_codex|^_find_codex_rollout|^_ensure_local_codex_resume|^_interrupted_response|^_codex_rollout_timestamp",
    ),
    ("local_server", r"^_run_with_local_server|^_materialize_codex"),
    ("remote_server", r"^_run_with_remote_server"),
    (
        "terminal",
        r"^_prepare_codex_terminal|^_launch_codex|^_create_codex_session|^_ensure_codex_terminal|^_wait_for_codex|^_close_codex|^_find_running_codex|^_attach_|^_can_attach_direct|^_direct_tmux|^_start_codex_forwarder|^_initialize_fresh|^_attach_terminal|^_post_initial_prompt|^_wait_for_thread|^_start_initial_turn|^_patch_external|^_launched_codex_terminal|^_codex_terminal_lookup|^_response_error|^_runner_offline|^_preflight_local|^_active_codex_session|^_mint_codex_thread",
    ),
    ("session_items", r"^_fetch_all_session_items_for_codex|^_fetch_codex_session|^_session_item_response"),
]


CLAUDE_SDK_EXECUTOR_GROUPS: list[tuple[str, str]] = [
    (
        "protocols",
        r"^_[A-Z].*Obj$|^_ClaudeSDK$|^_ClaudeQuery$|^_Stream$|^_ClaudeTransport$|^_ClaudeClient$|^_Process$|^_CancelScope$|^_TaskGroup$|^_TaskHandle$",
    ),
    ("types", r"^_ClaudeClientState|^PreparedClaudeCli|^_ResolvedSkills"),
    ("executor", r"^ClaudeSDKExecutor$"),
    (
        "mcp",
        r"mcp|^_build_mcp|^_omnigent_mcp|^_generated_sdk_mcp|^_build_sdk_mcp|^_sanitize_claude_mcp|^_claude_sdk_relay|^_build_stdio_bridge|^_claude_sdk_visible|^_omnigent_tool",
    ),
    (
        "cli",
        r"^prepare_claude_cli_path|^prepare_tight_cli|^prepare_claude|^_find_system_claude|^_resolve_gateway|^_databricks_claude|^_parse_optional_int|^_claude_internal",
    ),
    ("content", r"^_parse_data_uri|^_to_anthropic|^_multimodal_message"),
    (
        "process",
        r"^_sandbox_disabled|^_terminate_process|^_kill_process|^_unset_env_var|^_call_optional_method|^_best_effort_close|^_ensure_sdk|^_resolve_skills",
    ),
]


def decompose_repl() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/repl/_repl.py",
        "Rich-based REPL for omnigent",
        REPL_HELPER_GROUPS,
    )


def decompose_codex_native_forwarder() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/codex_native_forwarder.py",
        "Codex native event forwarder",
        CODEX_FORWARDER_GROUPS,
    )


CLAUDE_FORWARDER_GROUPS: list[tuple[str, str]] = [
    (
        "fwd_state",
        r"^HookForwardState|^SubagentEntry|^SubagentForwardState|^TranscriptForwardState|^DeltaForwardState|^_ForwardDedupeState|^_TranscriptCostCacheEntry|^_PostRetry",
    ),
    (
        "subagent",
        r"subagent|_subagents_dir|_read_subagent|_write_subagent|_post_external_subagent|_forward_available_subagents",
    ),
    (
        "cost",
        r"cost|_cumulative_cost|_transcript_cost|_session_cost|_forward_session_cost",
    ),
    (
        "supervisor",
        r"^supervise_forwarder|^_supervisor_|^_maybe_rotate|^_seed_fork|^_create_clear|^_create_fork",
    ),
    (
        "transcript",
        r"^forward_claude_transcript|transcript|_forward_transcript|_read_transcript|_parse_transcript|_emit_transcript|delta|_forward_delta|_handle_transcript",
    ),
    (
        "hooks",
        r"hook|_forward_hook|_process_hook|_emit_hook",
    ),
]


def decompose_claude_native_forwarder() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/claude_native_forwarder.py",
        "Claude native transcript forwarder",
        CLAUDE_FORWARDER_GROUPS,
    )


def decompose_claude_native_bridge() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/claude_native_bridge.py",
        "Claude Code native bridge helpers",
        CLAUDE_BRIDGE_GROUPS,
    )


def decompose_claude_native() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/claude_native.py",
        "Claude Code native terminal launcher",
        CLAUDE_NATIVE_GROUPS,
    )


def decompose_codex_native() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/codex_native.py",
        "Codex native terminal launcher",
        CODEX_NATIVE_GROUPS,
    )


def decompose_claude_sdk_executor() -> dict:
    return decompose_ast_monolith(
        ROOT / "omnigent/inner/claude_sdk_executor.py",
        "ClaudeSDKExecutor harness",
        CLAUDE_SDK_EXECUTOR_GROUPS,
    )


def run_phase4() -> dict[str, dict]:
    results = {}
    results["repl"] = decompose_repl()
    results["codex_native_forwarder"] = decompose_codex_native_forwarder()
    results["claude_native_forwarder"] = decompose_claude_native_forwarder()
    results["claude_native_bridge"] = decompose_claude_native_bridge()
    results["claude_native"] = decompose_claude_native()
    results["codex_native"] = decompose_codex_native()
    results["claude_sdk_executor"] = decompose_claude_sdk_executor()
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "phase2":
        for name, stats in run_phase2().items():
            print(f"{name}:", stats)
    elif len(sys.argv) > 1 and sys.argv[1] == "phase3":
        for name, stats in run_phase3().items():
            print(f"{name}:", stats)
    elif len(sys.argv) > 1 and sys.argv[1] == "phase4":
        for name, stats in run_phase4().items():
            print(f"{name}:", stats)
    else:
        print("sessions:", decompose_sessions())
        print("runner:", decompose_runner())