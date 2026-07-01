import { CommentSenderProvider } from "@/hooks/CommentSenderContext";
import { useChatStore } from "@/store/chatStore";
import { FileViewerBody } from "./components/file-viewer/FileViewerBody";

export { classifyAndRemapComments } from "./components/file-viewer/fileViewerUtils";

interface FileViewerProps {
  open: boolean;
  conversationId: string;
  path: string;
  onClose: () => void;
  onNavigateTo?: (path: string) => void;
  permissionLevel?: number | null;
  frameless?: boolean;
  onCommentsOpenChange?: (open: boolean) => void;
  sort?: import("./FlatFileList").ChangedSort;
}

export function FileViewer(props: FileViewerProps) {
  const agentId = useChatStore((s) => s.boundAgentId);
  return (
    <CommentSenderProvider sessionId={props.conversationId} agentId={agentId}>
      <FileViewerBody {...props} />
    </CommentSenderProvider>
  );
}