import { useNavigate } from "@/lib/routing";
import type { ConnectorManifest } from "@/lib/connectorsApi";

export function ProviderSelector({
  providers,
  provider,
}: {
  providers: ConnectorManifest[];
  provider: ConnectorManifest;
}) {
  const navigate = useNavigate();
  if (providers.length < 2) return null;
  return (
    <select
      className="h-8 min-w-48 rounded-md border border-input bg-background px-2 text-sm"
      value={provider.provider}
      onChange={(event) => navigate(`/connectors/${encodeURIComponent(event.target.value)}`)}
      aria-label="Connector provider"
    >
      {providers.map((item) => (
        <option key={item.provider} value={item.provider}>
          {item.name}
        </option>
      ))}
    </select>
  );
}