import { cn } from "@/lib/utils";

export function ToolbarBtn({
  children,
  active = false,
  title,
  onClick,
  className,
}: {
  children: React.ReactNode;
  active?: boolean;
  title: string;
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={cn(
        "min-w-[1.75rem] rounded px-1.5 py-0.5 text-xs transition-colors",
        active
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
        className,
      )}
    >
      {children}
    </button>
  );
}

export function Divider() {
  return <div className="mx-1 h-4 w-px shrink-0 bg-border" />;
}