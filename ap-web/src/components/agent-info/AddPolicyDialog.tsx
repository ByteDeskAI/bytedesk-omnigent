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
  const schema = entry?.params_schema as
    | {
        properties?: Record<
          string,
          {
            type?: string;
            description?: string;
            default?: unknown;
            enum?: string[];
            items?: { type?: string; enum?: string[] };
            uniqueItems?: boolean;
          }
        >;
        required?: string[];
      }
    | null
    | undefined;
  const properties = schema?.properties ?? {};
  const paramKeys = Object.keys(properties);

  function handleSelect(handler: string) {
    setSelected(handler);
    setFilter("");
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
    // Always send factory_params for factory-kind policies (even
    // if empty) so the stored entity has ``factory_params={}``
    // instead of ``None``. The builder uses ``arguments is not
    // None`` to distinguish factory form (invoke with kwargs)
    // from direct-callable form (use as-is). Without this,
    // factories like ``deny_pii_in_llm_request`` are called as
    // ``factory(event)`` instead of ``factory()(event)``.
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
          setSelected("");
          setFactoryParams({});
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
          {!selected &&
            (() => {
              const available = registry.filter((r) => !appliedHandlers.has(r.handler));
              const lowerFilter = filter.toLowerCase();
              const filtered = lowerFilter
                ? available.filter(
                    (r) =>
                      r.name.toLowerCase().includes(lowerFilter) ||
                      r.description?.toLowerCase().includes(lowerFilter),
                  )
                : available;
              return (
                <>
                  <input
                    type="text"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    placeholder="Filter policies..."
                    className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
                    // eslint-disable-next-line jsx-a11y/no-autofocus
                    autoFocus
                  />
                  <div className="flex max-h-52 flex-col divide-y divide-border overflow-y-auto rounded border border-border">
                    {filtered.map((r) => (
                      <button
                        key={r.handler}
                        type="button"
                        onClick={() => handleSelect(r.handler)}
                        className="flex flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-muted"
                      >
                        <span className="text-sm">{r.name}</span>
                        {r.description && (
                          <span className="line-clamp-2 text-[11px] text-muted-foreground">
                            {r.description}
                          </span>
                        )}
                      </button>
                    ))}
                    {filtered.length === 0 && (
                      <p className="py-2 text-center text-xs text-muted-foreground">
                        {available.length === 0
                          ? "All available policies are already applied."
                          : "No policies match your filter."}
                      </p>
                    )}
                  </div>
                </>
              );
            })()}
          {entry && (
            <div className="flex flex-col gap-1 rounded border border-border bg-muted/50 px-2.5 py-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{entry.name}</span>
                <button
                  type="button"
                  onClick={() => {
                    setSelected("");
                    setFactoryParams({});
                    setParamError(null);
                  }}
                  className="text-[11px] text-muted-foreground hover:text-foreground"
                >
                  Change
                </button>
              </div>
              {entry.description && (
                <p className="text-xs text-muted-foreground">{entry.description}</p>
              )}
            </div>
          )}
          {entry?.kind === "factory" && paramKeys.length > 0 && (
            <div className="space-y-2">
              {paramKeys.map((key) => {
                const prop = properties[key];
                return (
                  <div key={key}>
                    <label className="flex items-center gap-1 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">{key}</span>
                      {prop?.type && (
                        <span>
                          (
                          {prop.type === "array" && prop.items?.enum
                            ? "select"
                            : prop.type === "array"
                              ? "comma-separated"
                              : prop.type}
                          )
                        </span>
                      )}
                    </label>
                    {prop?.description && (
                      <p className="text-[11px] text-muted-foreground">{prop.description}</p>
                    )}
                    {prop?.type === "boolean" ? (
                      <select
                        value={
                          factoryParams[key] ??
                          (prop?.default !== undefined ? String(prop.default) : "")
                        }
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      >
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    ) : prop?.type === "string" && prop.enum ? (
                      <select
                        value={
                          factoryParams[key] ??
                          (prop?.default !== undefined
                            ? String(prop.default)
                            : (prop.enum[0] ?? ""))
                        }
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      >
                        {prop.enum.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </select>
                    ) : prop?.type === "array" && prop.items?.enum ? (
                      <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-1">
                        {prop.items.enum.map((v) => {
                          const current = factoryParams[key]
                            ? factoryParams[key].split(",").filter(Boolean)
                            : Array.isArray(prop?.default)
                              ? (prop.default as string[])
                              : [];
                          const checked = current.includes(v);
                          return (
                            <label key={v} className="flex items-center gap-1 text-sm">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={(e) => {
                                  const next = e.target.checked
                                    ? [...current, v]
                                    : current.filter((x) => x !== v);
                                  setFactoryParams((prev) => ({
                                    ...prev,
                                    [key]: next.join(","),
                                  }));
                                }}
                                className="rounded border-border"
                              />
                              <span>{v}</span>
                            </label>
                          );
                        })}
                      </div>
                    ) : (
                      <input
                        type={
                          prop?.type === "integer" || prop?.type === "number" ? "number" : "text"
                        }
                        placeholder={
                          prop?.type === "array"
                            ? prop?.default !== undefined
                              ? (prop.default as string[]).join(", ")
                              : "comma-separated values"
                            : prop?.default !== undefined
                              ? String(prop.default)
                              : ""
                        }
                        value={factoryParams[key] ?? ""}
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      />
                    )}
                  </div>
                );
              })}
            </div>
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
