/**
 * Read-only Configuration admin page (``/config``) — ADR-0150, BDP-2416.
 *
 * The reference consumer of the Configuration Control Plane: it renders the
 * whole surface *generically* from the self-describing catalog
 * (`GET /v1/config/descriptors`), grouped by scope, with tier + lock + secret
 * metadata, and shows each current value (`GET /v1/config/values/{key}`).
 * No editing yet — locked keys show their `read_only_reason`; secrets show
 * name+presence only.
 *
 * Gated on the client by an early admin check (non-admins see a "no
 * permission" message) AND on the server by the route handlers themselves —
 * client-side gating is just UX.
 */

import { useEffect, useMemo, useState } from "react";
import { LockIcon, RefreshCwIcon, SettingsIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNavigate } from "@/lib/routing";
import { getMe } from "@/lib/accountsApi";
import {
  isSecretValue,
  useConfigDescriptors,
  useConfigValue,
  type ConfigDescriptor,
} from "@/hooks/useConfigDescriptors";

const TIER_LABELS: Record<number, string> = {
  0: "Locked",
  1: "Floor-guarded",
  2: "Operator-editable",
  3: "Content",
};

/** Renders the current value of one key (its own query, read-only). */
function ConfigValueCell({ descriptor }: { descriptor: ConfigDescriptor }) {
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

function DescriptorCard({ descriptor }: { descriptor: ConfigDescriptor }) {
  const tierLabel = TIER_LABELS[descriptor.tier] ?? `Tier ${descriptor.tier}`;
  return (
    <div className="rounded-lg border border-border bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-medium">{descriptor.key}</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              Tier {descriptor.tier} · {tierLabel}
            </span>
            {descriptor.sensitivity === "secret" && (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                secret
              </span>
            )}
            {descriptor.writable ? (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                editable
              </span>
            ) : (
              <span className="flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                <LockIcon className="size-2.5" /> read-only
              </span>
            )}
          </div>
          {descriptor.what && (
            <p className="mt-1 text-xs text-muted-foreground">{descriptor.what}</p>
          )}
        </div>
      </div>

      <div className="ml-0 mt-2 rounded-md border border-border/60 bg-muted/40 px-3 py-2">
        <div className="flex items-baseline gap-1.5 text-xs">
          <span className="font-medium text-foreground/80">value</span>
          <ConfigValueCell descriptor={descriptor} />
        </div>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground/70">
          <span>storage: {descriptor.storage_source}</span>
          <span>effect: {descriptor.effect_timing}</span>
        </div>
        {!descriptor.writable && descriptor.read_only_reason && (
          <p className="mt-1 text-[11px] text-muted-foreground">
            {descriptor.read_only_reason}
          </p>
        )}
      </div>
    </div>
  );
}

export function ConfigPage() {
  const navigate = useNavigate();
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const { data, isLoading, isError, error, refetch } = useConfigDescriptors();

  useEffect(() => {
    void (async () => {
      const me = await getMe();
      if (me === null) {
        navigate("/login", { replace: true });
        return;
      }
      setMeIsAdmin(me.is_admin);
    })();
  }, [navigate]);

  const grouped = useMemo(() => {
    const byScope = new Map<string, ConfigDescriptor[]>();
    for (const d of data ?? []) {
      const list = byScope.get(d.scope) ?? [];
      list.push(d);
      byScope.set(d.scope, list);
    }
    return [...byScope.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [data]);

  if (meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (meIsAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Configuration</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to view system configuration.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8 pt-14">
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-start gap-2.5">
          <SettingsIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />
          <div>
            <h1 className="text-2xl font-semibold">Configuration</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Every configurable property, described by the system itself.
              Read-only — locked keys explain why; secrets show presence only.
            </p>
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={() => void refetch()}>
          <RefreshCwIcon />
        </Button>
      </div>

      {isError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error instanceof Error ? error.message : "Failed to load configuration."}
        </div>
      )}

      {isLoading && (
        <div className="text-sm text-muted-foreground">Loading configuration…</div>
      )}

      {!isLoading && !isError && grouped.length === 0 && (
        <div className="text-sm text-muted-foreground">
          No configuration descriptors are registered.
        </div>
      )}

      <div className="flex flex-col gap-8">
        {grouped.map(([scope, descriptors]) => (
          <section key={scope}>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {scope} · {descriptors.length}
            </h2>
            <div className="flex flex-col gap-3">
              {descriptors.map((d) => (
                <DescriptorCard key={d.key} descriptor={d} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
