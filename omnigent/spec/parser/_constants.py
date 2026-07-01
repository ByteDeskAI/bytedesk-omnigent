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


_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAML_1_2_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_STRUCTURED_EXECUTOR_CONFIG_KEYS = frozenset({"cost_optimize"})
_BLUEPRINT_NODE_RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "kind",
        "depends_on",
        "depends",
        "when",
        "target",
        "input",
        "return",
        "output",
        "loop",
        "metadata",
    }
)
_CWD_HIDDEN_SCAN_OVERFLOW_MODES = ("error", "warn", "unlimited")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GH_BASIC_DEFAULT_TARGETS = ("github.com", "api.github.com")
_GH_TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
_UNRESOLVED_VAR_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")
_TOOLS_CONFIG_KEYS = frozenset({"agents", "builtins", "timeout", "retry", "sandbox"})

