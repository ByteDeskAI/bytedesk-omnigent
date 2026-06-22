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
