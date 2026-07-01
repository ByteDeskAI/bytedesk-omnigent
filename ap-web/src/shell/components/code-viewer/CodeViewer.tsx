import { type RefObject } from "react";
import { type Comment } from "@/hooks/useComments";
import { useFileContent } from "@/hooks/useFileContent";
import { isBinaryPath, type ActiveSelection, type SaveStatus } from "../../codeViewerHelpers";
import { CodeViewerMainView } from "./CodeViewerMainView";
import {
  CodeViewerBinaryPanel,
  CodeViewerErrorPanel,
  CodeViewerLoadingPanel,
} from "./CodeViewerStatusPanels";
import { useCodeViewerState } from "./useCodeViewerState";

export interface CodeViewerProps {
  conversationId: string;
  path: string;
  fileQuery: ReturnType<typeof useFileContent>;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (
    sel: { start_index: number; end_index: number; anchor_content: string } | null,
  ) => void;
  panelOpen: boolean;
  searchOpen: boolean;
  setSearchOpen: (open: boolean) => void;
  searchInputRef: RefObject<HTMLInputElement | null>;
  viewMode: "editor" | "preview" | "source" | "diff";
  onDirtyChange?: (isDirty: boolean) => void;
  onSaveStatusChange?: (status: SaveStatus) => void;
  pendingBodyRef?: RefObject<string>;
}

export function CodeViewer(props: CodeViewerProps) {
  const viewerState = useCodeViewerState(props);
  const { path, fileQuery } = props;

  if (fileQuery.isLoading) return <CodeViewerLoadingPanel />;
  if (fileQuery.isError) return <CodeViewerErrorPanel fileQuery={fileQuery} />;
  if (fileQuery.data?.encoding === "base64" || isBinaryPath(path)) return <CodeViewerBinaryPanel />;

  return <CodeViewerMainView {...props} viewerState={viewerState} />;
}