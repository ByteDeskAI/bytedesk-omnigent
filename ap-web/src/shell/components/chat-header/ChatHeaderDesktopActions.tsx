import {
  EllipsisVerticalIcon,
  InfoIcon,
  PanelRightCloseIcon,
  PanelRightIcon,
  ShareIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { AgentInfoButton } from "@/components/AgentInfo";
import { PresenceAvatars } from "@/components/PresenceAvatars";
import type { Agent } from "@/hooks/useAgents";

export function ChatHeaderDesktopActions({
  conversationId,
  boundAgent,
  canShare,
  onShare,
  hasAgentInfo,
  onAgentInfo,
  hasHeaderMenu,
  hasRailContent,
  rightPanelOpen,
  onToggleRightPanel,
}: {
  conversationId: string | undefined;
  boundAgent: Agent | undefined;
  canShare: boolean;
  onShare: () => void;
  hasAgentInfo: boolean;
  onAgentInfo: () => void;
  hasHeaderMenu: boolean;
  hasRailContent: boolean;
  rightPanelOpen: boolean;
  onToggleRightPanel: () => void;
}) {
  return (
    <div className="flex items-center gap-1">
      {conversationId && <PresenceAvatars />}
      {conversationId && <AgentInfoButton agent={boundAgent} sessionId={conversationId} />}
      {hasHeaderMenu && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Session actions"
              data-testid="session-actions-menu"
              className="text-muted-foreground hover:text-foreground md:hidden"
            >
              <EllipsisVerticalIcon className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-44">
            {canShare && (
              <DropdownMenuItem
                onSelect={onShare}
                data-testid="mobile-share-session"
                className="gap-2.5 px-2.5 py-2 text-base"
              >
                <ShareIcon className="size-4" />
                Share
              </DropdownMenuItem>
            )}
            {hasAgentInfo && (
              <DropdownMenuItem
                onSelect={onAgentInfo}
                data-testid="mobile-agent-info"
                className="gap-2.5 px-2.5 py-2 text-base"
              >
                <InfoIcon className="size-4" />
                Agent info
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      {canShare && (
        <Button
          type="button"
          aria-label="Share session"
          onClick={onShare}
          className="share-button-glassy hidden h-8 rounded-full px-6 text-13 font-normal md:inline-flex"
        >
          <ShareIcon className="size-4" />
          Share
        </Button>
      )}
      {conversationId && hasRailContent && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={rightPanelOpen ? "Collapse right panel" : "Expand right panel"}
              onClick={onToggleRightPanel}
              className="hidden md:inline-flex text-muted-foreground hover:text-foreground"
            >
              {rightPanelOpen ? (
                <PanelRightCloseIcon className="size-4" />
              ) : (
                <PanelRightIcon className="size-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {rightPanelOpen ? "Collapse right panel" : "Expand right panel"}
          </TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}