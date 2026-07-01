import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { agentDisplayName } from "../../workforce-utils";
import type { PermissionsTabState } from "../usePermissionsTab";

export function PermissionsToolsTabView({
  agent,
  editable,
  scopeLabel,
  scopeTools,
  toolCatalogRows,
  scopeToolByKey,
  toolStateLabel,
  setScopeTool,
  upsertTool,
  effectiveTools,
  effectiveToolByKey,
  inheritedToolLabel,
  toggleOverride,
  upsertOverride,
  toolCatalog,
}: Pick<
  PermissionsTabState,
  | "agent"
  | "editable"
  | "scopeLabel"
  | "scopeTools"
  | "toolCatalogRows"
  | "scopeToolByKey"
  | "toolStateLabel"
  | "setScopeTool"
  | "upsertTool"
  | "effectiveTools"
  | "effectiveToolByKey"
  | "inheritedToolLabel"
  | "toggleOverride"
  | "upsertOverride"
  | "toolCatalog"
>) {
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_25rem]">
      <section className="mc-surface">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div>
            <div className="mc-label">{scopeLabel} Builtin Tools</div>
            <div className="text-xs text-muted-foreground">
              Explicit grant or deny rows for this inheritance level.
            </div>
          </div>
          <Badge variant="secondary">{scopeTools.length}</Badge>
        </div>
        <div className="grid max-h-[32rem] gap-2 overflow-y-auto p-3 md:grid-cols-2">
          {toolCatalogRows.map((tool) => {
            const assignment = scopeToolByKey.get(tool.toolKey);
            return (
              <div
                key={tool.toolKey}
                data-testid={`scope-tool-row-${tool.toolKey}`}
                className="flex min-w-0 flex-col gap-2 rounded-md border border-border/70 px-3 py-2 text-sm"
              >
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <span className="truncate font-medium">{tool.label}</span>
                    <Badge variant={assignment?.enabled ? "default" : "outline"}>
                      {toolStateLabel(tool)}
                    </Badge>
                  </div>
                  <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                    {tool.toolKey} · {tool.description}
                  </div>
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant={assignment?.enabled ? "secondary" : "outline"}
                    disabled={!editable || upsertTool.isPending}
                    onClick={() => void setScopeTool(tool, true)}
                  >
                    Grant here
                  </Button>
                  <Button
                    size="sm"
                    variant={assignment && !assignment.enabled ? "secondary" : "outline"}
                    disabled={!editable || upsertTool.isPending}
                    onClick={() => void setScopeTool(tool, false)}
                  >
                    Deny here
                  </Button>
                </div>
              </div>
            );
          })}
          {!toolCatalog.isLoading && toolCatalogRows.length === 0 && (
            <div className="text-sm text-muted-foreground">No builtin tools reported.</div>
          )}
        </div>
      </section>

      <section className="mc-surface">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div>
            <div className="mc-label text-accent-cyan">Effective Agent Tools</div>
            <div className="text-xs text-muted-foreground">{agentDisplayName(agent)}</div>
          </div>
          <Badge variant="secondary">
            {effectiveTools.filter((item) => item.enabled).length}
          </Badge>
        </div>
        <div className="max-h-[32rem] divide-y divide-border-dimmer overflow-y-auto">
          {toolCatalogRows.map((catalogItem) => {
            const tool = effectiveToolByKey.get(catalogItem.toolKey);
            const enabled = tool?.enabled ?? false;
            return (
              <div
                key={catalogItem.toolKey}
                data-testid={`effective-tool-row-${catalogItem.toolKey}`}
                className="p-3"
              >
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{catalogItem.label}</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {catalogItem.toolKey} · {inheritedToolLabel(tool)}
                    </div>
                  </div>
                  <Badge variant={enabled ? "default" : "outline"}>
                    {/* An unmanaged tool isn't "Disabled" — the agent's own
                        config.yaml decides; workforce only knows managed state. */}
                    {tool ? (enabled ? "Enabled" : "Disabled") : "Not managed"}
                  </Badge>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-2"
                  disabled={!editable || upsertOverride.isPending}
                  onClick={() => void toggleOverride("tool", catalogItem.toolKey, !enabled)}
                >
                  {enabled ? "Disable for agent" : "Enable for agent"}
                </Button>
              </div>
            );
          })}
          {!toolCatalog.isLoading && toolCatalogRows.length === 0 && (
            <div className="p-4 text-sm text-muted-foreground">No builtin tools reported.</div>
          )}
        </div>
      </section>
    </div>
  );
}