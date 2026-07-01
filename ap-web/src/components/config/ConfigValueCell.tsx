import { isSecretValue, useConfigValue, type ConfigDescriptor } from "@/hooks/useConfigDescriptors";

export function ConfigValueCell({ descriptor }: { descriptor: ConfigDescriptor }) {
  const { data, isLoading, isError, error } = useConfigValue(descriptor.key);

  if (isLoading) {
    return <span className="text-xs text-muted-foreground/70">Loading…</span>;
  }
  if (isError) {
    return (
      <span className="text-xs text-destructive">
        {error instanceof Error ? error.message : "Failed to read"}
      </span>
    );
  }
  const value = data?.value;
  if (isSecretValue(value)) {
    return (
      <span className="font-mono text-xs text-muted-foreground">
        secret · {value.present ? "present" : "absent"}{" "}
        <span className="text-muted-foreground/60">({value.source})</span>
      </span>
    );
  }
  return (
    <span className="break-all font-mono text-xs text-foreground/80">
      {value === null || value === undefined
        ? "—"
        : typeof value === "string"
          ? value
          : JSON.stringify(value)}
    </span>
  );
}