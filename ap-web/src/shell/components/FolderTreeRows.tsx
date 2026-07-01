import { ChevronRightIcon } from "lucide-react";
import {
  type WorkspaceChangedFile,
  type WorkspaceFile,
  useWorkspaceDirectory,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { FolderTreeIndentGuides } from "./FolderTreeIndentGuides";
import { FolderTreeFileRowItem } from "./FolderTreeFileRowItem";
import { indentFor } from "./folderTreeConstants";
import { type FileNode, type TreeNode } from "./folderTreeUtils";

export function FolderTreeSearchResultRow({
  file,
  onFileSelect,
  conversationId,
  changedFileMap,
}: {
  file: WorkspaceFile;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  changedFileMap: Map<string, WorkspaceChangedFile["status"]>;
}) {
  return (
    <FolderTreeFileRowItem
      path={file.path}
      displayLabel={file.path}
      labelIsPath={true}
      fileStatus={changedFileMap.get(file.path)}
      bytes={file.bytes}
      onFileSelect={onFileSelect}
      conversationId={conversationId}
    />
  );
}

export function FolderTreeFileRow({
  node,
  depth,
  onFileSelect,
  conversationId,
  fileStatus,
}: {
  node: FileNode;
  depth: number;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  fileStatus: WorkspaceChangedFile["status"] | undefined;
}) {
  return (
    <FolderTreeFileRowItem
      path={node.file.path}
      displayLabel={node.name}
      depth={depth}
      fileStatus={fileStatus}
      bytes={node.file.bytes}
      onFileSelect={onFileSelect}
      conversationId={conversationId}
    />
  );
}

export function FolderTreeNodeRow({
  node,
  depth,
  onFileSelect,
  conversationId,
  expandedPaths,
  onTogglePath,
  showHidden,
  changedFileMap,
  dirtyDirMap,
}: {
  node: TreeNode;
  depth: number;
  onFileSelect: (path: string) => void;
  conversationId: string | undefined;
  expandedPaths: Set<string>;
  onTogglePath: (path: string) => void;
  showHidden: boolean;
  changedFileMap: Map<string, WorkspaceChangedFile["status"]>;
  dirtyDirMap: Map<string, WorkspaceChangedFile["status"]>;
}) {
  const open = node.type === "dir" && expandedPaths.has(node.path);
  const isLazyDir = node.type === "dir" && node.lazy === true;

  const { data: lazyData, isLoading: lazyLoading } = useWorkspaceDirectory(
    conversationId,
    isLazyDir && open ? node.path : null,
  );

  if (node.type === "file") {
    return (
      <FolderTreeFileRow
        node={node}
        depth={depth}
        onFileSelect={onFileSelect}
        conversationId={conversationId}
        fileStatus={changedFileMap.get(node.file.path)}
      />
    );
  }

  const rawChildNodes: TreeNode[] =
    isLazyDir && lazyData
      ? lazyData.map((file): TreeNode => {
          if (file.type === "directory") {
            return { type: "dir", name: file.name, path: file.path, children: [], lazy: true };
          }
          return { type: "file", name: file.name, file };
        })
      : node.children;
  const childNodes = showHidden
    ? rawChildNodes
    : rawChildNodes.filter((n) => !n.name.startsWith("."));

  const dirStatus = dirtyDirMap.get(node.path);
  const dirDotClass =
    dirStatus === "created"
      ? "text-green-500 dark:text-green-400"
      : dirStatus === "modified"
        ? "text-amber-500 dark:text-amber-400"
        : dirStatus === "deleted"
          ? "text-destructive"
          : undefined;

  return (
    <li>
      <button
        type="button"
        className="group relative flex w-full min-w-0 cursor-pointer items-center gap-1.5 rounded-md py-1 pr-2 text-left hover:bg-muted"
        style={{ paddingLeft: `${indentFor(depth)}px` }}
        onClick={() => onTogglePath(node.path)}
        aria-expanded={open}
      >
        <FolderTreeIndentGuides depth={depth} />
        <ChevronRightIcon
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        <span
          className={cn(
            "min-w-0 flex-1 truncate font-mono text-sm md:text-xs",
            dirStatus === "created" && "font-semibold",
            dirDotClass,
          )}
        >
          {node.name}/
        </span>
        {dirStatus && (
          <span className={cn("shrink-0 text-[8px] leading-none", dirDotClass)} aria-hidden>
            ●
          </span>
        )}
      </button>
      {open && (
        <ul className="flex flex-col gap-0.5">
          {lazyLoading && (
            <li
              className="relative py-1 pr-2 text-muted-foreground text-xs"
              style={{ paddingLeft: `${indentFor(depth + 1)}px` }}
            >
              <FolderTreeIndentGuides depth={depth + 1} />
              Loading…
            </li>
          )}
          {!lazyLoading && childNodes.length === 0 && rawChildNodes.length > 0 && (
            <li
              className="relative py-1 pr-2 text-muted-foreground text-xs"
              style={{ paddingLeft: `${indentFor(depth + 1)}px` }}
            >
              <FolderTreeIndentGuides depth={depth + 1} />
              All files are hidden — click the eye icon to reveal them.
            </li>
          )}
          {childNodes.map((child) => (
            <FolderTreeNodeRow
              key={child.type === "file" ? child.file.path : child.path}
              node={child}
              depth={depth + 1}
              onFileSelect={onFileSelect}
              conversationId={conversationId}
              expandedPaths={expandedPaths}
              onTogglePath={onTogglePath}
              showHidden={showHidden}
              changedFileMap={changedFileMap}
              dirtyDirMap={dirtyDirMap}
            />
          ))}
        </ul>
      )}
    </li>
  );
}