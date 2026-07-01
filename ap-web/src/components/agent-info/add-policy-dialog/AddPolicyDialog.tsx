import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAddPolicy, type PolicyRegistryEntry } from "@/hooks/usePolicies";
import { coercePolicyParams } from "@/lib/policyParams";
import {
  PolicyHandlerPicker,
  PolicyParamFields,
  PolicySelectedEntry,
  type PolicyParamsSchema,
} from "@/components/policies/policy-dialog";

export function AddPolicyDialog({
  sessionId,
  registry,
  appliedHandlers,
  open,
  onOpenChange,
}: {
  sessionId: string;
  registry: PolicyRegistryEntry[];
  appliedHandlers: Set<string>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [selected, setSelected] = useState<string>("");
  const [filter, setFilter] = useState("");
  const [factoryParams, setFactoryParams] = useState<Record<string, string>>({});
  const [paramError, setParamError] = useState<string | null>(null);
  const addPolicy = useAddPolicy(sessionId);

  const entry = registry.find((r) => r.handler === selected);
  const schema = entry?.params_schema as PolicyParamsSchema | null | undefined;
  const properties = schema?.properties ?? {};
  const paramKeys = Object.keys(properties);

  function handleSelect(handler: string) {
    setSelected(handler);
    setFilter("");
    setFactoryParams({});
    setParamError(null);
  }

  function clearSelection() {
    setSelected("");
    setFactoryParams({});
    setParamError(null);
  }

  function handleAdd() {
    if (!entry) return;
    let parsedParams: Record<string, unknown> | undefined;
    if (entry.kind === "factory" && paramKeys.length > 0) {
      const result = coercePolicyParams(paramKeys, properties, factoryParams);
      if (!result.ok) {
        setParamError(result.error);
        return;
      }
      parsedParams = result.params;
    }
    setParamError(null);
    const includeFactoryParams =
      entry.kind === "factory" ? { factory_params: parsedParams ?? {} } : {};
    addPolicy.mutate(
      {
        name: entry.name.toLowerCase().replace(/\s+/g, "_"),
        type: "python",
        handler: entry.handler,
        ...includeFactoryParams,
      },
      {
        onSuccess: () => {
          clearSelection();
          onOpenChange(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add Policy</DialogTitle>
          <DialogDescription>Choose a policy to apply to this session.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3 pt-1">
          {!selected && (
            <PolicyHandlerPicker
              registry={registry}
              appliedHandlers={appliedHandlers}
              filter={filter}
              onFilterChange={setFilter}
              onSelect={handleSelect}
              emptyAllMessage="All available policies are already applied."
            />
          )}
          {entry && <PolicySelectedEntry entry={entry} onClear={clearSelection} />}
          {entry?.kind === "factory" && paramKeys.length > 0 && (
            <PolicyParamFields
              paramKeys={paramKeys}
              properties={properties}
              factoryParams={factoryParams}
              onFactoryParamsChange={setFactoryParams}
            />
          )}
          {(paramError || addPolicy.isError) && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {paramError ?? addPolicy.error?.message}
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="rounded px-3 py-1.5 text-xs hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleAdd}
              disabled={!selected || addPolicy.isPending}
              className="rounded bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
            >
              {addPolicy.isPending ? "Adding..." : "Add"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}