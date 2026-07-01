import { LayersIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { GoalTemplate } from "@/lib/goalsApi";

export function TemplatesPanel({
  templates,
  isLoading,
  isError,
  busy,
  scopeLabel,
  onInstantiate,
}: {
  templates: GoalTemplate[];
  isLoading: boolean;
  isError: boolean;
  busy: boolean;
  scopeLabel: string;
  onInstantiate: (template: GoalTemplate) => void;
}) {
  return (
    <div className="rounded-md border border-border">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <LayersIcon className="size-4 text-muted-foreground" />
        <h2 className="text-sm font-semibold">Templates</h2>
      </div>
      <div className="space-y-2 p-3">
        {isLoading ? (
          <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
            Loading templates…
          </div>
        ) : isError ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            Unable to load templates.
          </div>
        ) : templates.length === 0 ? (
          <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
            No templates yet.
          </div>
        ) : (
          templates.map((template) => (
            <div
              key={template.id}
              className="flex items-center justify-between gap-2 rounded-md border border-border p-2"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{template.name}</p>
                {template.description && (
                  <p className="truncate text-xs text-muted-foreground">{template.description}</p>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                title={`Instantiate into ${scopeLabel}`}
                onClick={() => onInstantiate(template)}
              >
                <PlusIcon /> Use
              </Button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}