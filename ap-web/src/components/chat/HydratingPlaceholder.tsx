import { Loader2Icon } from "lucide-react";

export function HydratingPlaceholder() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 text-muted-foreground text-sm">
      <Loader2Icon className="size-4 animate-spin" />
      Loading conversation…
    </div>
  );
}