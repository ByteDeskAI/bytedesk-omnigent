import { PlugIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  useConnectorAgentGrants,
  useConnectorsCatalog,
  useGrantConnectorToAgent,
} from "@/hooks/useConnectors";
import { toolsForConnection } from "./workforce-utils";

export function ConnectorsTab({ agentId, editable }: { agentId: string; editable: boolean }) {
  const catalog = useConnectorsCatalog();
  const grants = useConnectorAgentGrants(agentId);
  const grantMutation = useGrantConnectorToAgent();
  const [selectedByConnection, setSelectedByConnection] = useState<Record<string, string[]>>({});

  const rows = useMemo(
    () =>
      (catalog.data ?? []).flatMap((provider) =>
        provider.connections.map((connection) => ({
          provider,
          connection,
          tools: toolsForConnection(provider, connection),
        })),
      ),
    [catalog.data],
  );

  useEffect(() => {
    const next: Record<string, string[]> = {};
    for (const row of rows) {
      next[row.connection.id] = (grants.data ?? [])
        .filter((grant) => grant.connectionId === row.connection.id && grant.enabled)
        .map((grant) => `${grant.serviceKey}:${grant.toolKey}`);
    }
    setSelectedByConnection(next);
  }, [agentId, grants.data, rows]);

  function setTool(connectionId: string, token: string, checked: boolean) {
    setSelectedByConnection((prev) => {
      const current = new Set(prev[connectionId] ?? []);
      if (checked) current.add(token);
      else current.delete(token);
      return { ...prev, [connectionId]: Array.from(current) };
    });
  }

  return (
    <div className="mc-fade-up space-y-4 p-4">
      {rows.map(({ provider, connection, tools }) => {
        const selected = selectedByConnection[connection.id] ?? [];
        return (
          <section key={connection.id} className="mc-surface">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
              <div>
                <div className="text-sm font-medium">{connection.displayName}</div>
                <div className="text-xs text-muted-foreground">
                  {provider.name} · {connection.status} · {selected.length}/{tools.length} actions
                </div>
              </div>
              <Button
                size="sm"
                disabled={!editable || selected.length === 0 || grantMutation.isPending}
                onClick={() =>
                  grantMutation.mutate({
                    connectionId: connection.id,
                    agentId,
                    tools: selected,
                  })
                }
              >
                <PlugIcon /> Save actions
              </Button>
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
                    aria-label={`${connection.displayName} ${tool.name}`}
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
          </section>
        );
      })}
      {!catalog.isLoading && rows.length === 0 && (
        <div className="mc-surface p-4 text-sm text-muted-foreground">
          No connector connections are configured.
        </div>
      )}
      {grantMutation.isError && (
        <div className="text-sm text-destructive">
          {grantMutation.error instanceof Error ? grantMutation.error.message : "Grant failed"}
        </div>
      )}
    </div>
  );
}