"""Tests for the combined permission helper in ``_auth_helpers``.

Focused on :func:`require_access_and_level`, which folds ``require_access``
and ``get_permission_level`` into a single resolution. The behaviour it must
preserve is the 403-vs-404 distinction, the admin bypass, sub-agent parent
delegation, and the user-vs-public displayed-level asymmetry — all exercised
here against real SQLite-backed stores (no mocks) so the resolution matches
production exactly.
"""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from unittest.mock import MagicMock

from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_LOCAL,
    RESERVED_USER_PUBLIC,
)
from omnigent.server.routes._auth_helpers import (
    _require_access_sync,
    attribution_user,
    get_permission_level,
    get_session_owner_id,
    get_user_id,
    require_access,
    require_access_and_level,
    require_user,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

ALICE = "alice@test.com"
BOB = "bob@test.com"


@pytest.fixture()
def perm_store(db_uri: str) -> SqlAlchemyPermissionStore:
    """A fresh permission store on the per-test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest.
    :returns: A ready :class:`SqlAlchemyPermissionStore`.
    """
    return SqlAlchemyPermissionStore(db_uri)


@pytest.fixture()
def conv_store(db_uri: str) -> SqlAlchemyConversationStore:
    """A fresh conversation store on the per-test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest.
    :returns: A ready :class:`SqlAlchemyConversationStore`.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.mark.asyncio
async def test_owner_gets_level_and_conversation(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """An owner is allowed and the fetched conversation is returned for reuse.

    The returned ``conversation`` is what lets the snapshot skip its own
    ``get_conversation`` read — assert it is the same session, not ``None``.
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conv.id, LEVEL_OWNER)

    access = await require_access_and_level(ALICE, conv.id, LEVEL_READ, perm_store, conv_store)

    assert access.level == LEVEL_OWNER, (
        f"owner must report level {LEVEL_OWNER}, got {access.level}"
    )
    assert access.conversation is not None, (
        "the conversation must be returned so the snapshot can reuse it"
    )
    assert access.conversation.id == conv.id


@pytest.mark.asyncio
async def test_no_access_raises_404_not_403(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """Bob, with no grant on Alice's session, gets 404 — not a 403 oracle.

    Returning 403 would confirm the session exists; 404 keeps existence
    hidden from a user with no access at all.
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.ensure_user(BOB)
    perm_store.grant(ALICE, conv.id, LEVEL_OWNER)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(BOB, conv.id, LEVEL_READ, perm_store, conv_store)

    assert exc.value.code == ErrorCode.NOT_FOUND, f"no-access must be 404, got {exc.value.code}"


@pytest.mark.asyncio
async def test_insufficient_level_raises_403(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """A read-only user asking for edit gets 403 (has access, not enough)."""
    conv = conv_store.create_conversation()
    perm_store.ensure_user(BOB)
    perm_store.grant(BOB, conv.id, LEVEL_READ)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(BOB, conv.id, LEVEL_EDIT, perm_store, conv_store)

    assert exc.value.code == ErrorCode.FORBIDDEN, (
        f"insufficient level must be 403, got {exc.value.code}"
    )


@pytest.mark.asyncio
async def test_admin_allowed_and_bypasses_conversation_fetch(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """Admin is allowed at OWNER level and does not fetch the conversation.

    Mirrors ``check_session_access``'s admin short-circuit: ``conversation``
    is ``None`` (no lookup happened), and the level is ``LEVEL_OWNER``.
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user("root@test.com", is_admin=True)

    access = await require_access_and_level(
        "root@test.com", conv.id, LEVEL_OWNER, perm_store, conv_store
    )

    assert access.level == LEVEL_OWNER
    assert access.conversation is None, (
        "admin path must not fetch the conversation (it bypasses the lookup)"
    )


@pytest.mark.asyncio
async def test_public_grant_allows_but_level_reports_user_grant(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """Access via a higher public grant; displayed level is the user's own.

    The regression guard for the combined helper: a low user grant plus a
    higher ``__public__`` grant must still report the user's own level
    (matching ``get_permission_level``) while granting access via the
    public grant (matching ``check_access``).
    """
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.ensure_user(RESERVED_USER_PUBLIC)
    perm_store.grant(ALICE, conv.id, LEVEL_READ)  # user: read
    perm_store.grant(RESERVED_USER_PUBLIC, conv.id, LEVEL_OWNER)  # public: owner

    access = await require_access_and_level(ALICE, conv.id, LEVEL_EDIT, perm_store, conv_store)

    # Allowed (no raise) because the public grant satisfies EDIT ...
    assert access.conversation is not None
    assert access.conversation.id == conv.id, "must reuse the asked-for session"
    # ... but the displayed level is Alice's own read grant, unchanged.
    assert access.level == LEVEL_READ, (
        f"displayed level must be the user's own read grant, got {access.level}"
    )


@pytest.mark.asyncio
async def test_sub_agent_delegates_access_to_parent(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """A sub-agent session inherits access from its parent's grant.

    The user has a grant on the parent only; access to the sub-agent must
    be allowed via parent delegation, while the displayed level (a direct
    lookup on the sub-agent) stays ``None`` — unchanged from today.
    """
    parent = conv_store.create_conversation()
    child = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        sub_agent_name="summarizer",
    )
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, parent.id, LEVEL_OWNER)

    access = await require_access_and_level(ALICE, child.id, LEVEL_READ, perm_store, conv_store)

    assert access.conversation is not None
    assert access.conversation.id == child.id, "snapshot reuses the sub-agent row"
    # Displayed level is the direct grant on the sub-agent (none granted).
    assert access.level is None, "displayed level is the sub-agent's own grant, which is None here"


@pytest.mark.asyncio
async def test_permissions_disabled_returns_empty_access(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """With no permission store, the helper is a no-op (level None, no fetch)."""
    access = await require_access_and_level(None, "conv_whatever", LEVEL_READ, None, conv_store)

    assert access.level is None
    assert access.conversation is None


@pytest.mark.asyncio
async def test_unauthenticated_with_store_raises_401(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """An anonymous caller against an enabled store is rejected with 401."""
    conv = conv_store.create_conversation()

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(None, conv.id, LEVEL_READ, perm_store, conv_store)

    assert exc.value.code == ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_missing_conversation_raises_404(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    """A non-admin asking for a conversation that does not exist gets 404."""
    perm_store.ensure_user(ALICE)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(
            ALICE, "conv_does_not_exist", LEVEL_READ, perm_store, conv_store
        )

    assert exc.value.code == ErrorCode.NOT_FOUND


def test_get_user_id_returns_none_without_auth_provider() -> None:
    request = MagicMock()
    assert get_user_id(request, None) is None


def test_get_user_id_delegates_to_auth_provider() -> None:
    request = MagicMock()
    provider = MagicMock()
    provider.get_user_id.return_value = ALICE
    assert get_user_id(request, provider) == ALICE
    provider.get_user_id.assert_called_once_with(request)


def test_attribution_user_drops_local_sentinel() -> None:
    assert attribution_user(RESERVED_USER_LOCAL) is None
    assert attribution_user(ALICE) == ALICE
    assert attribution_user(None) is None


def test_require_user_raises_401_when_identity_missing() -> None:
    request = MagicMock()
    provider = MagicMock()
    provider.get_user_id.return_value = None
    with pytest.raises(OmnigentError) as exc:
        require_user(request, provider)
    assert exc.value.code == ErrorCode.UNAUTHORIZED


def test_require_user_returns_none_when_auth_disabled() -> None:
    assert require_user(MagicMock(), None) is None


def test_require_user_returns_identity_when_authenticated() -> None:
    request = MagicMock()
    provider = MagicMock()
    provider.get_user_id.return_value = ALICE
    assert require_user(request, provider) == ALICE


@pytest.mark.asyncio
async def test_require_access_async_wrapper_enforces_permissions(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    conv = conv_store.create_conversation()
    perm_store.ensure_user(BOB)
    perm_store.grant(BOB, conv.id, LEVEL_READ)

    await require_access(BOB, conv.id, LEVEL_READ, perm_store, conv_store)

    with pytest.raises(OmnigentError) as forbidden:
        await require_access(BOB, conv.id, LEVEL_EDIT, perm_store, conv_store)
    assert forbidden.value.code == ErrorCode.FORBIDDEN

    with pytest.raises(OmnigentError) as missing:
        await require_access(ALICE, conv.id, LEVEL_READ, perm_store, conv_store)
    assert missing.value.code == ErrorCode.NOT_FOUND


def test_require_access_sync_skips_when_permissions_disabled(
    conv_store: SqlAlchemyConversationStore,
) -> None:
    _require_access_sync(None, "conv_x", LEVEL_READ, None, conv_store)


def test_require_access_sync_raises_401_for_anonymous_user(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    with pytest.raises(OmnigentError) as exc:
        _require_access_sync(None, "conv_x", LEVEL_READ, perm_store, conv_store)
    assert exc.value.code == ErrorCode.UNAUTHORIZED


@pytest.mark.asyncio
async def test_get_permission_level_returns_direct_grant(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.grant(ALICE, conv.id, LEVEL_EDIT)
    level = await get_permission_level(ALICE, conv.id, perm_store)
    assert level == LEVEL_EDIT
    assert await get_permission_level(None, conv.id, perm_store) is None
    assert await get_permission_level(ALICE, conv.id, None) is None


@pytest.mark.asyncio
async def test_sub_agent_insufficient_parent_level_raises_403(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    parent = conv_store.create_conversation()
    child = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        sub_agent_name="worker",
    )
    perm_store.ensure_user(BOB)
    perm_store.grant(BOB, parent.id, LEVEL_READ)

    with pytest.raises(OmnigentError) as exc:
        await require_access_and_level(BOB, child.id, LEVEL_EDIT, perm_store, conv_store)
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_get_session_owner_id_finds_owner_grant(
    perm_store: SqlAlchemyPermissionStore, conv_store: SqlAlchemyConversationStore
) -> None:
    conv = conv_store.create_conversation()
    perm_store.ensure_user(ALICE)
    perm_store.ensure_user(BOB)
    perm_store.grant(ALICE, conv.id, LEVEL_READ)
    perm_store.grant(BOB, conv.id, LEVEL_OWNER)

    assert get_session_owner_id(conv.id, perm_store) == BOB
    assert get_session_owner_id(conv.id, None) is None
    assert get_session_owner_id("conv_missing", perm_store) is None
