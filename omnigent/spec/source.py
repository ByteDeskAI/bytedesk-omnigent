"""Pluggable ``SpecSource`` seam — where an agent spec's raw text comes from (BDP-2370).

Today agent specs are read off the filesystem: ``parser.parse`` opens
``<bundle>/config.yaml`` and ``_omnigent_compat`` reads a standalone YAML file. That
binds *where the bytes live* to *how they are parsed*. This module introduces the
Adapter Protocol that separates the two:

- a :class:`SpecSource` answers **"give me the raw spec text/dict for this ref"**
  (:meth:`SpecSource.load`) and **"what refs can you serve"** (:meth:`SpecSource.list`);
- the parser (``omnigent.spec.parser`` / ``_omnigent_compat``) stays **one shared
  path** that consumes that raw text — it is never forked per source.

The source yields *raw* text/dict so a future DB / URL / OCI backend only has to
implement "fetch the bytes for a ref"; it reuses the existing parser + validator
unchanged. The built-in :class:`FilesystemSpecSource` reads ``config.yaml`` exactly
as before (byte-identical), and is registered as the default in
:data:`spec_source_registry` per the :mod:`omnigent.kernel.pluggable` 4-invariant recipe.

A cache/invalidate seam (:meth:`SpecSource.invalidate`) is part of the Protocol so
the near-term hot-reload use (re-read a changed spec without a process restart) has a
place to land; the filesystem default reads fresh every call, so its
:meth:`~FilesystemSpecSource.invalidate` is a documented no-op. No DB source is built
here (YAGNI) — only the Protocol, the filesystem default, and the registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.kernel.pluggable import PluggableRegistry
from omnigent.spec.parser import _ConfigYamlLoader

# Stable seam id; also the suffix of the ``OMNIGENT_USE_SPEC_SOURCE`` override env.
SEAM = "spec_source"

# Per-extension hook consulted by the registry to merge contributed sources
# (mirrors ``artifact_store_providers``).
EXTENSION_HOOK = "spec_source_providers"

# The raw spec a source yields: either the undecoded ``config.yaml`` text, or an
# already-decoded mapping. The parser path accepts both (see ``decode_raw_spec``).
RawSpec = str | dict[str, object]


@runtime_checkable
class SpecSource(Protocol):
    """Adapter that yields a *raw* agent spec for a ref, decoupled from parsing.

    A ``SpecSource`` is responsible only for *retrieval*: turning a ref (a bundle
    name, a row id, a URL, an OCI digest — whatever the backend keys on) into the
    raw spec text/dict. Parsing + validation stay in the one shared parser path, so
    a new backend never re-implements spec semantics.
    """

    def load(self, ref: str) -> RawSpec:
        """Return the raw spec for *ref* — ``config.yaml`` text or a decoded mapping.

        :param ref: Backend-specific identifier for one spec.
        :returns: The raw spec as undecoded YAML text or an already-decoded mapping.
        :raises OmnigentError: if *ref* cannot be resolved by this source.
        """
        ...

    def list(self) -> list[str]:
        """Return the refs this source can currently serve (possibly empty)."""
        ...

    def invalidate(self, ref: str | None = None) -> None:
        """Drop any cached raw spec for *ref* (or all refs when ``None``).

        The hot-reload seam: a source that caches fetched specs clears its cache here
        so the next :meth:`load` re-reads. A source that always reads fresh (the
        filesystem default) implements this as a no-op.
        """
        ...


class FilesystemSpecSource:
    """The default :class:`SpecSource`: read ``config.yaml`` off the local filesystem.

    Behaviour is byte-identical to the historical path — ``parser.parse`` reads
    ``<bundle>/config.yaml`` with :class:`~omnigent.spec.parser._ConfigYamlLoader`,
    and this source reads the same file with the same loader. It reads fresh on every
    :meth:`load`, so :meth:`invalidate` is a no-op.

    A ref is a path to either a bundle directory (containing ``config.yaml``) or a
    ``config.yaml`` file directly. :meth:`list` enumerates the bundle directories
    under an optional *root*.
    """

    def __init__(self, root: Path | None = None) -> None:
        """Create a filesystem source.

        :param root: Optional directory scanned by :meth:`list` for bundle
            subdirectories (those containing a ``config.yaml``). When ``None``,
            :meth:`list` returns an empty list — :meth:`load` still resolves any ref.
        """
        self._root = root

    def _resolve(self, ref: str) -> Path:
        """Resolve *ref* to the ``config.yaml`` path it names.

        A directory ref resolves to ``<ref>/config.yaml``; a file ref is taken
        verbatim. Relative refs are resolved against *root* when one was given.
        """
        candidate = Path(ref)
        if not candidate.is_absolute() and self._root is not None:
            candidate = self._root / candidate
        if candidate.is_dir():
            return candidate / "config.yaml"
        return candidate

    def load(self, ref: str) -> RawSpec:
        """Read and return the raw ``config.yaml`` text for *ref*.

        Returns the undecoded text (not a parsed dict) so the shared parser owns
        decoding with its own loader — keeping the filesystem path byte-identical to
        ``parser.parse``'s ``config_path.read_text()``.

        :raises OmnigentError: if the resolved ``config.yaml`` does not exist.
        """
        config_path = self._resolve(ref)
        if not config_path.exists():
            raise OmnigentError(
                f"spec_source: config.yaml not found for ref {ref!r} (looked at {config_path})",
                code=ErrorCode.INVALID_INPUT,
            )
        return config_path.read_text()

    def list(self) -> list[str]:
        """Return bundle directory names under *root* that contain a ``config.yaml``."""
        if self._root is None or not self._root.is_dir():
            return []
        return sorted(
            child.name
            for child in self._root.iterdir()
            if child.is_dir() and (child / "config.yaml").exists()
        )

    def invalidate(self, ref: str | None = None) -> None:
        """No-op: the filesystem source reads fresh on every :meth:`load`."""


def decode_raw_spec(raw: RawSpec) -> dict[str, object]:
    """Decode a source's :data:`RawSpec` into the mapping the parser consumes.

    String payloads are parsed with :class:`~omnigent.spec.parser._ConfigYamlLoader`
    — the **same** loader ``parser.parse`` uses — so a filesystem source's text and
    that loader stay one shared decode path. Mapping payloads (already decoded by a
    DB/URL source) pass through after a shape check.

    :param raw: The raw spec from :meth:`SpecSource.load`.
    :returns: The decoded spec mapping.
    :raises OmnigentError: if the payload is not a mapping (or YAML that decodes to
        one).
    """
    if isinstance(raw, dict):
        return raw
    decoded = yaml.load(raw, Loader=_ConfigYamlLoader)
    if not isinstance(decoded, dict):
        raise OmnigentError(
            f"spec source yielded a non-mapping spec ({type(decoded).__name__})",
            code=ErrorCode.INVALID_INPUT,
        )
    return decoded


def build_spec_source_registry(
    root: Path | None = None,
) -> PluggableRegistry[SpecSource]:
    """Build the :class:`SpecSource` registry with the filesystem default.

    Follows the :mod:`omnigent.kernel.pluggable` recipe: filesystem is the registered
    default (active unless ``OMNIGENT_USE_SPEC_SOURCE`` overrides it), and extensions
    contribute alternatives via the :data:`EXTENSION_HOOK`. A future DB/URL/OCI
    source registers here without touching this module.

    :param root: Optional bundle-scan root for the filesystem default's
        :meth:`~FilesystemSpecSource.list`.
    """
    registry: PluggableRegistry[SpecSource] = PluggableRegistry(
        SEAM, default=("filesystem", lambda: FilesystemSpecSource(root))
    )
    # Extension discovery deferred to server startup (Wave-2 composition root):
    # it loads FastAPI-heavy entry-point extensions; keep off the import hot path.
    # Hook: EXTENSION_HOOK.
    return registry


# Module-level registry seeded with the filesystem default + extension discovery
# (no scan root). Callers that need ``list()`` over a specific tree build their own
# via :func:`build_spec_source_registry`.
spec_source_registry: PluggableRegistry[SpecSource] = build_spec_source_registry()


__all__ = [
    "EXTENSION_HOOK",
    "SEAM",
    "FilesystemSpecSource",
    "RawSpec",
    "SpecSource",
    "build_spec_source_registry",
    "decode_raw_spec",
    "spec_source_registry",
]
