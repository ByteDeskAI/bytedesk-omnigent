import { ChatPageShell } from "./organisms/ChatPageShell";
import { useChatPage } from "./useChatPage";

// Re-export extracted modules for backward compatibility with tests and consumers.
export * from "@/components/chat/chat-utils";
export { Composer, type ComposerProps } from "@/components/chat/Composer";
export { BubbleView } from "@/components/chat/BubbleView";
export { ConnectionIndicator, SandboxFailedIndicator } from "@/components/chat/ConnectionIndicator";
export { RunnerStartingIndicator } from "@/components/chat/RunnerStartingIndicator";
export { HistoryAutoLoader } from "@/components/chat/HistoryAutoLoader";
export { JumpToTopButton } from "@/components/chat/JumpToTopButton";
export { MainAgentSurface } from "@/components/chat/MainAgentSurface";

export function ChatPage() {
  return <ChatPageShell {...useChatPage()} />;
}