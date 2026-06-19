"""A small, pluggable secret store for provider credentials.

This is the storage layer behind ``keychain:<name>`` secret references in
``~/.omnigent/config.yaml`` (see
:func:`omnigent.onboarding.provider_config.resolve_secret`). The runtime reads a
secret back when a family's ``api_key_ref`` is ``keychain:<name>``.

**Backends form a chain (BDP-2303).** Each backend implements
:class:`SecretBackend`. The store consults, in order:

1. **Extension-contributed backends** (e.g. an Infisical backend from the
   ``bytedesk_omnigent`` package, discovered via the ``omnigent.extensions``
   seam) — but only the ones reporting :meth:`SecretBackend.available`. This is
   the generic, upstream-contributable hook; core names no specific provider.
2. **The local backend** (:class:`LocalBackend`) — the historical
   keyring-then-``0600``-file behaviour, always present as the fallback.

Reads (:func:`load_secret`) fall through the chain and return the first match.
Writes (:func:`store_secret` / :func:`delete_secret`) go to the **primary**
(first available) backend. ``OMNIGENT_SECRET_BACKEND`` pins a specific backend by
name (``"infisical"`` / ``"keyring"`` / ``"file"`` / ``"local"``), bypassing
selection.

The local backend keeps two sub-stores, picked transparently:

- **OS keychain** (macOS Keychain, GNOME Keyring, Windows Credential Locker) via
  the ``keyring`` package. Used unless ``OMNIGENT_DISABLE_KEYRING`` is set or a
  keyring call raises :class:`keyring.errors.KeyringError`.
- **A ``0600`` JSON file** at ``<config_home>/secrets.json``. The complete,
  self-contained fallback for headless / locked-keyring / CI hosts.

This module must not import :mod:`omnigent.onboarding.provider_config` — that
module imports *this* one lazily inside ``resolve_secret`` to avoid a circular
import. The extension seam is likewise imported lazily.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Protocol, runtime_checkable

import keyring
import keyring.errors

logger = logging.getLogger(__name__)

# The subset of keyring exceptions that mean "this backend can't serve the
# request" (locked / headless / no backend) — we fall back to the file backend
# rather than crash.
_KEYRING_ERRORS: tuple[type[Exception], ...] = (keyring.errors.KeyringError,)

# Service name under which secrets are stored in the OS keychain. A single
# service groups all omnigent secrets; the per-secret ``name`` is the keychain
# "username".
_KEYRING_SERVICE = "omnigent"

# Env var that forces the file sub-store even when ``keyring`` is importable.
_DISABLE_KEYRING_ENV = "OMNIGENT_DISABLE_KEYRING"

# Env var that pins the active backend by name, bypassing chain selection.
_BACKEND_OVERRIDE_ENV = "OMNIGENT_SECRET_BACKEND"

# Backend identifiers returned by :func:`active_backend`.
KEYRING_BACKEND = "keyring"
FILE_BACKEND = "file"
LOCAL_BACKEND = "local"


@runtime_checkable
class SecretBackend(Protocol):
    """A store for ``keychain:<name>`` secrets (BDP-2303).

    Extensions contribute backends through ``BytedeskExtension.secret_backends()``;
    the local keyring/file store implements this same protocol.
    """

    name: str

    def available(self) -> bool:
        """Whether this backend is usable right now (creds present, reachable).

        An unavailable backend is skipped during selection so a misconfigured
        remote store never shadows the local fallback.
        """
        ...

    def load(self, name: str) -> str | None:
        """Return the secret stored under *name*, or ``None`` if absent."""
        ...

    def store(self, name: str, value: str) -> None:
        """Store *value* under *name*."""
        ...

    def delete(self, name: str) -> None:
        """Delete the secret under *name* (no-op if absent)."""
        ...


# ── local backend (historical keyring-then-file behaviour) ───────────────────


def _keyring_disabled() -> bool:
    return os.environ.get(_DISABLE_KEYRING_ENV, "").strip().lower() in ("true", "1", "yes")


def _use_keyring() -> bool:
    return not _keyring_disabled()


def _config_home() -> str:
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    if config_home:
        return config_home
    return os.path.join(os.path.expanduser("~"), ".omnigent")


def _secrets_path() -> str:
    return os.path.join(_config_home(), "secrets.json")


def _read_secrets_file() -> dict[str, str]:
    path = _secrets_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data: dict[str, str] = json.load(f)
    return data


def _write_secrets_file(secrets: dict[str, str]) -> None:
    path = _secrets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(secrets, f, indent=2)
    os.chmod(path, 0o600)


class LocalBackend:
    """The on-host keyring/file secret store — always available.

    Tries the OS keychain when enabled; on a :class:`keyring.errors.KeyringError`
    (locked / headless / no backend) it transparently falls back to the ``0600``
    JSON file so a secret is never lost to a keyring hiccup. This is exactly the
    behaviour the module shipped before the backend chain (BDP-2303).
    """

    @property
    def name(self) -> str:
        # Reflects which sub-store is in effect, for diagnostics.
        return KEYRING_BACKEND if _use_keyring() else FILE_BACKEND

    def available(self) -> bool:
        return True

    def load(self, name: str) -> str | None:
        if _use_keyring():
            try:
                return keyring.get_password(_KEYRING_SERVICE, name)
            except _KEYRING_ERRORS:
                pass
        return _read_secrets_file().get(name)

    def store(self, name: str, value: str) -> None:
        if _use_keyring():
            try:
                keyring.set_password(_KEYRING_SERVICE, name, value)
                return
            except _KEYRING_ERRORS:
                pass
        secrets = _read_secrets_file()
        secrets[name] = value
        _write_secrets_file(secrets)

    def delete(self, name: str) -> None:
        if _use_keyring():
            try:
                keyring.delete_password(_KEYRING_SERVICE, name)
                return
            except _KEYRING_ERRORS:
                pass
        secrets = _read_secrets_file()
        if name in secrets:
            del secrets[name]
            _write_secrets_file(secrets)


# ── backend chain + selection ────────────────────────────────────────────────

_chain: list[SecretBackend] | None = None


def _extension_backends() -> list[SecretBackend]:
    """Secret backends contributed by extensions (ADR-0143 seam), or ``[]``.

    Imported lazily and defensively: a missing/broken extension seam must never
    break local secret resolution (CLI runs have no server, headless hosts may
    lack the entry-point metadata).
    """
    try:
        from omnigent.extensions import extension_secret_backends

        return list(extension_secret_backends())
    except Exception:  # noqa: BLE001 — extensions are best-effort
        logger.debug("no extension secret backends available", exc_info=True)
        return []


def _is_available(backend: SecretBackend) -> bool:
    try:
        return bool(backend.available())
    except Exception:  # noqa: BLE001 — a backend that can't even report is unusable
        logger.warning("secret backend %r failed availability check", backend, exc_info=True)
        return False


def _build_chain() -> list[SecretBackend]:
    """Resolve the ordered backend chain: available extension backends, then local.

    An ``OMNIGENT_SECRET_BACKEND`` override pins a single backend by name.
    """
    local = LocalBackend()
    available_ext = [b for b in _extension_backends() if _is_available(b)]

    override = os.environ.get(_BACKEND_OVERRIDE_ENV, "").strip().lower()
    if override:
        if override in (FILE_BACKEND, KEYRING_BACKEND, LOCAL_BACKEND):
            return [local]
        for b in available_ext:
            if b.name.lower() == override:
                return [b, local]
        logger.warning(
            "OMNIGENT_SECRET_BACKEND=%r not available; using local store", override
        )
        return [local]

    return [*available_ext, local]


def _backends() -> list[SecretBackend]:
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


def reset_backends() -> None:
    """Clear the cached backend chain so the next call re-selects (tests / config reload)."""
    global _chain
    _chain = None


def active_backend() -> str:
    """Return the name of the primary (write) backend, for diagnostics."""
    return _backends()[0].name


# ── facade (stable public API) ───────────────────────────────────────────────


def store_secret(name: str, value: str) -> None:
    """Store *value* under *name* in the primary backend."""
    _backends()[0].store(name, value)


def load_secret(name: str) -> str | None:
    """Return the secret stored under *name*, trying each backend in order.

    Returns the first non-``None`` match; ``None`` if no backend has it.
    """
    for backend in _backends():
        try:
            value = backend.load(name)
        except Exception:  # noqa: BLE001 — a flaky backend must not block the chain
            logger.warning(
                "secret backend %r load failed for %r", backend.name, name, exc_info=True
            )
            continue
        if value is not None:
            return value
    return None


def delete_secret(name: str) -> None:
    """Delete the secret under *name* from the primary backend."""
    _backends()[0].delete(name)
