import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CloudOffIcon,
  Loader2Icon,
  MoreHorizontalIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { SaveStatus } from "../../codeViewerHelpers";
import { useToolbarOverflow } from "../useToolbarOverflow";
import type { FileViewerToolbarAction } from "./buildFileViewerToolbarActions";

export function FileViewerToolbar({
  path,
  frameless,
  saveStatus,
  showNavButtons,
  prevPath,
  nextPath,
  currentNavIdx,
  navigableFilesLength,
  toolbarActions,
  onClose,
  onNavigateTo,
  guardDirty,
}: {
  path: string;
  frameless?: boolean;
  saveStatus: SaveStatus;
  showNavButtons: boolean;
  prevPath: string | null;
  nextPath: string | null;
  currentNavIdx: number;
  navigableFilesLength: number;
  toolbarActions: FileViewerToolbarAction[];
  onClose: () => void;
  onNavigateTo?: (path: string) => void;
  guardDirty: (action: () => void) => void;
}) {
  const {
    headerRef: toolbarHeaderRef,
    backRef: toolbarBackRef,
    navRef: toolbarNavRef,
    pathMeasureRef: toolbarPathMeasureRef,
    chipRef: toolbarChipRef,
    measureRef: toolbarMeasureRef,
    collapsed: toolbarCollapsed,
  } = useToolbarOverflow(
    [
      toolbarActions.map((a) => a.key).join(","),
      `back:${!frameless}`,
      `chip:${saveStatus !== "idle"}`,
      `nav:${showNavButtons}`,
      `path:${path}`,
    ].join("|"),
  );

  const renderActionButtons = (interactive: boolean) =>
    toolbarActions.map((action) => (
      <TooltipProvider key={action.key}>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant={action.active ? "default" : "ghost"}
              size="icon-sm"
              aria-label={action.label}
              tabIndex={interactive ? undefined : -1}
              onClick={interactive ? action.onSelect : undefined}
            >
              {action.icon}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{action.tooltip ?? action.label}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    ));

  return (
    <div
      ref={toolbarHeaderRef}
      className="flex min-w-0 shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-3"
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {!frameless && (
          <div ref={toolbarBackRef} className="shrink-0">
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Close file viewer"
                    onClick={() => guardDirty(onClose)}
                  >
                    <ArrowLeftIcon className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Close</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        )}
        {showNavButtons && (
          <div ref={toolbarNavRef} className="flex items-center gap-0.5 shrink-0">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="Previous file"
              disabled={!prevPath}
              onClick={() => prevPath && guardDirty(() => onNavigateTo?.(prevPath))}
            >
              <ChevronLeftIcon className="size-4" />
            </Button>
            <span className="text-[10px] text-muted-foreground tabular-nums">
              {currentNavIdx + 1}/{navigableFilesLength}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="Next file"
              disabled={!nextPath}
              onClick={() => nextPath && guardDirty(() => onNavigateTo?.(nextPath))}
            >
              <ChevronRightIcon className="size-4" />
            </Button>
          </div>
        )}
        <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">{path}</span>
      </div>
      <div
        className="relative flex min-w-0 items-center justify-end gap-1"
        data-testid="FILESTOOLBAR"
      >
        {saveStatus !== "idle" && (
          <span
            ref={toolbarChipRef}
            aria-live="polite"
            title={
              saveStatus === "offline"
                ? "Runner offline — your changes will save when it reconnects"
                : undefined
            }
            className={cn(
              "mr-1 flex shrink-0 items-center gap-1 whitespace-nowrap text-[11px]",
              saveStatus === "error" ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {saveStatus === "unsaved" && (
              <>
                <span className="size-1.5 rounded-full bg-muted-foreground/70" />
                Unsaved
              </>
            )}
            {saveStatus === "saving" && (
              <>
                <Loader2Icon className="size-3 animate-spin" />
                Saving…
              </>
            )}
            {saveStatus === "saved" && (
              <>
                <CheckIcon className="size-3 text-green-500" />
                Saved
              </>
            )}
            {saveStatus === "error" && (
              <>
                <AlertTriangleIcon className="size-3" />
                Save failed
              </>
            )}
            {saveStatus === "offline" && (
              <>
                <CloudOffIcon className="size-3" />
                Unsaved
              </>
            )}
          </span>
        )}
        <div className="flex items-center justify-end gap-1">
          {toolbarCollapsed ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button type="button" variant="ghost" size="icon-sm" aria-label="More actions">
                  <MoreHorizontalIcon className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-auto min-w-40">
                {toolbarActions.map((action) => (
                  <DropdownMenuItem
                    key={action.key}
                    className="whitespace-nowrap"
                    onSelect={action.onSelect}
                  >
                    {action.icon}
                    {action.tooltip ?? action.label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            renderActionButtons(true)
          )}
        </div>
        <div
          ref={toolbarMeasureRef}
          aria-hidden
          className="pointer-events-none absolute left-[-9999px] top-0 flex flex-nowrap items-center gap-1"
        >
          {renderActionButtons(false)}
        </div>
        <span
          ref={toolbarPathMeasureRef}
          aria-hidden
          className="pointer-events-none absolute left-[-9999px] top-0 font-mono text-xs whitespace-nowrap"
        >
          {path}
        </span>
      </div>
    </div>
  );
}