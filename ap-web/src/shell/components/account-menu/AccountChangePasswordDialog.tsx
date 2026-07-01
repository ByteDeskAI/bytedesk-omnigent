import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

export interface AccountChangePasswordDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  oldPw: string;
  newPw: string;
  confirmPw: string;
  busy: boolean;
  error: string | null;
  done: boolean;
  onOldPwChange: (value: string) => void;
  onNewPwChange: (value: string) => void;
  onConfirmPwChange: (value: string) => void;
  onSubmit: () => void;
}

export function AccountChangePasswordDialog({
  open,
  onOpenChange,
  oldPw,
  newPw,
  confirmPw,
  busy,
  error,
  done,
  onOldPwChange,
  onNewPwChange,
  onConfirmPwChange,
  onSubmit,
}: AccountChangePasswordDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Change password</DialogTitle>
          <DialogDescription>
            {done
              ? "Your password has been changed."
              : "Enter your current password and choose a new one."}
          </DialogDescription>
        </DialogHeader>

        {!done && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              onSubmit();
            }}
          >
            <Input
              type="password"
              autoComplete="current-password"
              placeholder="Current password"
              value={oldPw}
              onChange={(e) => onOldPwChange(e.target.value)}
              disabled={busy}
              required
            />
            <Input
              type="password"
              autoComplete="new-password"
              placeholder="New password"
              value={newPw}
              onChange={(e) => onNewPwChange(e.target.value)}
              disabled={busy}
              required
            />
            <Input
              type="password"
              autoComplete="new-password"
              placeholder="Confirm new password"
              value={confirmPw}
              onChange={(e) => onConfirmPwChange(e.target.value)}
              disabled={busy}
              required
            />
            {error !== null && (
              <div
                role="alert"
                className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                {error}
              </div>
            )}
            <DialogFooter>
              <Button
                type="submit"
                disabled={busy || oldPw.length === 0 || newPw.length === 0 || confirmPw.length === 0}
              >
                {busy ? "Changing…" : "Change password"}
              </Button>
            </DialogFooter>
          </form>
        )}

        {done && (
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}