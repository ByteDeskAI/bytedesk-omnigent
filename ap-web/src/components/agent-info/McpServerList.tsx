import { ServerIcon } from "lucide-react";
import type { McpServerSummary } from "@/hooks/useAgents";

/** Compact pill row listing MCP servers attached to an agent. */
export function McpServerList({ servers }: { servers: McpServerSummary[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {servers.map((srv) => (
        <span
          key={srv.name}
          title={srv.description ?? srv.name}
          className="flex items-center gap-0.5 rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
        >
          <ServerIcon className="size-2.5 shrink-0" />
          {srv.name}
        </span>
      ))}
    </div>
  );
}