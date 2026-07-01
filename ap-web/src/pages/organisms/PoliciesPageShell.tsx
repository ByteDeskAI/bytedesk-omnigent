import { PlusIcon, RefreshCwIcon, ShieldCheckIcon, TrashIcon } from "lucide-react";
import { AddDefaultPolicyDialog } from "@/components/policies";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import type { DefaultPolicy } from "@/hooks/useDefaultPolicies";
import type { PolicyRegistryEntry } from "@/hooks/usePolicies";

export interface PoliciesPageShellProps {
  meIsAdmin: boolean | null;
  policies: DefaultPolicy[];
  registry: PolicyRegistryEntry[];
  registryByHandler: Map<string, PolicyRegistryEntry>;
  appliedHandlers: Set<string>;
  addOpen: boolean;
  setAddOpen: (open: boolean) => void;
  deleteCandidate: DefaultPolicy | null;
  setDeleteCandidate: (policy: DefaultPolicy | null) => void;
  pendingAction: boolean;
  actionError: string | null;
  setActionError: (error: string | null) => void;
  onRefresh: () => void;
  onTogglePolicy: (policyId: string, enabled: boolean) => void;
  onConfirmDelete: () => void;
}

export function PoliciesPageShell({
  meIsAdmin,
  policies,
  registry,
  registryByHandler,
  appliedHandlers,
  addOpen,
  setAddOpen,
  deleteCandidate,
  setDeleteCandidate,
  pendingAction,
  actionError,
  setActionError,
  onRefresh,
  onTogglePolicy,
  onConfirmDelete,
}: PoliciesPageShellProps) {
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
        <h1 className="mb-2 text-2xl font-semibold">Global Policies</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to manage global policies.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8 pt-14">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Global Policies</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Global policies applied to all sessions.
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)}>
          <PlusIcon /> Add policy
        </Button>
      </div>

      {policies.length > 0 && (
        <div className="flex flex-col gap-3">
          {policies.map((p) => {
            const registryEntry = registryByHandler.get(p.handler);
            const params = p.factory_params;
            const hasParams = params != null && Object.keys(params).length > 0;
            return (
              <div key={p.id} className="rounded-lg border border-border bg-background p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-2.5 min-w-0">
                    <ShieldCheckIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{p.name}</span>
                        {!p.enabled && (
                          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                            Disabled
                          </span>
                        )}
                      </div>
                      {registryEntry?.description && (
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {registryEntry.description}
                        </p>
                      )}
                      <code className="mt-1 block text-[11px] text-muted-foreground/70">
                        {p.handler}
                      </code>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Switch
                      checked={p.enabled}
                      onCheckedChange={(checked) => onTogglePolicy(p.id, checked)}
                      aria-label={`Toggle ${p.name}`}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-8 text-muted-foreground hover:text-destructive"
                      title="Remove policy"
                      onClick={() => setDeleteCandidate(p)}
                      disabled={pendingAction}
                    >
                      <TrashIcon className="size-3.5" />
                    </Button>
                  </div>
                </div>
                {hasParams && (
                  <div className="ml-6.5 mt-2 rounded-md border border-border/60 bg-muted/40 px-3 py-2">
                    <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
                      Parameters
                    </span>
                    <div className="mt-1 flex flex-col gap-0.5">
                      {Object.entries(params).map(([key, value]) => (
                        <div key={key} className="flex items-baseline gap-1.5 text-xs">
                          <span className="font-medium text-foreground/80">{key}:</span>
                          <span className="text-muted-foreground">
                            {Array.isArray(value) ? value.join(", ") : String(value)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {policies.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No global policies configured. Add one to apply it to all sessions.
        </p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={onRefresh}>
          <RefreshCwIcon /> Refresh
        </Button>
      </div>

      <AddDefaultPolicyDialog
        registry={registry}
        appliedHandlers={appliedHandlers}
        open={addOpen}
        onOpenChange={setAddOpen}
      />

      <Dialog
        open={deleteCandidate !== null}
        onOpenChange={(open) => {
          if (pendingAction) return;
          if (!open) {
            setDeleteCandidate(null);
            setActionError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove {deleteCandidate?.name}?</DialogTitle>
            <DialogDescription>
              This removes the global policy from all sessions. Existing session-level policies with
              the same handler are unaffected.
            </DialogDescription>
          </DialogHeader>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setDeleteCandidate(null)}
              disabled={pendingAction}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => void onConfirmDelete()}
              disabled={pendingAction}
            >
              {pendingAction ? "Removing..." : "Remove"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}