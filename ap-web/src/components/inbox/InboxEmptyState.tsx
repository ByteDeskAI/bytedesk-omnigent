import { InboxIcon } from "lucide-react";

export function InboxEmptyState() {
  return (
    <div className="flex flex-col items-center gap-2 py-16 text-center">
      <InboxIcon className="size-8 text-muted-foreground/50" />
      <p className="text-sm font-medium">Nothing waiting on you</p>
      <p className="text-xs text-muted-foreground">
        When an agent needs your input or someone comments on a file, it will show up here.
      </p>
    </div>
  );
}