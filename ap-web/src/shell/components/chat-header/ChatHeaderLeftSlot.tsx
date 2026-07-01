import { BotIcon, ChevronLeftIcon, PanelLeftIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import { cn } from "@/lib/utils";

export function ChatHeaderLeftSlot({
  sidebarOpen,
  onOpenSidebar,
  isChildSession,
  parentSessionId,
  boundAgent,
}: {
  sidebarOpen: boolean;
  onOpenSidebar: () => void;
  isChildSession: boolean;
  parentSessionId: string | null | undefined;
  boundAgent: Agent | undefined;
}) {
  return (
    <div className={cn("flex items-center gap-1", !sidebarOpen && "traffic-light-clearance")}>
      {!sidebarOpen && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Open sidebar"
              onClick={onOpenSidebar}
              className="text-muted-foreground hover:text-foreground"
            >
              <PanelLeftIcon className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Open sidebar</TooltipContent>
        </Tooltip>
      )}
      {isChildSession && parentSessionId && (
        <>
          <Button
            asChild
            type="button"
            variant="ghost"
            size="sm"
            className="gap-0.5 pl-1.5 pr-2 text-muted-foreground hover:text-foreground"
          >
            <Link to={`/c/${parentSessionId}`} aria-label="Back to parent session">
              <ChevronLeftIcon className="size-4" />
              <span>Back</span>
            </Link>
          </Button>
          <span aria-hidden className="mx-1 h-5 w-px bg-border" />
          <div className="flex min-w-0 items-center gap-2">
            <BotIcon className="size-4 shrink-0 text-muted-foreground" />
            {boundAgent?.name ? (
              <div className="flex min-w-0 flex-col leading-tight">
                <span className="truncate text-sm font-semibold text-foreground">
                  {boundAgent.name}
                </span>
                <span className="text-xs text-muted-foreground">Sub-agent</span>
              </div>
            ) : (
              <span className="text-sm font-semibold text-foreground">Sub-agent</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}