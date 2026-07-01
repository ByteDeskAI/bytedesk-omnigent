import { useCallback, useEffect, useState } from "react";
import { CheckIcon, LinkIcon, Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { type Permission } from "@/hooks/usePermissions";
import { getOmnigentTransformShareLink } from "@/lib/host";
import { useRebasePath } from "@/lib/routing";

function getShareableLink(sessionId: string, rebasePath: (path: string) => string): string {
  const path = rebasePath(`/c/${sessionId}`);
  const transform = getOmnigentTransformShareLink();
  return transform ? transform(path) : `${window.location.origin}${path}`;
}

export function CopyLinkButton({ sessionId }: { sessionId: string }) {
  const [copied, setCopied] = useState(false);
  const rebasePath = useRebasePath();

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);

  const handleCopy = useCallback(() => {
    const url = getShareableLink(sessionId, rebasePath);
    navigator.clipboard.writeText(url).then(
      () => setCopied(true),
      (err) => {
        console.warn("Failed to copy link to clipboard", err);
      },
    );
  }, [sessionId, rebasePath]);

  return (
    <Button variant="ghost" size="sm" onClick={handleCopy} className="gap-1.5 text-primary">
      {copied ? <CheckIcon className="size-3.5" /> : <LinkIcon className="size-3.5" />}
      {copied ? "Copied!" : "Copy link"}
    </Button>
  );
}

export function GrantRow({
  permission,
  onRevoke,
  onChangeLevel,
  busy,
}: {
  permission: Permission;
  onRevoke: (userId: string) => void;
  onChangeLevel: (userId: string, level: number) => void;
  busy: boolean;
}) {
  const isOwner = permission.level === 4;
  // Manage is not grantable from the UI, so a pre-existing manage grant
  // renders as a fixed label rather than a dropdown choice. Unlike the
  // owner row it can still be revoked.
  const isManage = permission.level === 3;

  return (
    <div className="flex items-center gap-2 rounded-md px-2 py-0.5 hover:bg-muted/50">
      <span className="flex-1 truncate text-sm" title={permission.user_id}>
        {permission.user_id}
      </span>
      {isOwner || isManage ? (
        <span className="flex h-8 w-28 items-center px-3 text-sm text-muted-foreground">
          {isOwner ? "Owner" : "Manage"}
        </span>
      ) : (
        <Select
          value={String(permission.level)}
          onValueChange={(v) => onChangeLevel(permission.user_id, parseInt(v, 10))}
          disabled={busy}
        >
          <SelectTrigger
            className="h-8 w-28"
            aria-label={`Permission level for ${permission.user_id}`}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="1">Read</SelectItem>
            <SelectItem value="2">Edit</SelectItem>
          </SelectContent>
        </Select>
      )}
      {isOwner ? (
        <span className="size-7 shrink-0" aria-hidden="true" />
      ) : (
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => onRevoke(permission.user_id)}
          disabled={busy}
          className="shrink-0 text-muted-foreground hover:text-destructive"
        >
          <Trash2Icon className="size-3.5" />
          <span className="sr-only">Revoke</span>
        </Button>
      )}
    </div>
  );
}
