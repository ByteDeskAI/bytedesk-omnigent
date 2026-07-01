import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useCreateConnectorConnection } from "@/hooks/useConnectors";
import type { ConnectorManifest } from "@/lib/connectorsApi";

export function CredentialSetup({ provider }: { provider: ConnectorManifest }) {
  const createConnector = useCreateConnectorConnection();
  const [displayName, setDisplayName] = useState(provider.name);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDisplayName(provider.name);
    setValues({});
    setError(null);
  }, [provider.provider, provider.name]);

  async function submit() {
    setError(null);
    const metadata: Record<string, unknown> = {};
    const secretPayload: Record<string, unknown> = {};
    for (const field of provider.auth.setupFields) {
      const raw = values[field.key]?.trim() ?? "";
      if (field.required && !raw) {
        setError(`${field.label} is required.`);
        return;
      }
      if (!raw) continue;
      let value: unknown = raw;
      if (field.input === "json_secret") {
        try {
          value = JSON.parse(raw) as Record<string, unknown>;
        } catch {
          setError(`${field.label} is invalid JSON.`);
          return;
        }
      }
      if (field.target === "secret_payload") {
        secretPayload[field.key] = value;
      } else {
        metadata[field.key] = value;
      }
    }
    await createConnector.mutateAsync({
      provider: provider.provider,
      displayName,
      metadata,
      secretPayload: Object.keys(secretPayload).length > 0 ? secretPayload : undefined,
      enabledServices: provider.services.map((svc) => svc.key),
    });
    setValues({});
  }

  return (
    <div className="rounded-md border border-border bg-muted/20 p-3">
      <div className="grid gap-2 md:grid-cols-2">
        <Input
          aria-label="Connection name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />
        {provider.auth.setupFields
          .filter((field) => field.input === "text")
          .map((field) => (
            <Input
              key={field.key}
              placeholder={field.label}
              value={values[field.key] ?? ""}
              onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
            />
          ))}
      </div>
      {provider.auth.setupFields
        .filter((field) => field.input === "json_secret")
        .map((field) => (
          <Textarea
            key={field.key}
            className="mt-2 min-h-28 font-mono text-xs"
            placeholder={field.label}
            value={values[field.key] ?? ""}
            onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
          />
        ))}
      {error && <div className="mt-2 text-xs text-destructive">{error}</div>}
      {createConnector.isError && (
        <div className="mt-2 text-xs text-destructive">
          {createConnector.error instanceof Error
            ? createConnector.error.message
            : "Connection failed"}
        </div>
      )}
      <div className="mt-3 flex justify-end">
        <Button
          size="sm"
          onClick={() => void submit()}
          disabled={!displayName || createConnector.isPending}
        >
          Connect
        </Button>
      </div>
    </div>
  );
}