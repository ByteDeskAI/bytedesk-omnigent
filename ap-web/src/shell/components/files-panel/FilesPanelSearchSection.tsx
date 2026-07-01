import { SearchIcon, SlidersHorizontalIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ChangedSort } from "../../FlatFileList";
import { FileScopeSwitch } from "../FileScopeSwitch";
import { FilesPanelSortSelector } from "../FilesPanelSortSelector";
import { SearchFilterInput } from "../SearchFilterInput";

export interface FilesPanelSearchSectionProps {
  flatView: boolean;
  changedCount: number;
  changedSort: ChangedSort;
  changedSearch: string;
  treeSearch: string;
  treeInclude: string;
  treeExclude: string;
  showSearchFilters: boolean;
  treeFiltersActive: boolean;
  onFlatViewChange: (flatView: boolean) => void;
  onSortChange: (sort: ChangedSort) => void;
  onChangedSearchChange: (value: string) => void;
  onTreeSearchChange: (value: string) => void;
  onTreeIncludeChange: (value: string) => void;
  onTreeExcludeChange: (value: string) => void;
  onToggleSearchFilters: () => void;
}

export function FilesPanelSearchSection({
  flatView,
  changedCount,
  changedSort,
  changedSearch,
  treeSearch,
  treeInclude,
  treeExclude,
  showSearchFilters,
  treeFiltersActive,
  onFlatViewChange,
  onSortChange,
  onChangedSearchChange,
  onTreeSearchChange,
  onTreeIncludeChange,
  onTreeExcludeChange,
  onToggleSearchFilters,
}: FilesPanelSearchSectionProps) {
  if (flatView) {
    return (
      <div
        className="shrink-0 flex items-center gap-2 px-2 py-1.5 @max-[400px]/filespanel:flex-col @max-[400px]/filespanel:items-stretch"
        onClick={(e) => e.stopPropagation()}
      >
        <FileScopeSwitch flatView={flatView} onChange={onFlatViewChange} count={changedCount} />
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-[6px] rounded-full border border-border px-[10px] py-[4px] transition-colors focus-within:border-border-strong">
            <SearchIcon className="size-4 shrink-0 text-muted-foreground" />
            <input
              aria-label="Search changed files"
              className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
              onChange={(event) => onChangedSearchChange(event.target.value)}
              placeholder="Search"
              type="search"
              value={changedSearch}
            />
          </div>
          <FilesPanelSortSelector sort={changedSort} onChange={onSortChange} />
        </div>
      </div>
    );
  }

  return (
    <div className="shrink-0" onClick={(e) => e.stopPropagation()}>
      <div className="flex items-center gap-2 px-2 py-1.5 @max-[400px]/filespanel:flex-col @max-[400px]/filespanel:items-stretch">
        <FileScopeSwitch flatView={flatView} onChange={onFlatViewChange} count={changedCount} />
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-[6px] rounded-full border border-border px-[10px] py-[4px] transition-colors focus-within:border-border-strong">
            <SearchIcon className="size-4 shrink-0 text-muted-foreground" />
            <input
              aria-label="Search all files"
              className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
              onChange={(event) => onTreeSearchChange(event.target.value)}
              placeholder="Search"
              type="search"
              value={treeSearch}
            />
          </div>
          <button
            type="button"
            aria-label={showSearchFilters ? "Hide search filters" : "Show search filters"}
            aria-expanded={showSearchFilters}
            title="Files to include / exclude"
            className={cn(
              "flex shrink-0 cursor-pointer items-center gap-1 rounded-full px-2.5 py-[4px] hover:bg-muted",
              showSearchFilters || treeFiltersActive
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
            onClick={onToggleSearchFilters}
          >
            <SlidersHorizontalIcon className="size-3.5" />
            {treeFiltersActive && !showSearchFilters && (
              <span className="size-1.5 rounded-full bg-primary" aria-hidden />
            )}
          </button>
        </div>
      </div>
      {showSearchFilters && (
        <div className="flex flex-col gap-1.5 border-border border-t px-3 py-2">
          <SearchFilterInput
            label="files to include"
            placeholder="e.g. *.ts, src/**"
            value={treeInclude}
            onChange={onTreeIncludeChange}
          />
          <SearchFilterInput
            label="files to exclude"
            placeholder="e.g. **/node_modules, *.test.ts"
            value={treeExclude}
            onChange={onTreeExcludeChange}
          />
        </div>
      )}
    </div>
  );
}