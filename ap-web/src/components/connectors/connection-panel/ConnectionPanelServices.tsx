import { Switch } from "@/components/ui/switch";
import type { useSetConnectorServiceEnabled } from "@/hooks/useConnectors";
import type { groupConnectionServices } from "../connectors-utils";

type ServiceGroups = ReturnType<typeof groupConnectionServices>;
type ToggleMutation = ReturnType<typeof useSetConnectorServiceEnabled>;

export function ConnectionPanelServices({
  connectionId,
  serviceGroups,
  toggle,
}: {
  connectionId: string;
  serviceGroups: ServiceGroups;
  toggle: ToggleMutation;
}) {
  if (serviceGroups.length === 0) return null;

  return (
    <section className="mt-4">
      <h4 className="mb-2 text-xs font-medium text-muted-foreground">Services</h4>
      <div className="space-y-3">
        {serviceGroups.map((group) => (
          <div key={group.key} className="rounded-md border border-border/70 p-3">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-medium">{group.name}</div>
              <div className="text-[11px] text-muted-foreground">
                {group.services.length} services
              </div>
            </div>
            <div className="grid gap-2 md:grid-cols-2">
              {group.services.map(({ definition, state }) => (
                <div
                  key={state.serviceKey}
                  className="flex items-center justify-between gap-3 rounded-md border border-border/60 px-3 py-2"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{definition.name}</div>
                    <div className="text-[11px] text-muted-foreground">
                      {state.status} · {definition.tools.length} actions
                    </div>
                  </div>
                  <Switch
                    size="sm"
                    checked={state.enabled}
                    onCheckedChange={(enabled) =>
                      toggle.mutate({
                        connectionId,
                        serviceKey: state.serviceKey,
                        enabled,
                      })
                    }
                    aria-label={`Toggle ${definition.name}`}
                  />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}