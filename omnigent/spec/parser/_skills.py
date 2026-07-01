"""Parse an agent image directory into an AgentSpec."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import (
    DEFAULT_BASIC_USERNAME,
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
    TerminalEnvSpec,
)
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    AgentSpec,
    ApiKeyAuth,
    BlueprintLoopSpec,
    BlueprintNode,
    BlueprintSpec,
    BuiltinToolConfig,
    CompactionConfig,
    DatabricksAuth,
    ExecutorSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    InteractionConfig,
    LabelDef,
    LLMConfig,
    LocalToolInfo,
    MCPOAuthConfig,
    MCPServerConfig,
    ModalityConfig,
    Phase,
    PhaseSelector,
    PolicyAction,
    PolicySpec,
    ProviderAuth,
    RetryPolicy,
    SandboxConfig,
    SkillSpec,
    ToolsConfig,
)

_log = logging.getLogger(__name__)

# Context files scanned in priority order when ``instructions:`` is absent.
# First file found wins (no merge).
_CONTEXT_FILE_PRIORITY: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".cursorrules")

# Pattern for SKILL.md YAML frontmatter delimited by ---
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


# Allowed tool ``type`` values when the supervisor harness is
# selected (``config.harness == "databricks_supervisor"``). Each entry maps the
# tool type to its required field names — the parser enforces both
# membership and required fields. Lives at the top of the module so
# they are easy to grep and so two functions cannot independently
# duplicate the same set.
#
# Adding a new tool type is a one-line change here plus a parser
# test — no runtime, harness, or workflow code touches needed. See
# ``designs/DATABRICKS_SUPERVISOR_API_INTEGRATION.md`` for the recipe and the
# rationale for why these tools are Databricks-resident only.
_SUPERVISOR_TOOL_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "genie_space": frozenset({"id", "description"}),
    "uc_function": frozenset({"name", "description"}),
    "uc_connection": frozenset({"name", "description"}),
    "app": frozenset({"name", "description"}),
    "knowledge_assistant": frozenset({"knowledge_assistant_id", "description"}),
    "uc_table": frozenset({"table_name", "description"}),
    "volume": frozenset({"name", "description"}),
}


def _import_package_bindings() -> None:
    from . import _constants as _pkg_constants
    from . import _state as _pkg_state
    g = globals()
    for _mod in (_pkg_constants, _pkg_state):
        for _key, _value in _mod.__dict__.items():
            if not _key.startswith("__"):
                g[_key] = _value


_import_package_bindings()

def _read_contained_file(root: Path, value: str) -> str | None:
    """
    Read a bundle-relative file named by *value*, only if it stays in *root*.

    The instruction-file reference comes from a spec field (``instructions:``)
    that, for an uploaded bundle, is attacker-controlled. Resolving symlinks
    and ``..`` and confirming the target is contained in *root* prevents a
    crafted spec (e.g. ``instructions: ../../etc/passwd``) from reading files
    outside the bundle on the runner. A non-contained or non-existent path
    returns ``None`` so the caller falls back to treating *value* as literal
    instruction text — preserving the existing "missing file → inline text"
    behavior for the CLI.

    :param root: The bundle root directory the value is anchored to,
        e.g. ``Path("/tmp/agent-bundle")``.
    :param value: The single-line ``instructions:`` value, e.g.
        ``"prompts/system.md"``.
    :returns: The file contents if *value* names a file contained within
        *root*, else ``None``.
    """
    candidate = root / value
    try:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root.resolve()) and resolved.is_file():
            return resolved.read_text()
    except OSError:
        # Path too long or invalid characters — treat as inline text.
        pass
    return None

def _resolve_instructions(root: Path, raw_value: object) -> str | None:
    """
    Resolve the instructions for an agent image.

    - If ``instructions`` is set in config.yaml and the value is
      a path to an existing file relative to *root*, read that
      file.
    - If ``instructions`` is set but is not a file path, treat
      the value as inline text.
    - If ``instructions`` is not set, scan ``_CONTEXT_FILE_PRIORITY``
      and return the first file found (first-wins, no merge).

    :param root: Path to the agent image directory.
    :param raw_value: The raw ``instructions`` value from
        config.yaml, or ``None`` if the key was absent. May be
        a relative file path (e.g. ``"prompts/system.md"``) or
        inline text.
    :returns: The resolved instruction text, or ``None`` if no
        instructions are available.
    """
    if raw_value is not None:
        text = str(raw_value)
        # Only attempt file lookup for short single-line values
        # that look like filenames (multiline text can't be a path).
        if "\n" not in text:
            contained = _read_contained_file(root, text)
            if contained is not None:
                return contained
        return text
    # Default: first-wins scan across known context files.
    for filename in _CONTEXT_FILE_PRIORITY:
        candidate = root / filename
        try:
            if candidate.is_file():
                return candidate.read_text()
        except OSError:
            pass
    return None

def _parse_skills_filter(raw: object) -> str | list[str]:
    """
    Parse the top-level YAML ``skills:`` field into a host-skill
    filter string or list of names.

    Distinct from the bundle-side ``skills/<name>/SKILL.md`` files
    discovered by :func:`_discover_skills` — that's the agent's own
    bundled skills, always loaded. This filter only controls
    HOST-scope skills that the harness picks up from the user's
    machine (``~/.claude/skills/`` and ancestor ``.claude/skills/``
    dirs of the cwd, when running with the Claude SDK harness).

    Supported YAML shapes:

    - field omitted / ``null`` / ``"all"`` → returns ``"all"``;
      every host skill is loaded. Default.
    - ``"none"`` or ``[]`` → returns ``"none"``; no host skills,
      hermetic against the user's local skill library.
    - ``[<name>, ...]`` → returns the list as-is; only the named
      skills are exposed.

    :param raw: The raw YAML value (already parsed). One of
        ``None``, a string, or a list.
    :returns: ``"all"``, ``"none"``, or a non-empty ``list[str]``.
    :raises OmnigentError: When the value isn't one of the
        supported shapes (e.g. boolean, dict, integer), or list
        items are non-strings, or a string isn't ``"all"`` or
        ``"none"``.
    """
    if raw is None:
        return "all"
    if isinstance(raw, str):
        if raw not in ("all", "none"):
            raise OmnigentError(
                f'top-level skills: must be "all", "none", or a list of '
                f"skill names; got string {raw!r}",
                code=ErrorCode.INVALID_INPUT,
            )
        return raw
    if isinstance(raw, list):
        if len(raw) == 0:
            # Explicit empty list reads as "no host skills" — same as "none".
            return "none"
        names: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise OmnigentError(
                    f"top-level skills: list items must be strings; "
                    f"got {type(item).__name__} {item!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            names.append(item)
        return names
    raise OmnigentError(
        f'top-level skills: must be "all", "none", or a list of skill '
        f"names; got {type(raw).__name__}",
        code=ErrorCode.INVALID_INPUT,
    )

def discover_host_skills(
    agent_root: Path,
    skills_filter: str | list[str],
) -> list[SkillSpec]:
    """
    Discover host-scope skills from ``.claude/skills/`` and
    ``.agents/skills/`` directories walking up from *agent_root*,
    plus the user's global ``~/.claude/skills/``.

    Not called by :func:`parse` — host-scope skills are a REPL
    concern, not a spec concern. Callers (e.g. ``chat.py``) merge
    the result into ``spec.skills`` before passing to
    ``run_repl``.

    :param agent_root: The agent bundle's root directory.
    :param skills_filter: The parsed ``skills:`` filter from the
        agent spec. ``"none"`` suppresses all host skills;
        ``"all"`` loads everything; a list of names loads only
        those.
    :returns: Deduplicated list of :class:`SkillSpec` objects.
        Later directories (closer to /) lose on name collision
        with earlier ones (closer to agent_root).
    """
    if skills_filter == "none":
        return []

    seen_names: set[str] = set()
    skills: list[SkillSpec] = []
    skipped: list[str] = []
    filter_names: set[str] | None = None
    if isinstance(skills_filter, list):
        filter_names = set(skills_filter)

    def _scan_dir(d: Path) -> None:
        for spec in _discover_skills(d, skipped=skipped):
            if spec.name in seen_names:
                continue
            if filter_names is not None and spec.name not in filter_names:
                continue
            seen_names.add(spec.name)
            skills.append(spec)

    # Walk from agent_root up to filesystem root, scanning
    # .claude/skills/ and .agents/skills/ at each level.
    current = agent_root.resolve()
    while True:
        for dotdir in (".claude", ".agents"):
            candidate = current / dotdir / "skills"
            if candidate.is_dir():
                _scan_dir(candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Also scan user-global skill directories.
    for dotdir in (".claude", ".agents"):
        home_skills = Path.home() / dotdir / "skills"
        if home_skills.is_dir():
            _scan_dir(home_skills)

    if skipped:
        dest = getattr(sys.stderr, "_original_stderr", sys.stderr)
        n = len(skipped)
        print(
            f"Warning: skipped {n} skill(s) with frontmatter errors:",
            file=dest,
        )
        for detail in skipped:
            print(f"  - {detail}", file=dest)
        print(
            "Fix the YAML frontmatter in the above SKILL.md file(s) to load them.",
            file=dest,
        )

    return skills

def _discover_skills(
    skills_dir: Path,
    *,
    skipped: list[str] | None = None,
) -> list[SkillSpec]:
    """
    Discover and parse all skills under the ``skills/`` directory.

    Each subdirectory containing a ``SKILL.md`` file is parsed via
    :func:`_parse_skill`.

    :param skills_dir: Path to the ``skills/`` directory, e.g.
        ``root / "skills"``.
    :param skipped: When not ``None``, enables lenient mode: YAML
        parse errors and missing-frontmatter errors are caught
        per-file, a human-readable message is appended to this
        list, and the skill is skipped instead of aborting.
        Pass ``None`` (the default) to fail loud on the first
        error — used for bundled skills that the agent author
        controls.
    :returns: A sorted list of parsed :class:`SkillSpec` objects.
        Returns an empty list if *skills_dir* does not exist.
    """
    if not skills_dir.is_dir():
        return []
    skills: list[SkillSpec] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            skill = _parse_skill(skill_md)
        except (OmnigentError, yaml.YAMLError) as exc:
            if skipped is None:
                raise
            msg = f"{skill_md}: {exc}"
            _log.warning("Skipping skill with bad frontmatter: %s", msg)
            skipped.append(msg)
            continue
        skills.append(skill)
    return skills

def _parse_skill(skill_md: Path) -> SkillSpec:
    """
    Parse a single ``SKILL.md`` file into a :class:`SkillSpec`.

    The file must begin with YAML frontmatter delimited by ``---``
    lines, containing at least ``name`` and ``description`` keys.

    :param skill_md: Path to the ``SKILL.md`` file, e.g.
        ``skills/code-review/SKILL.md``.
    :returns: A populated :class:`SkillSpec`.
    :raises OmnigentError: If the file cannot be read, or the
        frontmatter is missing, malformed, or lacks required fields.
        All failure modes funnel through a single exception type so
        the tolerant scanner in :func:`_discover_skills` (when
        ``strict=False``) can catch them uniformly.
    """
    try:
        text = skill_md.read_text()
    except OSError as exc:
        raise OmnigentError(
            f"SKILL.md could not be read: {skill_md}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise OmnigentError(
            f"SKILL.md missing YAML frontmatter: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    frontmatter_str, content = match.groups()
    try:
        frontmatter = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as exc:
        raise OmnigentError(
            f"SKILL.md has invalid YAML frontmatter: {skill_md}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if not isinstance(frontmatter, dict):
        raise OmnigentError(
            f"SKILL.md frontmatter must be a YAML mapping: {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    name = frontmatter.get("name")
    if name is None:
        raise OmnigentError(
            f"SKILL.md frontmatter missing required field 'name': {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    description = frontmatter.get("description")
    if description is None:
        raise OmnigentError(
            f"SKILL.md frontmatter missing required field 'description': {skill_md}",
            code=ErrorCode.INVALID_INPUT,
        )
    return SkillSpec(
        name=str(name),
        description=str(description),
        content=content.strip(),
        skill_dir=skill_md.parent,
    )


def _wire_sibling_modules() -> None:
    g = globals()
    from . import _capabilities as _sib_capabilities
    from . import _core as _sib_core
    from . import _credentials as _sib_credentials
    from . import _discover as _sib_discover
    from . import _guardrails as _sib_guardrails
    from . import _helpers as _sib_helpers
    from . import _llm as _sib_llm
    from . import _mcp as _sib_mcp
    from . import _os_env as _sib_os_env
    from . import _policies as _sib_policies
    from . import _tools as _sib_tools
    for _key, _value in _sib_capabilities.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_core.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_credentials.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_discover.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_guardrails.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_helpers.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_llm.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_mcp.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_os_env.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_policies.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)
    for _key, _value in _sib_tools.__dict__.items():
        if not _key.startswith("__"):
            g.setdefault(_key, _value)

_wire_sibling_modules()
