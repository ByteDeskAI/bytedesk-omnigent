import { WorkspacePathRow } from "./WorkspacePathRow";

export interface WorkspacePathDropdownProps {
  filteredRecent: string[];
  matches: string[];
  hiddenMatchCount: number;
  highlight: number;
  showLoading: boolean;
  onSelect: (path: string) => void;
}

export function WorkspacePathDropdown({
  filteredRecent,
  matches,
  hiddenMatchCount,
  highlight,
  showLoading,
  onSelect,
}: WorkspacePathDropdownProps) {
  return (
    <div
      id="workspace-path-listbox"
      role="listbox"
      className="mt-1 max-h-72 overflow-y-auto rounded-md border border-border bg-popover shadow-md"
      data-testid="workspace-path-dropdown"
    >
      {filteredRecent.length > 0 && (
        <>
          <div className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Recent
          </div>
          {filteredRecent.map((path, i) => (
            <WorkspacePathRow
              key={`recent-${path}`}
              path={path}
              active={highlight === i}
              onSelect={() => onSelect(path)}
              testId={`workspace-recent-${i}`}
            />
          ))}
        </>
      )}
      {matches.length > 0 && (
        <>
          <div className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Matches
          </div>
          {matches.map((path, j) => (
            <WorkspacePathRow
              key={`match-${path}`}
              path={path}
              active={highlight === filteredRecent.length + j}
              onSelect={() => onSelect(path)}
              testId={`workspace-match-${j}`}
            />
          ))}
          {hiddenMatchCount > 0 && (
            <div
              className="px-3 py-2 text-xs text-muted-foreground"
              data-testid="workspace-match-overflow"
            >
              +{hiddenMatchCount} more — keep typing to narrow
            </div>
          )}
        </>
      )}
      {showLoading && <div className="px-3 py-2 text-xs text-muted-foreground">Loading…</div>}
    </div>
  );
}