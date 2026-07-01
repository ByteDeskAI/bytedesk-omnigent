import { useState } from "react";
import { PlusIcon, ShieldCheckIcon, TrashIcon } from "lucide-react";
import {
  usePolicies,
  usePolicyRegistry,
  useDeletePolicy,
} from "@/hooks/usePolicies";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { SectionLabel } from "./SectionLabel";
import { AddPolicyDialog } from "./AddPolicyDialog";

export function SessionPoliciesSection({ sessionId }: { sessionId: string }) {
  const { data: sessionPolicies = [] } = usePolicies(sessionId);
  const { data: registry = [] } = usePolicyRegistry();
  const deletePolicy = useDeletePolicy(sessionId);
  const [addOpen, setAddOpen] = useState(false);

  const userPolicies = sessionPolicies.filter((p) => p.source === "session");
  const registryByHandler = new Map(registry.map((r) => [r.handler, r]));
  const appliedHandlers = new Set(
    sessionPolicies.map((p) => p.handler).filter((h): h is string => h != null),
  );

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <SectionLabel>Policies</SectionLabel>
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          className="rounded p-0.5 hover:bg-muted"
          title="Add policy"
        >
          <PlusIcon className="size-3 text-muted-foreground" />
        </button>
      </div>
      {userPolicies.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {userPolicies.map((p) => {
            const description =
              p.description ??
              (p.handler ? registryByHandler.get(p.handler)?.description : undefined);
            return (
              <Popover key={p.id ?? p.name}>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="flex cursor-pointer items-center gap-0.5 rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:bg-muted/80"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <ShieldCheckIcon className="size-2.5 shrink-0" />
                    {p.name}
                  </button>
                </PopoverTrigger>
                <PopoverContent
                  side="top"
                  align="start"
                  className="w-64"
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-1.5">
                      <ShieldCheckIcon className="size-3.5 text-muted-foreground" />
                      <span className="font-medium text-sm">{p.name}</span>
                    </div>
                    {description && <p className="text-xs text-muted-foreground">{description}</p>}
                    <button
                      type="button"
                      onClick={() => p.id && deletePolicy.mutate(p.id)}
                      className="flex items-center gap-1 self-end rounded px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                    >
                      <TrashIcon className="size-3" />
                      Remove
                    </button>
                  </div>
                </PopoverContent>
              </Popover>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">No policies added</p>
      )}
      <AddPolicyDialog
        sessionId={sessionId}
        registry={registry}
        appliedHandlers={appliedHandlers}
        open={addOpen}
        onOpenChange={setAddOpen}
      />
    </div>
  );
}
