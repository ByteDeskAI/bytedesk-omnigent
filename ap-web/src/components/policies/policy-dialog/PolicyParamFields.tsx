import type { PolicyParamFieldsProps } from "./types";

export function PolicyParamFields({
  paramKeys,
  properties,
  factoryParams,
  onFactoryParamsChange,
}: PolicyParamFieldsProps) {
  return (
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
                value={factoryParams[key] ?? (prop?.default !== undefined ? String(prop.default) : "")}
                onChange={(e) =>
                  onFactoryParamsChange((prev) => ({ ...prev, [key]: e.target.value }))
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
                  (prop?.default !== undefined ? String(prop.default) : (prop.enum[0] ?? ""))
                }
                onChange={(e) =>
                  onFactoryParamsChange((prev) => ({ ...prev, [key]: e.target.value }))
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
                          onFactoryParamsChange((prev) => ({ ...prev, [key]: next.join(",") }));
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
                type={prop?.type === "integer" || prop?.type === "number" ? "number" : "text"}
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
                  onFactoryParamsChange((prev) => ({ ...prev, [key]: e.target.value }))
                }
                className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
              />
            )}
          </div>
        );
      })}
    </div>
  );
}