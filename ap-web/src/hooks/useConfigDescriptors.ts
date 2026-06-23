/**
 * Read-side hooks for the omnigent Configuration Control Plane (ADR-0150).
 *
 * The admin page is a generic, schema-driven consumer: it reads the
 * self-describing catalog (`GET /v1/config/descriptors`) and the current
 * value of each key (`GET /v1/config/values/{key}`). Secrets come back as
 * name+presence only — the value endpoint never returns the secret itself.
 */

import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** One configurable property, fully described (mirrors the backend `_serialize`). */
export interface ConfigDescriptor {
  key: string;
  scope: string;
  what: string;
  json_schema: Record<string, unknown>;
  tier: number;
  sensitivity: "public" | "secret";
  effect_timing: string;
  storage_source: string;
  floor: Record<string, unknown> | null;
  change_event: string | null;
  writable: boolean;
  read_only_reason: string | null;
}

/** A secret value is exposed as name+presence only — never the secret. */
export interface SecretValue {
  name: string;
  present: boolean;
  source: string;
}

export interface ConfigValue {
  key: string;
  value: unknown;
  etag: string | null;
  source: string;
  writable: boolean;
  read_only_reason: string | null;
}

export function isSecretValue(value: unknown): value is SecretValue {
  return (
    typeof value === "object" &&
    value !== null &&
    "present" in value &&
    "name" in value
  );
}

async function fetchDescriptors(): Promise<ConfigDescriptor[]> {
  const res = await authenticatedFetch("/v1/config/descriptors");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { data: ConfigDescriptor[] };
  return body.data;
}

async function fetchValue(key: string): Promise<ConfigValue> {
  const res = await authenticatedFetch(
    `/v1/config/values/${encodeURIComponent(key)}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as ConfigValue;
}

export function useConfigDescriptors() {
  return useQuery({
    queryKey: ["config-descriptors"],
    queryFn: fetchDescriptors,
    staleTime: 5_000,
  });
}

export function useConfigValue(key: string) {
  return useQuery({
    queryKey: ["config-value", key],
    queryFn: () => fetchValue(key),
    staleTime: 5_000,
    enabled: !!key,
  });
}
