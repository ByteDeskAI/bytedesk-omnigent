import { RefreshCwIcon, UserPlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { AccountListEntry, InviteCreated, PasswordReset } from "@/lib/accountsApi";
import {
  MembersAccessDenied,
  MembersDialogs,
  MembersLoadingState,
  MembersTable,
} from "./members";

export interface MembersPageShellProps {
  meIsAdmin: boolean | null;
  meId: string | null;
  users: AccountListEntry[] | null;
  loadError: string | null;
  inviteResult: InviteCreated | null;
  setInviteResult: (result: InviteCreated | null) => void;
  showCreateInvite: boolean;
  setShowCreateInvite: (open: boolean) => void;
  inviteAsAdmin: boolean;
  setInviteAsAdmin: (value: boolean) => void;
  resetResult: PasswordReset | null;
  setResetResult: (result: PasswordReset | null) => void;
  deleteCandidate: string | null;
  setDeleteCandidate: (userId: string | null) => void;
  pendingAction: boolean;
  actionError: string | null;
  setActionError: (error: string | null) => void;
  onRefresh: () => void;
  onCreateInvite: () => void;
  onConfirmDelete: () => void;
  onResetPassword: (userId: string) => void;
}

export function MembersPageShell({
  meIsAdmin,
  meId,
  users,
  loadError,
  inviteResult,
  setInviteResult,
  showCreateInvite,
  setShowCreateInvite,
  inviteAsAdmin,
  setInviteAsAdmin,
  resetResult,
  setResetResult,
  deleteCandidate,
  setDeleteCandidate,
  pendingAction,
  actionError,
  setActionError,
  onRefresh,
  onCreateInvite,
  onConfirmDelete,
  onResetPassword,
}: MembersPageShellProps) {
  if (meIsAdmin === null) {
    return <MembersLoadingState />;
  }

  if (meIsAdmin === false) {
    return <MembersAccessDenied />;
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8 pt-14">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Members</h1>
        <Button onClick={() => setShowCreateInvite(true)}>
          <UserPlusIcon /> Invite member
        </Button>
      </div>

      {loadError !== null && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {loadError}
        </div>
      )}

      {users !== null && users.length > 0 && (
        <MembersTable
          users={users}
          meId={meId}
          pendingAction={pendingAction}
          onResetPassword={onResetPassword}
          setDeleteCandidate={setDeleteCandidate}
        />
      )}

      {users !== null && users.length === 0 && (
        <p className="text-sm text-muted-foreground">No members yet.</p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={() => void onRefresh()}>
          <RefreshCwIcon /> Refresh
        </Button>
      </div>

      <MembersDialogs
        showCreateInvite={showCreateInvite}
        setShowCreateInvite={setShowCreateInvite}
        inviteAsAdmin={inviteAsAdmin}
        setInviteAsAdmin={setInviteAsAdmin}
        inviteResult={inviteResult}
        setInviteResult={setInviteResult}
        resetResult={resetResult}
        setResetResult={setResetResult}
        deleteCandidate={deleteCandidate}
        setDeleteCandidate={setDeleteCandidate}
        pendingAction={pendingAction}
        actionError={actionError}
        setActionError={setActionError}
        onCreateInvite={onCreateInvite}
        onConfirmDelete={onConfirmDelete}
      />
    </div>
  );
}