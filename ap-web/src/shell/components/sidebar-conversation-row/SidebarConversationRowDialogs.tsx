import { GitBranchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PermissionsModal } from "@/components/PermissionsModal";

export function SidebarConversationRowDialogs({
  conversationId,
  label,
  gitBranch,
  shareOpen,
  setShareOpen,
  deleteOpen,
  setDeleteOpen,
  deleteBranch,
  setDeleteBranch,
  deletePending,
  onConfirmDelete,
  stopOpen,
  setStopOpen,
  stopPending,
  stopError,
  onConfirmStop,
}: {
  conversationId: string;
  label: string;
  gitBranch: string | null;
  shareOpen: boolean;
  setShareOpen: (open: boolean) => void;
  deleteOpen: boolean;
  setDeleteOpen: (open: boolean) => void;
  deleteBranch: boolean;
  setDeleteBranch: (checked: boolean) => void;
  deletePending: boolean;
  onConfirmDelete: () => void;
  stopOpen: boolean;
  setStopOpen: (open: boolean) => void;
  stopPending: boolean;
  stopError: boolean;
  onConfirmStop: () => void;
}) {
  return (
    <>
      <PermissionsModal sessionId={conversationId} open={shareOpen} onOpenChange={setShareOpen} />
      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          if (!open) setDeleteBranch(false);
        }}
      >
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>Delete conversation?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> and all of its history will be
              removed. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {gitBranch !== null && (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <p className="text-xs text-muted-foreground">
                Optionally clean up the git worktree. These actions are{" "}
                <span className="font-semibold text-destructive">irreversible</span>.
              </p>
              <label className="flex cursor-pointer items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  data-testid="delete-branch-checkbox"
                  checked={deleteBranch}
                  onChange={(e) => setDeleteBranch(e.target.checked)}
                  className="mt-0.5 size-4 shrink-0 accent-destructive"
                />
                <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0">
                  Delete local branch{" "}
                  <code className="break-all rounded bg-muted px-1 py-0.5 text-xs">{gitBranch}</code>
                </span>
              </label>
            </div>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={deletePending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={onConfirmDelete}
              disabled={deletePending}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={stopOpen} onOpenChange={setStopOpen}>
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>Stop session?</DialogTitle>
            <DialogDescription>
              This terminates the running session for <span className="font-medium">{label}</span>{" "}
              and stops its runner. The conversation and its history are kept.
            </DialogDescription>
          </DialogHeader>
          {stopError && (
            <p className="text-sm text-destructive" role="alert">
              Couldn't stop the session — it may still be running. Try again in a moment.
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setStopOpen(false)}
              disabled={stopPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={onConfirmStop}
              disabled={stopPending}
            >
              Stop session
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}