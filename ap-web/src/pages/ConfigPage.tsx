/**
 * Read-only Configuration admin page (``/config``) — ADR-0150, BDP-2416.
 */

import { useEffect, useMemo, useState } from "react";
import { RefreshCwIcon, SettingsIcon } from "lucide-react";
import { DescriptorCard } from "@/components/config";
import { Button } from "@/components/ui/button";
import { useNavigate } from "@/lib/routing";
import { getMe } from "@/lib/accountsApi";
import { useConfigDescriptors } from "@/hooks/useConfigDescriptors";

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
    const byScope = new Map<string, NonNullable<typeof data>>();
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