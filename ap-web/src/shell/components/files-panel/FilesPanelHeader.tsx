import { ChevronDownIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { HiddenFilesToggle } from "../HiddenFilesToggle";
import { WorkingDirLabel } from "../WorkingDirLabel";

export interface FilesPanelHeaderProps {
  fullScreen: boolean;
  collapsed: boolean;
  workingDir: string | null;
  showHidden: boolean;
  hiddenFilesCount: number;
  onToggleCollapsed: () => void;
  onToggleHidden: () => void;
  onClose?: () => void;
}

export function FilesPanelHeader({
  fullScreen,
  collapsed,
  workingDir,
  showHidden,
  hiddenFilesCount,
  onToggleCollapsed,
  onToggleHidden,
  onClose,
}: FilesPanelHeaderProps) {
  return (
    <div className="flex shrink-0 items-center gap-2 px-3 py-2">
      {fullScreen ? (
        <>
          <span className="shrink-0 font-medium text-sm">Working folder</span>
          {workingDir && <WorkingDirLabel dir={workingDir} />}
          <div className="ml-auto flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
            <HiddenFilesToggle
              showHidden={showHidden}
              onToggle={onToggleHidden}
              size="4"
              hiddenCount={hiddenFilesCount}
            />
            {onClose && (
              <button
                type="button"
                aria-label="Close files"
                className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                onClick={onClose}
              >
                <XIcon className="size-4" />
              </button>
            )}
          </div>
        </>
      ) : (
        <>
          <button
            type="button"
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
          >
            <span className="shrink-0 font-medium text-sm">Working folder</span>
            {workingDir && <WorkingDirLabel dir={workingDir} />}
            <ChevronDownIcon
              className={cn(
                "ml-auto size-4 shrink-0 text-muted-foreground transition-transform duration-150",
                collapsed && "-rotate-90",
              )}
            />
          </button>
          {!collapsed && (
            <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
              <HiddenFilesToggle
                showHidden={showHidden}
                onToggle={onToggleHidden}
                size="3.5"
                hiddenCount={hiddenFilesCount}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}