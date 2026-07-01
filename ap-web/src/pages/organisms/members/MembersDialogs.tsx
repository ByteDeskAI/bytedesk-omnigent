import { CopyableValue, formatTtl, rebaseUrl } from "@/components/members";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { InviteCreated, PasswordReset } from "@/lib/accountsApi";

export interface MembersDialogsProps {
  showCreateInvite: boolean;
  setShowCreateInvite: (open: boolean) => void;
  inviteAsAdmin: boolean;
  setInviteAsAdmin: (value: boolean) => void;
  inviteResult: InviteCreated | null;
  setInviteResult: (result: InviteCreated | null) => void;
  resetResult: PasswordReset | null;
  setResetResult: (result: PasswordReset | null) => void;
  deleteCandidate: string | null;
  setDeleteCandidate: (userId: string | null) => void;
  pendingAction: boolean;
  actionError: string | null;
  setActionError: (error: string | null) => void;
  onCreateInvite: () => void;
  onConfirmDelete: () => void;
}

export function MembersDialogs({
  showCreateInvite,
  setShowCreateInvite,
  inviteAsAdmin,
  setInviteAsAdmin,
  inviteResult,
  setInviteResult,
  resetResult,
  setResetResult,
  deleteCandidate,
  setDeleteCandidate,
  pendingAction,
  actionError,
  setActionError,
  onCreateInvite,
  onConfirmDelete,
}: MembersDialogsProps) {
  return (
    <>
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
    </>
  );
}