"""Two-tier agent cache — disk + in-memory — backed by ArtifactStore."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from omnigent.entities import LoadedAgent
from omnigent.spec import AgentSpec
from omnigent.spec import load as load_spec
from omnigent.stores.artifact_store import ArtifactStore


class AgentCache:
    """
    Two-tier cache for loaded agents.

    Tier 1 (in-memory): parsed AgentSpec objects keyed by agent_id.
    Tier 2 (disk): extracted agent directories under cache_dir/<agent_id>/.
    Source of truth: ArtifactStore (tarball bytes).

    On cache miss the bundle is downloaded from the ArtifactStore,
    extracted to disk, parsed, validated, and stored in both tiers.
    """

    def __init__(self, artifact_store: ArtifactStore, cache_dir: Path) -> None:
        """
        Initialize the two-tier agent cache.

        :param artifact_store: The ArtifactStore holding agent
            bundle tarballs (source of truth).
        :param cache_dir: Root directory for the disk cache.
            Each agent is extracted to
            ``<cache_dir>/<agent_id>/``.
        """
        self._artifact_store = artifact_store
        self._cache_dir = cache_dir
        self._specs: dict[tuple[str, str, bool], AgentSpec] = {}
        self._bundle_locations: dict[str, str] = {}

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Load an agent, populating caches on miss.

        Raises KeyError if the agent bundle does not exist in the
        ArtifactStore. Raises ValueError if the spec is invalid.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        :param bundle_location: Artifact store key for the bundle,
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param expand_env: Whether to expand ``${VAR}`` references in
            the spec against the server process environment. Defaults
            to ``False`` and MUST stay ``False`` for tenant-supplied
            (session-scoped) agents: expanding their ``${VAR}``
            against the server env leaks secrets into a spec-controlled
            MCP/LLM connection. Callers pass
            ``expand_env=True`` only for operator-authored template
            agents (``Agent.session_id is None`` — ``--agent`` /
            built-ins). The default is fail-safe: a caller that
            forgets the flag gets no expansion (a template agent may
            fail to resolve, loudly) rather than a silent leak.
        :returns: A LoadedAgent with the parsed spec and the
            on-disk working directory.
        """
        workdir = self._cache_dir / agent_id
        cache_key = (agent_id, bundle_location, expand_env)

        # Tier 1: in-memory spec. The cached spec was parsed with the
        # *expand_env* value of whichever caller populated it first.
        # That is consistent across callers because *expand_env* is
        # derived from the agent's immutable ``session_id`` provenance,
        # which never changes for a given ``agent_id``.
        if cache_key in self._specs:
            return LoadedAgent(spec=self._specs[cache_key], workdir=workdir)

        # Tier 2: disk cache (directory already extracted)
        cached_bundle = self._bundle_locations.get(agent_id) or _read_bundle_marker(workdir)
        if workdir.is_dir() and cached_bundle == bundle_location:
            spec = load_spec(workdir, expand_env=expand_env)
            self._specs[cache_key] = spec
            self._bundle_locations[agent_id] = bundle_location
            return LoadedAgent(spec=spec, workdir=workdir)

        # Cache miss — download bundle, write to temp file, extract
        if workdir.is_dir():
            shutil.rmtree(workdir)
        bundle_bytes = self._artifact_store.get(bundle_location)
        return self._extract_and_cache(
            agent_id,
            bundle_location,
            bundle_bytes,
            workdir,
            expand_env=expand_env,
        )

    def replace(
        self,
        agent_id: str,
        bundle_location: str,
        bundle_bytes: bytes,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Warm-swap an agent's cached spec and disk directory.

        Extracts the new bundle to a temp directory, swaps the
        in-memory spec entry, renames into the cache location, and
        cleans up the old directory. Concurrent readers see either
        the old spec or the new spec, never an empty cache.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        :param bundle_location: New artifact store key (unused
            during extraction but passed for consistency),
            e.g. ``"ag_abc123/a1b2c3d4e5f6..."``.
        :param bundle_bytes: Raw bytes of the new ``.tar.gz``
            bundle.
        :param expand_env: Whether to expand ``${VAR}`` references
            against the server process environment. Defaults to
            ``False`` (fail-safe); pass ``True`` only for
            operator-authored template agents. See :meth:`load` for
            the full rationale.
        :returns: A LoadedAgent with the new spec and working
            directory.
        """
        workdir = self._cache_dir / agent_id
        staging_dir = self._cache_dir / f"{agent_id}_staging"

        # Extract new bundle to staging directory
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(bundle_bytes)
            spec = load_spec(tmp_path, dest=staging_dir, expand_env=expand_env)
        finally:
            tmp_path.unlink()

        # Swap in-memory entry (atomic dict assignment)
        self._specs = {
            key: value for key, value in self._specs.items() if key[0] != agent_id
        }
        self._specs[(agent_id, bundle_location, expand_env)] = spec
        self._bundle_locations[agent_id] = bundle_location

        # Replace disk directory: remove old, rename staging into place
        if workdir.is_dir():
            shutil.rmtree(workdir)
        staging_dir.rename(workdir)
        _write_bundle_marker(workdir, bundle_location)

        return LoadedAgent(spec=spec, workdir=workdir)

    def evict(self, agent_id: str) -> None:
        """
        Remove an agent from both cache tiers. Called when an
        agent is deleted. No-op if the agent is not cached.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        """
        self._specs = {
            key: value for key, value in self._specs.items() if key[0] != agent_id
        }
        self._bundle_locations.pop(agent_id, None)
        workdir = self._cache_dir / agent_id
        if workdir.is_dir():
            shutil.rmtree(workdir)

    def _extract_and_cache(
        self,
        agent_id: str,
        bundle_location: str,
        bundle_bytes: bytes,
        workdir: Path,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Extract bundle bytes to disk and populate both cache tiers.

        :param agent_id: Unique agent identifier.
        :param bundle_location: Artifact store key for the bundle.
        :param bundle_bytes: Raw bytes of the ``.tar.gz`` bundle.
        :param workdir: Target directory for extraction.
        :param expand_env: Whether to expand ``${VAR}`` references
            against the server process environment. Forwarded from
            :meth:`load`; defaults to ``False`` (fail-safe). See
            :meth:`load` for the rationale.
        :returns: A LoadedAgent with the parsed spec and workdir.
        """
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(bundle_bytes)
            spec = load_spec(tmp_path, dest=workdir, expand_env=expand_env)
        finally:
            tmp_path.unlink()

        self._specs[(agent_id, bundle_location, expand_env)] = spec
        self._bundle_locations[agent_id] = bundle_location
        _write_bundle_marker(workdir, bundle_location)
        return LoadedAgent(spec=spec, workdir=workdir)


_BUNDLE_MARKER = ".omnigent-bundle-location"


def _read_bundle_marker(workdir: Path) -> str | None:
    marker = workdir / _BUNDLE_MARKER
    if not marker.is_file():
        return None
    return marker.read_text().strip() or None


def _write_bundle_marker(workdir: Path, bundle_location: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / _BUNDLE_MARKER).write_text(bundle_location)
