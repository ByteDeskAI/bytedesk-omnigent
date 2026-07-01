/**
 * Admin members management page (``/members``).
 *
 * Lists every account on the server and lets admins:
 *
 * - Mint a single-use invite URL to share out-of-band.
 * - Reset a member's password (server generates a fresh random
 *   one and returns it exactly once — admin DMs it to the user).
 * - Remove a member entirely (cascades grants via the existing
 *   ``ON DELETE CASCADE`` on session_permissions).
 *
 * Gated on the client by an early "not an admin → render nothing"
 * check AND on the server by the route handlers themselves —
 * client-side gating is just UX so non-admins don't see useless
 * buttons; the server is what actually enforces.
 *
 * The "reset password" and "create invite" flows display the
 * sensitive value EXACTLY ONCE in a modal with a Copy button.
 * There is intentionally no way to retrieve them later — admins
 * who lose them just reset again. This matches the field
 * convention (GitLab, n8n, Coolify all do the same) and avoids
 * accidentally caching secrets in a list endpoint.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "@/lib/routing";
import {
  type AccountListEntry,
  type InviteCreated,
  type PasswordReset,
  createInvite,
  deleteUser,
  getMe,
  listUsers,
  resetUserPassword,
} from "@/lib/accountsApi";
import { MembersPageShell } from "./organisms/MembersPageShell";

export function MembersPage() {
  const navigate = useNavigate();
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const [meId, setMeId] = useState<string | null>(null);
  const [users, setUsers] = useState<AccountListEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [inviteResult, setInviteResult] = useState<InviteCreated | null>(null);
  const [showCreateInvite, setShowCreateInvite] = useState(false);
  const [inviteAsAdmin, setInviteAsAdmin] = useState(false);
  const [resetResult, setResetResult] = useState<PasswordReset | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const list = await listUsers();
    if (list === null) {
      setLoadError(
        "Could not load members. You may not have admin permission, or the server is unreachable.",
      );
      setUsers([]);
      return;
    }
    setLoadError(null);
    setUsers(list);
  }, []);

  useEffect(() => {
    void (async () => {
      const me = await getMe();
      if (me === null) {
        navigate("/login", { replace: true });
        return;
      }
      setMeId(me.id);
      setMeIsAdmin(me.is_admin);
      if (me.is_admin) await refresh();
    })();
  }, [navigate, refresh]);

  async function onCreateInvite() {
    setPendingAction(true);
    setActionError(null);
    const result = await createInvite(inviteAsAdmin);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setShowCreateInvite(false);
    setInviteResult(result);
    setInviteAsAdmin(false);
  }

  async function onConfirmDelete() {
    if (deleteCandidate === null) return;
    setPendingAction(true);
    setActionError(null);
    const result = await deleteUser(deleteCandidate);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setDeleteCandidate(null);
    await refresh();
  }

  async function onResetPassword(userId: string) {
    setPendingAction(true);
    setActionError(null);
    const result = await resetUserPassword(userId);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setResetResult(result);
  }

  return (
    <MembersPageShell
      meIsAdmin={meIsAdmin}
      meId={meId}
      users={users}
      loadError={loadError}
      inviteResult={inviteResult}
      setInviteResult={setInviteResult}
      showCreateInvite={showCreateInvite}
      setShowCreateInvite={setShowCreateInvite}
      inviteAsAdmin={inviteAsAdmin}
      setInviteAsAdmin={setInviteAsAdmin}
      resetResult={resetResult}
      setResetResult={setResetResult}
      deleteCandidate={deleteCandidate}
      setDeleteCandidate={setDeleteCandidate}
      pendingAction={pendingAction}
      actionError={actionError}
      setActionError={setActionError}
      onRefresh={() => void refresh()}
      onCreateInvite={() => void onCreateInvite()}
      onConfirmDelete={() => void onConfirmDelete()}
      onResetPassword={(userId) => void onResetPassword(userId)}
    />
  );
}