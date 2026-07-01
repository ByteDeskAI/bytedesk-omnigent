import { ArrowDownAZIcon, FileClockIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { type ChangedSort } from "../FlatFileList";

const SORT_OPTIONS: { value: ChangedSort; label: string; Icon: typeof ArrowDownAZIcon }[] = [
  { value: "alpha", label: "Filename", Icon: ArrowDownAZIcon },
  { value: "recent", label: "Last edited", Icon: FileClockIcon },
];

export function FilesPanelSortSelector({
  sort,
  onChange,
}: {
  sort: ChangedSort;
  onChange: (next: ChangedSort) => void;
}) {
  const active = SORT_OPTIONS.find((o) => o.value === sort) ?? SORT_OPTIONS[0];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`Sort: ${active.label}`}
          className="flex shrink-0 cursor-pointer items-center gap-1 rounded-full px-2.5 py-[4px] text-muted-foreground text-xs hover:bg-muted hover:text-foreground"
        >
          <span>Sort:</span>
          <active.Icon className="size-3.5" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-40">
        <DropdownMenuRadioGroup value={sort} onValueChange={(v) => onChange(v as ChangedSort)}>
          {SORT_OPTIONS.map(({ value, label, Icon }) => (
            <DropdownMenuRadioItem key={value} value={value}>
              <Icon className="size-3.5" />
              {label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}