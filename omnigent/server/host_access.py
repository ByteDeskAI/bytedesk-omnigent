"""Host visibility-scope authorization (ADR-0151).

A host is code-execution capability. Upstream omnigent scopes every host to a
single owner (a public multi-tenant default). ByteDesk runs omnigent as a
single-org system where org compute should be *shared*, so visibility is a
configurable scope (`system.host.visibility_scope`) read here at request time:

- ``org-shared``: any authenticated member sees/uses any EXTERNAL host.
- ``private``: per-owner isolation (the upstream behavior).

MANAGED/sandbox hosts (``sandbox_provider`` set) are NEVER shared cross-owner in
any scope — they are ephemeral per-session launch targets whose hijack boundary
is enforced by their launch token. The scope only governs external,
user-connected hosts.
"""

from __future__ import annotations

from omnigent.stores.host_store import Host

# Per-owner isolation is the safe default: it's the upstream behavior, and it's
# what a non-bytedesk deployment (no descriptor registered) — or a failed config
# read — falls back to, so visibility never *widens* by accident.
_DEFAULT_SCOPE = "private"


def host_visibility_scope() -> str:
    """The configured host visibility scope, defaulting to ``private``.

    Reads ``system.host.visibility_scope`` from the config control plane
    (ADR-0150). Absent descriptor or any read error → ``private`` (fail to the
    more restrictive scope; a host pool must never widen on a config glitch).

    :returns: ``"org-shared"`` or ``"private"``.
    """
    try:
        from omnigent.config import ConfigCtx, build_registry

        value = build_registry().read("system.host.visibility_scope", ConfigCtx()).value
    except Exception:  # noqa: BLE001 - fail-safe: any read error → restrictive default
        return _DEFAULT_SCOPE
    return str(value) if value else _DEFAULT_SCOPE


def can_access_host(host: Host, user_id: str | None, *, scope: str | None = None) -> bool:
    """Whether ``user_id`` may see/use ``host`` under the visibility scope.

    :param host: The host record.
    :param user_id: Authenticated caller, or ``None`` when auth is disabled.
    :param scope: The visibility scope; read from config when ``None`` (pass it
        explicitly when filtering a list so the config is read once).
    :returns: ``True`` when access is allowed.
    """
    if user_id is None:
        return True  # auth disabled → single-user/local runtime
    if host.owner == user_id:
        return True
    if host.sandbox_provider is not None:
        return False  # managed/sandbox hosts are never shared cross-owner
    if scope is None:
        scope = host_visibility_scope()
    return scope == "org-shared"
