import type { ConnectorConnection } from "@/lib/connectorsApi";

export function ConnectionSelector({
  connections,
  selectedConnectionId,
  onChange,
}: {
  connections: ConnectorConnection[];
  selectedConnectionId: string;
  onChange: (connectionId: string) => void;
}) {
  if (connections.length < 2) return null;
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <label className="text-sm font-medium" htmlFor="connector-connection">
        Connection
      </label>
      <select
        id="connector-connection"
        className="h-8 min-w-56 rounded-md border border-input bg-background px-2 text-sm"
        value={selectedConnectionId}
        onChange={(event) => onChange(event.target.value)}
      >
        {connections.map((connection) => (
          <option key={connection.id} value={connection.id}>
            {connection.displayName}
          </option>
        ))}
      </select>
    </div>
  );
}