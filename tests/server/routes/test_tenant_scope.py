"""Cross-tenant isolation guard (BDP-2395, ADR-0149).

Pins ``_enforce_tenant_scope``: a tenant-scoped principal is denied another
tenant's session (NOT_FOUND, not FORBIDDEN, so existence isn't leaked), while
single-org / legacy (``None`` tenant on either side) is exempt.
"""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import _enforce_tenant_scope


def _conv(tenant_id: str | None) -> Conversation:
    return Conversation(
        id="c1", created_at=0, updated_at=0, root_conversation_id="c1", tenant_id=tenant_id
    )


def test_same_tenant_is_allowed() -> None:
    _enforce_tenant_scope("tenant-a", _conv("tenant-a"))  # no raise


def test_none_caller_tenant_is_exempt() -> None:
    _enforce_tenant_scope(None, _conv("tenant-a"))  # single-org caller — unchanged


def test_none_session_tenant_is_exempt() -> None:
    _enforce_tenant_scope("tenant-b", _conv(None))  # legacy/local session — unchanged


def test_cross_tenant_is_denied_as_not_found() -> None:
    with pytest.raises(OmnigentError) as exc:
        _enforce_tenant_scope("tenant-b", _conv("tenant-a"))
    assert exc.value.code == ErrorCode.NOT_FOUND


# ── Session-list owner-ACL relaxation for admins (BDP-2438) ──────────


def test_non_admin_is_scoped_to_own_sessions() -> None:
    """A non-admin's list is filtered to sessions they own (``accessible_by``)."""
    from omnigent.server.routes.sessions import _session_list_accessible_by

    assert _session_list_accessible_by("alice", is_admin=False) == "alice"


def test_admin_owner_acl_is_relaxed() -> None:
    """An admin gets ``None`` (no owner filter) — tenant filter still applies."""
    from omnigent.server.routes.sessions import _session_list_accessible_by

    assert _session_list_accessible_by("bob", is_admin=True) is None


def test_auth_disabled_is_unchanged() -> None:
    """``None`` caller (auth disabled) already means 'no filter' — unchanged."""
    from omnigent.server.routes.sessions import _session_list_accessible_by

    assert _session_list_accessible_by(None, is_admin=False) is None
