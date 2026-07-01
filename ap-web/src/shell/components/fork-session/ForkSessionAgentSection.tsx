import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { SAME_AS_SOURCE } from "./forkSessionConstants";

export function ForkSessionAgentSection({
  agentChoice,
  setAgentChoice,
  sourceAgentDisplay,
  switchableAgents,
  switching,
}: {
  agentChoice: string;
  setAgentChoice: (value: string) => void;
  sourceAgentDisplay: string;
  switchableAgents: AvailableAgent[];
  switching: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor="fork-session-agent" className="text-xs font-medium text-muted-foreground">
        Agent
      </label>
      <Select value={agentChoice} onValueChange={setAgentChoice}>
        <SelectTrigger
          id="fork-session-agent"
          data-testid="fork-session-agent-select"
          className="w-full text-xs"
        >
          <SelectValue>
            {switching ? (
              (switchableAgents.find((a) => a.id === agentChoice)?.display_name ?? sourceAgentDisplay)
            ) : (
              <>
                {sourceAgentDisplay}{" "}
                <span className="text-muted-foreground">(same as original session)</span>
              </>
            )}
          </SelectValue>
        </SelectTrigger>
        <SelectContent position="popper" align="start">
          <SelectItem
            value={SAME_AS_SOURCE}
            data-testid="fork-session-agent-option-same"
            className="text-xs"
          >
            {sourceAgentDisplay}{" "}
            <span className="text-muted-foreground">(same as original session)</span>
          </SelectItem>
          {switchableAgents.map((agent) => (
            <SelectItem
              key={agent.id}
              value={agent.id}
              data-testid={`fork-session-agent-option-${agent.id}`}
              className="text-xs"
            >
              {agent.display_name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}