"""The Skills Concierge built-in agent bundle parses and is wired correctly.

The Concierge (``deploy/bytedesk/agents/skills-concierge``) is a ``claude-sdk``
built-in the web Skills surface talks to. These tests pin the load-bearing wiring
so a spec edit can't silently break it: the skills stdio MCP front is attached
with the right tool allowlist, the verification-probe builtins are granted (via
``async`` + ``spawn``), the saga skill is bundled, and it stays registered in the
startup seed list.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from omnigent.server.bundles import validate_agent_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLE_DIR = _REPO_ROOT / "deploy" / "bytedesk" / "agents" / "skills-concierge"
_SERVER_MANIFEST = _REPO_ROOT / "deploy" / "bytedesk" / "k8s" / "server.yaml"

# The sys_skill_* opt-in builtins that replaced the old stdio MCP front
# (BDP-2487). Listing them in tools.builtins is the opt-in grant.
_SKILL_BUILTINS = {
    "sys_skill_search",
    "sys_skill_sources",
    "sys_skill_installed",
    "sys_skill_resolve_targets",
    "sys_skill_stage_preview",
    "sys_skill_apply",
    "sys_skill_remove",
}


def _bundle_bytes(root: Path) -> bytes:
    """Pack a bundle directory into the ``.tar.gz`` ``validate_agent_bundle`` takes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                tf.add(path, arcname=path.relative_to(root).as_posix())
    return buf.getvalue()


@pytest.fixture(scope="module")
def spec():
    return validate_agent_bundle(_bundle_bytes(_BUNDLE_DIR))


def test_concierge_runs_on_claude_sdk(spec) -> None:
    assert spec.name == "skills-concierge"
    assert spec.executor.config.get("harness") == "claude-sdk"


def test_skill_builtins_attached_and_stdio_front_gone(spec) -> None:
    # The seven sys_skill_* tools must be granted as opt-in builtins.
    declared = {b.name for b in spec.tools.builtins}
    assert declared >= _SKILL_BUILTINS, (
        "the sys_skill_* install builtins must be listed in tools.builtins"
    )
    # The old stdio `skills` MCP front must be gone — the runner held no server
    # credential, so its require_user mutating routes 401'd (BDP-2487).
    servers = {s.name for s in spec.mcp_servers}
    assert "skills" not in servers, "the legacy skills stdio MCP front must be removed"


def test_probe_surface_is_granted(spec) -> None:
    # async → inbox builtins to await each probe; spawn → sys_session_create
    # against any registered target agent. Both are required by the saga.
    assert spec.async_enabled is True
    assert spec.spawn is True


def test_saga_skill_is_bundled(spec) -> None:
    assert any(s.name == "skills-install" for s in spec.skills)


def test_prompt_encodes_three_state_verification_and_idempotency(spec) -> None:
    # The top-level ``prompt:`` YAML is the legacy alias for ``instructions``.
    prompt = spec.instructions or ""
    # The saga's load-bearing invariants must be present in the standing prompt.
    assert "installed and ready to go" in prompt
    assert "UNVERIFIABLE" in prompt
    assert "skip_existing" in prompt


def test_concierge_registered_in_startup_seed_list() -> None:
    manifest = _SERVER_MANIFEST.read_text()
    assert "/build/deploy/bytedesk/agents/skills-concierge" in manifest, (
        "skills-concierge must be appended to OMNIGENT_BUILTIN_AGENT_DIRS so the "
        "startup seeder registers it as a built-in"
    )
