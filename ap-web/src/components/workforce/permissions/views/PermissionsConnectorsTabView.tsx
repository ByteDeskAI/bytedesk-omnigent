import { PlugIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { inheritedSourceLabel } from "../../workforce-utils";
import type { PermissionsTabState } from "../usePermissionsTab";

export function PermissionsConnectorsTabView({
  editable,
  scopeLabel,
  scope,
  rows,
  selectedByConnection,
  setTool,
  saveConnector,
  upsertConnector,
  connectorCatalog,
  effectiveConnectors,
  effective,
  connectorLabel,
  toggleOverride,
  upsertOverride,
}: Pick<
  PermissionsTabState,
  | "editable"
  | "scopeLabel"
  | "scope"
  | "rows"
  | "selectedByConnection"
  | "setTool"
  | "saveConnector"
  | "upsertConnector"
  | "connectorCatalog"
  | "effectiveConnectors"
  | "effective"
  | "connectorLabel"
  | "toggleOverride"
  | "upsertOverride"
>) {
  return (
    <div className="space-y-4">
      <section className="mc-surface">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div>
            <div className="mc-label">{scopeLabel} Connector Actions</div>
            <div className="text-xs text-muted-foreground">
              {(scope.data?.connectors ?? []).filter((item) => item.enabled).length} active
            </div>
          </div>
        </div>
        <div className="space-y-3 p-3">
          {rows.map(({ provider, connection, tools }) => {
            const selected = selectedByConnection[connection.id] ?? [];
            return (
              <div key={connection.id} className="rounded-md border border-border-dimmer">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
                  <div>
                    <div className="text-sm font-medium">{connection.displayName}</div>
                    <div className="text-xs text-muted-foreground">
                      {provider.name} · {selected.length}/{tools.length} permissions
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!editable || tools.length === 0 || upsertConnector.isPending}
                      onClick={() => void saveConnector(connection.id, [], false)}
                    >
                      Disable all
                    </Button>
                    <Button
                      size="sm"
                      disabled={!editable || selected.length === 0 || upsertConnector.isPending}
                      onClick={() => void saveConnector(connection.id, selected, true)}
                    >
                      <PlugIcon /> Save permissions
                    </Button>
                  </div>
                </div>
                <div className="grid gap-2 p-3 md:grid-cols-2 xl:grid-cols-3">
                  {tools.map((tool) => (
                    <label
                      key={tool.token}
                      className="flex items-start gap-2 rounded-md border border-border/70 px-3 py-2 text-sm"
                    >
                      <input
                        type="checkbox"
                        className="mt-1 size-3.5"
                        checked={selected.includes(tool.token)}
                        disabled={!editable}
                        onChange={(event) => setTool(connection.id, tool.token, event.target.checked)}
                        aria-label={`${scopeLabel} ${connection.displayName} ${tool.name}`}
                      />
                      <span className="min-w-0">
                        <span className="block truncate font-medium">{tool.name}</span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {tool.serviceName} · {tool.mcpTool}
                        </span>
                      </span>
                    </label>
                  ))}
                  {tools.length === 0 && (
                    <div className="text-sm text-muted-foreground">No enabled actions.</div>
                  )}
                </div>
              </div>
            );
          })}
          {!connectorCatalog.isLoading && rows.length === 0 && (
            <div className="text-sm text-muted-foreground">No connector connections.</div>
          )}
        </div>
      </section>

      <section className="mc-surface">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div className="mc-label text-accent-cyan">Effective Connector Permissions</div>
          <Badge variant="secondary">
            {effectiveConnectors.filter((item) => item.enabled).length}
          </Badge>
        </div>
        <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
          {effectiveConnectors.map((item) => (
            <div key={item.itemKey} className="flex items-start justify-between gap-3 p-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{connectorLabel(item)}</div>
                <div className="truncate text-xs text-muted-foreground">
                  {inheritedSourceLabel(item)}
                </div>
              </div>
              <Button
                size="xs"
                variant="outline"
                disabled={!editable || upsertOverride.isPending}
                onClick={() => void toggleOverride("connector", item.itemKey, !item.enabled)}
              >
                {item.enabled ? "Disable for agent" : "Enable for agent"}
              </Button>
            </div>
          ))}
          {!effective.isLoading && effectiveConnectors.length === 0 && (
            <div className="p-4 text-sm text-muted-foreground">
              No inherited connector permissions.
            </div>
          )}
        </div>
      </section>
    </div>
  );
}