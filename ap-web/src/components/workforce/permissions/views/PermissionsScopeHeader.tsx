import { ChevronRightIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { PermissionsTabState } from "../usePermissionsTab";

export function PermissionsScopeHeader({
  editable,
  department,
  departmentScopeId,
  scopeKind,
  setScopeKind,
  scopeSummary,
  scope,
  scopes,
}: Pick<
  PermissionsTabState,
  | "editable"
  | "department"
  | "departmentScopeId"
  | "scopeKind"
  | "setScopeKind"
  | "scopeSummary"
  | "scope"
  | "scopes"
>) {
  return (
    <section className="mc-surface flex flex-wrap items-center justify-between gap-3 p-3">
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setScopeKind("organization")}
          disabled={!editable}
          className={cn(
            "mc-label rounded-full px-2.5 py-1.5 transition-colors disabled:opacity-50",
            scopeKind === "organization"
              ? "bg-accent-orange/15 text-accent-orange"
              : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
          )}
        >
          Organization
        </button>
        {departmentScopeId && (
          <>
            <ChevronRightIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
            <button
              type="button"
              onClick={() => setScopeKind("department")}
              disabled={!editable}
              className={cn(
                "mc-label rounded-full px-2.5 py-1.5 transition-colors disabled:opacity-50",
                scopeKind === "department"
                  ? "bg-accent-orange/15 text-accent-orange"
                  : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
              )}
            >
              {department}
            </button>
          </>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="mc-value">
          {scopeSummary?.agentIds.length ?? 0} agents
        </Badge>
        <Badge variant="outline" className="mc-value">
          rev {scope.data?.revision ?? scopes.data?.revision ?? "-"}
        </Badge>
      </div>
    </section>
  );
}