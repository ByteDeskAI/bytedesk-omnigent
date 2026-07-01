import { KeyRoundIcon, Trash2Icon } from "lucide-react";
import { formatEpoch } from "@/components/members";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AccountListEntry } from "@/lib/accountsApi";

export interface MembersTableProps {
  users: AccountListEntry[];
  meId: string | null;
  pendingAction: boolean;
  onResetPassword: (userId: string) => void;
  setDeleteCandidate: (userId: string | null) => void;
}

export function MembersTable({
  users,
  meId,
  pendingAction,
  onResetPassword,
  setDeleteCandidate,
}: MembersTableProps) {
  return (
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
  );
}