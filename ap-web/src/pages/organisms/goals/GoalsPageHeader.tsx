import { TargetIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";

export interface GoalsPageHeaderProps {
  scopeLabel: string;
}

export function GoalsPageHeader({ scopeLabel }: GoalsPageHeaderProps) {
  return (
    <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
          <TargetIcon className="size-4" />
        </span>
        <div className="min-w-0">
          <h1 className="truncate text-base font-semibold">Goals</h1>
          <p className="truncate text-xs text-muted-foreground">{scopeLabel}</p>
        </div>
      </div>
      <Button variant="ghost" size="icon" asChild aria-label="Close goals">
        <Link to="/">
          <XIcon />
        </Link>
      </Button>
    </header>
  );
}