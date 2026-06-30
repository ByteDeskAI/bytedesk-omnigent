"""Two-tier agent cache — disk + in-memory — backed by ArtifactStore."""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from pathlib import Path

from omnigent.entities import LoadedAgent
from omnigent.spec import AgentSpec
from omnigent.spec import load as load_spec
from omnigent.stores.artifact_store import ArtifactStore


class AgentCache:
    """
    Two-tier cache for loaded agents.

    Tier 1 (in-memory): parsed AgentSpec objects keyed by agent_id.
    Tier 2 (disk): extracted immutable bundle generations under
    cache_dir/<agent_id>/<bundle-generation>/.
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
            Each bundle generation is extracted to
            ``<cache_dir>/<agent_id>/<bundle-generation>/``.
        """
        self._artifact_store = artifact_store
        self._cache_dir = cache_dir
        self._specs: dict[tuple[str, str, bool], AgentSpec] = {}
        self._workdirs: dict[tuple[str, str, bool], Path] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

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
        cache_key = (agent_id, bundle_location, expand_env)
        with self._agent_lock(agent_id):
            return self._load_locked(agent_id, bundle_location, cache_key, expand_env=expand_env)

    def _load_locked(
        self,
        agent_id: str,
        bundle_location: str,
        cache_key: tuple[str, str, bool],
        *,
        expand_env: bool,
    ) -> LoadedAgent:
        """Load an agent while the per-agent cache lock is held."""
        if cache_key in self._specs:
            return LoadedAgent(
                spec=self._specs[cache_key],
                workdir=self._workdirs[cache_key],
            )

        generation_workdir = self._workdir_for(agent_id, bundle_location)
        loaded = self._try_load_disk_cache(
            cache_key,
            generation_workdir,
            bundle_location,
            expand_env=expand_env,
        )
        if loaded is not None:
            return loaded

        legacy_workdir = self._legacy_workdir(agent_id)
        if legacy_workdir != generation_workdir:
            loaded = self._try_load_disk_cache(
                cache_key,
                legacy_workdir,
                bundle_location,
                expand_env=expand_env,
            )
            if loaded is not None:
                return loaded

        # Cache miss — download bundle, write to temp file, extract
        bundle_bytes = self._artifact_store.get(bundle_location)
        return self._extract_and_cache(
            agent_id,
            bundle_location,
            bundle_bytes,
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
        with self._agent_lock(agent_id):
            return self._extract_and_cache(
                agent_id,
                bundle_location,
                bundle_bytes,
                expand_env=expand_env,
            )

    def evict(self, agent_id: str) -> None:
        """
        Remove an agent from both cache tiers. Called when an
        agent is deleted. No-op if the agent is not cached.

        :param agent_id: Unique agent identifier,
            e.g. ``"ag_abc123"``.
        """
        with self._agent_lock(agent_id):
            self._specs = {key: value for key, value in self._specs.items() if key[0] != agent_id}
            self._workdirs = {
                key: value for key, value in self._workdirs.items() if key[0] != agent_id
            }
            workdir = self._legacy_workdir(agent_id)
            if workdir.is_dir():
                shutil.rmtree(workdir)

    def _extract_and_cache(
        self,
        agent_id: str,
        bundle_location: str,
        bundle_bytes: bytes,
        *,
        expand_env: bool = False,
    ) -> LoadedAgent:
        """
        Extract bundle bytes to disk and populate both cache tiers.

        :param agent_id: Unique agent identifier.
        :param bundle_location: Artifact store key for the bundle.
        :param bundle_bytes: Raw bytes of the ``.tar.gz`` bundle.
        :param expand_env: Whether to expand ``${VAR}`` references
            against the server process environment. Forwarded from
            :meth:`load`; defaults to ``False`` (fail-safe). See
            :meth:`load` for the rationale.
        :returns: A LoadedAgent with the parsed spec and workdir.
        """
        workdir = self._workdir_for(agent_id, bundle_location)
        agent_root = self._legacy_workdir(agent_id)
        agent_root.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{_bundle_generation(bundle_location)}.",
                suffix=".staging",
                dir=agent_root,
            )
        )
        try:
            spec = load_spec(bundle_bytes, dest=staging_dir, expand_env=expand_env)
            _write_bundle_marker(staging_dir, bundle_location)
            if workdir.is_dir():
                shutil.rmtree(workdir)
            staging_dir.rename(workdir)
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

        self._specs = {key: value for key, value in self._specs.items() if key[0] != agent_id}
        self._workdirs = {
            key: value for key, value in self._workdirs.items() if key[0] != agent_id
        }
        self._specs[(agent_id, bundle_location, expand_env)] = spec
        self._workdirs[(agent_id, bundle_location, expand_env)] = workdir
        return LoadedAgent(spec=spec, workdir=workdir)

    def _try_load_disk_cache(
        self,
        cache_key: tuple[str, str, bool],
        workdir: Path,
        bundle_location: str,
        *,
        expand_env: bool,
    ) -> LoadedAgent | None:
        if not workdir.is_dir() or _read_bundle_marker(workdir) != bundle_location:
            return None
        try:
            spec = load_spec(workdir, expand_env=expand_env)
        except FileNotFoundError:
            return None
        self._specs[cache_key] = spec
        self._workdirs[cache_key] = workdir
        return LoadedAgent(spec=spec, workdir=workdir)

    def _agent_lock(self, agent_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(agent_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[agent_id] = lock
            return lock

    def _legacy_workdir(self, agent_id: str) -> Path:
        return self._cache_dir / agent_id

    def _workdir_for(self, agent_id: str, bundle_location: str) -> Path:
        return self._legacy_workdir(agent_id) / _bundle_generation(bundle_location)


_BUNDLE_MARKER = ".omnigent-bundle-location"
_SAFE_GENERATION_RE = re.compile(r"^[A-Za-z0-9._=-]+$")


def _bundle_generation(bundle_location: str) -> str:
    candidate = bundle_location.rstrip("/").rsplit("/", 1)[-1]
    if candidate and candidate not in {".", ".."} and _SAFE_GENERATION_RE.fullmatch(candidate):
        return candidate
    import hashlib

    return hashlib.sha256(bundle_location.encode()).hexdigest()


def _read_bundle_marker(workdir: Path) -> str | None:
    marker = workdir / _BUNDLE_MARKER
    if not marker.is_file():
        return None
    return marker.read_text().strip() or None


def _write_bundle_marker(workdir: Path, bundle_location: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / _BUNDLE_MARKER).write_text(bundle_location)
