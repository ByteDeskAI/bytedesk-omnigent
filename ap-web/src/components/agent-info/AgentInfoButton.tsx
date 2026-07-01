import { InfoIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentInfoContent, agentHasInfo, type AgentInfoProps } from "./AgentInfoContent";

export function AgentInfoButton({ agent, sessionId }: AgentInfoProps) {
  if (!agentHasInfo(agent, sessionId)) return null;

  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Agent tools and policies"
              data-testid="agent-info-trigger"
              className="hidden text-muted-foreground hover:text-foreground md:inline-flex"
            >
              <InfoIcon className="size-4" />
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>Agent tools &amp; policies</TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-80">
        <AgentInfoContent agent={agent} sessionId={sessionId} />
      </PopoverContent>
    </Popover>
  );
}