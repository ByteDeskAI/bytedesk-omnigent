import type { ReactNode } from "react";
import {
  CheckIcon,
  CodeIcon,
  Columns2Icon,
  DownloadIcon,
  EyeIcon,
  FileDiffIcon,
  Link2Icon,
  MessageSquareTextIcon,
  PencilLineIcon,
  RowsIcon,
  SearchIcon,
} from "lucide-react";
import type { useFileContent } from "@/hooks/useFileContent";

export type FileViewerToolbarAction = {
  key: string;
  label: string;
  tooltip?: string;
  icon: ReactNode;
  onSelect: () => void;
  active?: boolean;
};

export function buildFileViewerToolbarActions({
  isPreviewable,
  lang,
  viewMode,
  commentsOpen,
  isDiffAvailable,
  splitToggleAvailable,
  diffLayout,
  isDeletedFile,
  fileQuery,
  linkCopied,
  guardDirty,
  setPreviewableViewMode,
  onCommentsToggle,
  setDiffActive,
  setDiffLayout,
  openSearch,
  downloadFile,
  copyFileLink,
}: {
  isPreviewable: boolean;
  lang: string;
  viewMode: "editor" | "preview" | "source" | "diff";
  commentsOpen: boolean;
  isDiffAvailable: boolean;
  splitToggleAvailable: boolean;
  diffLayout: "unified" | "split";
  isDeletedFile: boolean;
  fileQuery: ReturnType<typeof useFileContent>;
  linkCopied: boolean;
  guardDirty: (action: () => void) => void;
  setPreviewableViewMode: React.Dispatch<
    React.SetStateAction<"editor" | "preview" | "source">
  >;
  onCommentsToggle: () => void;
  setDiffActive: React.Dispatch<React.SetStateAction<boolean>>;
  setDiffLayout: React.Dispatch<React.SetStateAction<"unified" | "split">>;
  openSearch: () => void;
  downloadFile: () => void;
  copyFileLink: () => void;
}): FileViewerToolbarAction[] {
  const actions: FileViewerToolbarAction[] = [];
  if (isPreviewable && viewMode !== "diff") {
    const previewLabel =
      lang === "markdown"
        ? viewMode === "editor"
          ? "Source view"
          : "Rich text editor"
        : viewMode === "preview"
          ? "View source"
          : "View preview";
    actions.push({
      key: "preview",
      label: previewLabel,
      icon:
        lang === "markdown" ? (
          viewMode === "editor" ? (
            <CodeIcon className="size-4" />
          ) : (
            <PencilLineIcon className="size-4" />
          )
        ) : viewMode === "preview" ? (
          <CodeIcon className="size-4" />
        ) : (
          <EyeIcon className="size-4" />
        ),
      onSelect: () => {
        if (lang === "markdown") {
          guardDirty(() =>
            setPreviewableViewMode((mode) => (mode === "editor" ? "source" : "editor")),
          );
        } else {
          setPreviewableViewMode((mode) => (mode === "preview" ? "source" : "preview"));
        }
      },
    });
  }
  actions.push({
    key: "comments",
    label: commentsOpen ? "Hide comments" : "Show comments",
    icon: <MessageSquareTextIcon className="size-4" />,
    active: commentsOpen,
    onSelect: onCommentsToggle,
  });
  if (isDiffAvailable) {
    actions.push({
      key: "diff",
      label: viewMode === "diff" ? "Exit diff view" : "Show diff",
      icon: <FileDiffIcon className="size-4" />,
      active: viewMode === "diff",
      onSelect: () => guardDirty(() => setDiffActive((prev) => !prev)),
    });
  }
  if (viewMode === "diff" && splitToggleAvailable) {
    actions.push({
      key: "diff-layout",
      label: diffLayout === "unified" ? "Split view" : "Unified view",
      icon:
        diffLayout === "unified" ? (
          <Columns2Icon className="size-4" />
        ) : (
          <RowsIcon className="size-4" />
        ),
      onSelect: () => setDiffLayout((l) => (l === "unified" ? "split" : "unified")),
    });
  }
  actions.push({
    key: "search",
    label: "Find in file",
    icon: <SearchIcon className="size-4" />,
    onSelect: openSearch,
  });
  if (!isDeletedFile && fileQuery.data) {
    actions.push({
      key: "download",
      label: "Download file",
      tooltip: fileQuery.data.truncated
        ? "Download (file was truncated — content may be incomplete)"
        : "Download",
      icon: <DownloadIcon className="size-4" />,
      onSelect: downloadFile,
    });
  }
  actions.push({
    key: "copy-link",
    label: "Copy link to file",
    tooltip: linkCopied ? "Copied!" : "Copy link",
    icon: linkCopied ? (
      <CheckIcon className="size-4 text-green-500" />
    ) : (
      <Link2Icon className="size-4" />
    ),
    onSelect: copyFileLink,
  });
  return actions;
}