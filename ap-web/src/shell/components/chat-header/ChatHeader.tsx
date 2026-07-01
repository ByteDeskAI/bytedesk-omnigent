import type { Agent } from "@/hooks/useAgents";
import { cn } from "@/lib/utils";
import { ChatHeaderDesktopActions } from "./ChatHeaderDesktopActions";
import {
  ChatHeaderMobileRailMenu,
  type MobileSessionMenuProps,
} from "./ChatHeaderMobileRailMenu";
import { ChatHeaderLeftSlot } from "./ChatHeaderLeftSlot";

export type { MobileSessionMenuProps };

interface ChatHeaderProps {
  sidebarOpen: boolean;
  onOpenSidebar: () => void;
  isChildSession: boolean;
  parentSessionId: string | null | undefined;
  conversationId: string | undefined;
  boundAgent: Agent | undefined;
  canShare: boolean;
  onShare: () => void;
  hasAgentInfo: boolean;
  onAgentInfo: () => void;
  hasHeaderMenu: boolean;
  showFilesPanel: boolean;
  hasRailContent: boolean;
  rightPanelOpen: boolean;
  onToggleRightPanel: () => void;
  mobileMenu: MobileSessionMenuProps;
}

export function ChatHeader({
  sidebarOpen,
  onOpenSidebar,
  isChildSession,
  parentSessionId,
  conversationId,
  boundAgent,
  canShare,
  onShare,
  hasAgentInfo,
  onAgentInfo,
  hasHeaderMenu,
  showFilesPanel,
  hasRailContent,
  rightPanelOpen,
  onToggleRightPanel,
  mobileMenu,
}: ChatHeaderProps) {
  return (
    <header
      className={cn(
        "absolute inset-x-0 top-0 z-30 flex h-14 items-center justify-between px-2 py-3",
      )}
    >
      <ChatHeaderLeftSlot
        sidebarOpen={sidebarOpen}
        onOpenSidebar={onOpenSidebar}
        isChildSession={isChildSession}
        parentSessionId={parentSessionId}
        boundAgent={boundAgent}
      />

      <div className="flex items-center gap-1">
        <ChatHeaderDesktopActions
          conversationId={conversationId}
          boundAgent={boundAgent}
          canShare={canShare}
          onShare={onShare}
          hasAgentInfo={hasAgentInfo}
          onAgentInfo={onAgentInfo}
          hasHeaderMenu={hasHeaderMenu}
          hasRailContent={hasRailContent}
          rightPanelOpen={rightPanelOpen}
          onToggleRightPanel={onToggleRightPanel}
        />
        <ChatHeaderMobileRailMenu
          conversationId={conversationId}
          showFilesPanel={showFilesPanel}
          hasRailContent={hasRailContent}
          mobileMenu={mobileMenu}
        />
      </div>
    </header>
  );
}