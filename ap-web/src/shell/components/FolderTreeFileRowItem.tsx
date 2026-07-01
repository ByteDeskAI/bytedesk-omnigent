import { FileIcon } from "lucide-react";
import { type WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { formatBytes, gitStatusLabel, gitStatusLetter } from "../fileStatusUtils";
import { FileDownloadButton } from "../FileDownloadButton";
import { useCursorTooltip } from "../useCursorTooltip";
import { FolderTreeIndentGuides } from "./FolderTreeIndentGuides";
import { indentFor } from "./folderTreeConstants";

export function FolderTreeFileRowItem({
  path,
  displayLabel,
  labelIsPath = false,
  depth = 0,
  fileStatus,
  bytes,
  onFileSelect,
  conversationId,
}: {
  path: string;
  displayLabel: string;
  labelIsPath?: boolean;
  depth?: number;
  fileStatus: WorkspaceChangedFile["status"] | undefined;
  bytes: number | null;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
}) {
  const isDeleted = fileStatus === "deleted";
  const fileColorClass =
    fileStatus === "created"
      ? "text-green-500 dark:text-green-400"
      : fileStatus === "modified"
        ? "text-amber-500 dark:text-amber-400"
        : isDeleted
          ? "text-destructive"
          : undefined;
  const { handlers, tooltip } = useCursorTooltip(path);

  return (
    <li>
      <div
        className="group relative flex w-full min-w-0 items-center gap-1.5 rounded-md py-1 pr-2 hover:bg-muted"
        style={{ paddingLeft: `${indentFor(depth)}px` }}
      >
        <FolderTreeIndentGuides depth={depth} />
        <button
          type="button"
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 text-left"
          onClick={() => !isDeleted && onFileSelect(path)}
          disabled={isDeleted}
        >
          <FileIcon
            className={cn("size-3.5 shrink-0", fileColorClass ?? "text-muted-foreground")}
          />
          <span
            className={cn(
              "min-w-0 flex-1 truncate font-mono text-sm md:text-xs",
              labelIsPath ? "[direction:rtl]" : fileStatus === "created" && "font-semibold",
              isDeleted && "line-through opacity-50",
              fileColorClass,
            )}
            {...handlers}
          >
            {labelIsPath ? <bdi>{displayLabel}</bdi> : displayLabel}
          </span>
          {fileStatus && (
            <span
              className={cn(
                "shrink-0 rounded px-1 py-0.5 font-mono text-[10px]",
                isDeleted
                  ? "bg-destructive/10 text-destructive"
                  : fileStatus === "created"
                    ? "bg-green-500/10 text-green-600 dark:text-green-400"
                    : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
              )}
              title={gitStatusLabel(fileStatus)}
            >
              {gitStatusLetter(fileStatus)}
            </span>
          )}
          {bytes !== null && !isDeleted && (
            <span className="shrink-0 text-muted-foreground text-[10px]">{formatBytes(bytes)}</span>
          )}
        </button>
        {!isDeleted && conversationId && (
          <FileDownloadButton conversationId={conversationId} path={path} />
        )}
      </div>
      {tooltip}
    </li>
  );
}