import { KeyRoundIcon, RefreshCwIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { CopyableValue, formatEpoch, formatTtl, rebaseUrl } from "@/components/members";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { AccountListEntry, InviteCreated, PasswordReset } from "@/lib/accountsApi";

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
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (meIsAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Members</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to manage members.
        </p>
      </div>
    );
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
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Username</th>
                <th className="px-3 py-2 font-medium">Role</th>
                <th className="px-3 py-2 font-medium">Last login</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="mc-stagger-children">
              {users.map((u) => (
                <tr key={u.id} className="border-t border-border">
                  <td className="px-3 py-2 align-middle">
                    <span className="font-medium">{u.id}</span>
                    {u.id === meId && (
                      <span className="ml-2 text-xs text-muted-foreground">(you)</span>
                    )}
                    {!u.has_password && (
                      <Badge variant="outline" className="ml-2">
                        External
                      </Badge>
                    )}
                  </td>
                  <td className="px-3 py-2 align-middle">
                    {u.is_admin ? <Badge>Admin</Badge> : <Badge variant="secondary">Member</Badge>}
                  </td>
                  <td className="px-3 py-2 align-middle text-muted-foreground">
                    {formatEpoch(u.last_login_at)}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="xs"
                        title="Reset password"
                        onClick={() => void onResetPassword(u.id)}
                        disabled={pendingAction || !u.has_password}
                      >
                        <KeyRoundIcon /> Reset
                      </Button>
                      <Button
                        variant="ghost"
                        size="xs"
                        title="Remove user"
                        onClick={() => setDeleteCandidate(u.id)}
                        disabled={pendingAction || u.id === meId}
                      >
                        <Trash2Icon /> Remove
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {users !== null && users.length === 0 && (
        <p className="text-sm text-muted-foreground">No members yet.</p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={() => void onRefresh()}>
          <RefreshCwIcon /> Refresh
        </Button>
      </div>

      <Dialog
        open={showCreateInvite}
        onOpenChange={(open) => {
          if (pendingAction) return;
          setShowCreateInvite(open);
          if (!open) setActionError(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Invite a member</DialogTitle>
            <DialogDescription>
              A single-use invite URL will be created. Share it with the person you want to add.
              They'll choose their own username and password when they redeem it.
            </DialogDescription>
          </DialogHeader>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={inviteAsAdmin}
              onChange={(e) => setInviteAsAdmin(e.target.checked)}
              disabled={pendingAction}
            />
            Grant admin privileges
          </label>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setShowCreateInvite(false)}
              disabled={pendingAction}
            >
              Cancel
            </Button>
            <Button onClick={() => void onCreateInvite()} disabled={pendingAction}>
              {pendingAction ? "Creating…" : "Create invite"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={inviteResult !== null}
        onOpenChange={(open) => {
          if (!open) setInviteResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Invite URL</DialogTitle>
            <DialogDescription>
              Send this URL to the new member. It expires in {formatTtl(inviteResult?.expires_at)}{" "}
              and is single-use — once they redeem it, it can't be used again. This URL is shown
              only once.
            </DialogDescription>
          </DialogHeader>
          {inviteResult !== null && <CopyableValue value={rebaseUrl(inviteResult.register_url)} />}
          <DialogFooter>
            <Button onClick={() => setInviteResult(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={resetResult !== null}
        onOpenChange={(open) => {
          if (!open) setResetResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New password for {resetResult?.id}</DialogTitle>
            <DialogDescription>
              Send this password to the user out-of-band (e.g. Slack DM). It is shown only once.
            </DialogDescription>
          </DialogHeader>
          {resetResult !== null && <CopyableValue value={resetResult.new_password} />}
          <DialogFooter>
            <Button onClick={() => setResetResult(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deleteCandidate !== null}
        onOpenChange={(open) => {
          if (pendingAction) return;
          if (!open) {
            setDeleteCandidate(null);
            setActionError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {deleteCandidate}?</DialogTitle>
            <DialogDescription>
              This deletes the user account and revokes all their session permissions. Sessions they
              own become inaccessible unless another user has manage rights on them. This action
              cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setDeleteCandidate(null)}
              disabled={pendingAction}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => void onConfirmDelete()}
              disabled={pendingAction}
            >
              {pendingAction ? "Removing…" : "Remove"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}