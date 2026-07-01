import { Button } from "@/components/ui/button";
import type { AvailableConnectorTool } from "../connectors-utils";
import type { ConnectionPanelActionGroup } from "./useConnectionPanelState";

export function ConnectionPanelAgentActions({
  actionGroups,
  availableTools,
  selectedTools,
  onToolSelected,
  onToolsSelected,
}: {
  actionGroups: ConnectionPanelActionGroup[];
  availableTools: AvailableConnectorTool[];
  selectedTools: string[];
  onToolSelected: (token: string, checked: boolean) => void;
  onToolsSelected: (tokens: string[], checked: boolean) => void;
}) {
  if (availableTools.length === 0) return null;

  return (
    <section className="mt-4 rounded-md border border-border/70 p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h4 className="text-xs font-medium text-muted-foreground">Agent actions</h4>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Select the actions this connection should materialize for the agent.
          </p>
        </div>
        <div className="text-[11px] text-muted-foreground">
          {selectedTools.length}/{availableTools.length} selected
        </div>
      </div>
      <div className="max-h-[36rem] space-y-3 overflow-y-auto pr-1">
        {actionGroups.map((group) => (
          <div key={group.key} className="rounded-md border border-border/60 p-3">
            <div className="mb-2 text-sm font-medium">{group.name}</div>
            <div className="space-y-3">
              {group.services.map(({ definition, tools }) => {
                const tokens = tools.map((tool) => tool.token);
                return (
                  <div key={definition.key} className="rounded-md bg-muted/20 p-2">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <div className="text-sm font-medium">{definition.name}</div>
                        <div className="text-[11px] text-muted-foreground">
                          {tools.length} actions
                        </div>
                      </div>
                      <div className="flex gap-1">
                        <Button
                          type="button"
                          size="xs"
                          variant="ghost"
                          onClick={() => onToolsSelected(tokens, true)}
                        >
                          Select all
                        </Button>
                        <Button
                          type="button"
                          size="xs"
                          variant="ghost"
                          onClick={() => onToolsSelected(tokens, false)}
                        >
                          Clear
                        </Button>
                      </div>
                    </div>
                    <div className="grid gap-2 md:grid-cols-2">
                      {tools.map((tool) => (
                        <label key={tool.token} className="flex items-start gap-2 text-sm">
                          <input
                            type="checkbox"
                            className="mt-1 size-3.5"
                            checked={selectedTools.includes(tool.token)}
                            onChange={(e) => onToolSelected(tool.token, e.target.checked)}
                          />
                          <span>
                            <span className="font-medium">{tool.name}</span>
                            <span className="ml-1 text-[11px] text-muted-foreground">
                              {tool.serviceKey}:{tool.key}
                            </span>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}