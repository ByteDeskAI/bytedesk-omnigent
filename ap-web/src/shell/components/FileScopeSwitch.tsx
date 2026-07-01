import { FolderTreeIcon, ListIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export function FileScopeSwitch({
  flatView,
  onChange,
  count,
}: {
  flatView: boolean;
  onChange: (flatView: boolean) => void;
  count: number;
}) {
  const changedSelected = flatView;
  const allSelected = !flatView;
  const pill =
    "flex cursor-pointer items-center gap-[6px] rounded-full px-[14px] py-[2px] text-[13px] font-medium leading-5 transition-colors";
  const activePill =
    "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground";
  const idlePill = "text-muted-foreground hover:text-foreground";
  return (
    <div role="radiogroup" aria-label="File scope" className="flex shrink-0 items-center gap-1">
      <button
        type="button"
        role="radio"
        aria-checked={changedSelected}
        aria-label="Changed"
        title="Show changed files only"
        onClick={() => onChange(true)}
        className={cn(pill, changedSelected ? activePill : idlePill)}
      >
        <ListIcon className="size-3.5 shrink-0" />
        Changed
        {count > 0 && (
          <span className="shrink-0 font-normal text-[11px] text-muted-foreground tabular-nums">
            {count}
          </span>
        )}
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={allSelected}
        aria-label="All"
        title="Show the full folder tree"
        onClick={() => onChange(false)}
        className={cn(pill, allSelected ? activePill : idlePill)}
      >
        <FolderTreeIcon className="size-3.5 shrink-0" />
        All
      </button>
    </div>
  );
}