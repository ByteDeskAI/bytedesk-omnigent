import { type FormEvent, useState } from "react";
import { UserPlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  useGrantPermission,
  usePermissions,
  useRevokePermission,
} from "@/hooks/usePermissions";
import { AddUserField } from "./AddUserField";
import { CopyLinkButton, GrantRow } from "./GrantRow";

const PUBLIC_USER = "__public__";

export interface PermissionsModalProps {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function PermissionsModal({ sessionId, open, onOpenChange }: PermissionsModalProps) {
  const { data: permissions, isLoading } = usePermissions(open ? sessionId : null);
  const grant = useGrantPermission(sessionId);
  const revoke = useRevokePermission(sessionId);

  const [newUserId, setNewUserId] = useState("");
  const [newLevel, setNewLevel] = useState("1");
  const [error, setError] = useState<string | null>(null);

  const userGrants = (permissions ?? []).filter((p) => p.user_id !== PUBLIC_USER);
  const publicGrant = (permissions ?? []).find((p) => p.user_id === PUBLIC_USER);
  const isPublic = !!publicGrant;

  function handleGrant(e: FormEvent) {
    e.preventDefault();
    const trimmed = newUserId.trim();
    if (!trimmed) return;
    setError(null);
    grant.mutate(
      { userId: trimmed, level: parseInt(newLevel, 10) },
      {
        onSuccess: () => {
          setNewUserId("");
          setNewLevel("1");
        },
        onError: (err) => setError(err.message),
      },
    );
  }

  function handleRevoke(userId: string) {
    setError(null);
    revoke.mutate(userId, {
      onError: (err) => setError(err.message),
    });
  }

  function handleChangeLevel(userId: string, level: number) {
    setError(null);
    grant.mutate({ userId, level }, { onError: (err) => setError(err.message) });
  }

  function handlePublicToggle(checked: boolean) {
    setError(null);
    if (checked) {
      grant.mutate({ userId: PUBLIC_USER, level: 1 }, { onError: (err) => setError(err.message) });
    } else {
      revoke.mutate(PUBLIC_USER, {
        onError: (err) => setError(err.message),
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">Share this session</DialogTitle>
          <DialogDescription>
            Invite others to view or collaborate on this session.
          </DialogDescription>
        </DialogHeader>

        {/* Public toggle */}
        <div className="flex items-center justify-between rounded-lg border px-3 py-2">
          <div>
            <p className="text-sm font-medium">Public access</p>
            <p className="text-xs text-muted-foreground">Anyone can view this session</p>
          </div>
          <Switch
            checked={isPublic}
            onCheckedChange={handlePublicToggle}
            disabled={grant.isPending || revoke.isPending}
          />
        </div>

        {/* Current grants */}
        <div>
          {isLoading ? (
            <p className="text-sm text-muted-foreground py-2">Loading…</p>
          ) : userGrants.length === 0 ? (
            <p className="text-sm text-muted-foreground py-2">No grants yet.</p>
          ) : (
            <>
              {/* Column headers */}
              <div className="flex items-center gap-2 px-2 pb-0.5">
                <span className="flex-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Name
                </span>
                <span className="w-28 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Permission
                </span>
                <span className="size-7 shrink-0" aria-hidden="true" />
              </div>
              <div className="max-h-48 overflow-y-auto">
                {userGrants.map((p) => (
                  <GrantRow
                    key={p.user_id}
                    permission={p}
                    onRevoke={handleRevoke}
                    onChangeLevel={handleChangeLevel}
                    busy={grant.isPending || revoke.isPending}
                  />
                ))}
              </div>
            </>
          )}
        </div>

        {/* Add grant form */}
        <form onSubmit={handleGrant} className="flex items-end gap-2">
          <div className="flex-1">
            <label htmlFor="perm-user" className="text-xs font-medium text-muted-foreground">
              User ID
            </label>
            <AddUserField value={newUserId} onChange={setNewUserId} />
          </div>
          <div>
            <label htmlFor="perm-level" className="text-xs font-medium text-muted-foreground">
              Level
            </label>
            <Select value={newLevel} onValueChange={setNewLevel}>
              <SelectTrigger className="mt-1 w-24">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1">Read</SelectItem>
                <SelectItem value="2">Edit</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" size="sm" disabled={!newUserId.trim() || grant.isPending}>
            <UserPlusIcon className="mr-1 size-3.5" />
            Grant
          </Button>
        </form>

        {error && <p className="text-xs text-destructive">{error}</p>}

        <DialogFooter className="flex-row justify-between sm:justify-between">
          <CopyLinkButton sessionId={sessionId} />
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
