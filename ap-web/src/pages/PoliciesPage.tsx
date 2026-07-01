/**
 * Admin default-policies management page (``/policies``).
 *
 * Lists every global default policy and lets admins add, toggle,
 * and remove them. The add-policy dialog reuses the same registry-
 * driven picker as the per-session policy UI in AgentInfo.
 *
 * Gated on the client by an early admin check (non-admins see a
 * "no permission" message) AND on the server by the route handlers
 * themselves — client-side gating is just UX.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "@/lib/routing";
import {
  useDefaultPolicies,
  useUpdateDefaultPolicy,
  useDeleteDefaultPolicy,
  type DefaultPolicy,
} from "@/hooks/useDefaultPolicies";
import { usePolicyRegistry } from "@/hooks/usePolicies";
import { getMe } from "@/lib/accountsApi";
import { PoliciesPageShell } from "./organisms/PoliciesPageShell";

export function PoliciesPage() {
  const navigate = useNavigate();
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const { data: policies = [], refetch } = useDefaultPolicies();
  const { data: registry = [] } = usePolicyRegistry();
  const updatePolicy = useUpdateDefaultPolicy();
  const deletePolicy = useDeleteDefaultPolicy();
  const [addOpen, setAddOpen] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<DefaultPolicy | null>(null);
  const [pendingAction, setPendingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const registryByHandler = useMemo(
    () => new Map(registry.map((r) => [r.handler, r])),
    [registry],
  );
  const appliedHandlers = useMemo(() => new Set(policies.map((p) => p.handler)), [policies]);

  const refresh = useCallback(() => {
    void refetch();
  }, [refetch]);

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

  async function onConfirmDelete() {
    if (deleteCandidate === null) return;
    setPendingAction(true);
    setActionError(null);
    deletePolicy.mutate(deleteCandidate.id, {
      onSuccess: () => {
        setPendingAction(false);
        setDeleteCandidate(null);
      },
      onError: (err) => {
        setPendingAction(false);
        setActionError(err.message);
      },
    });
  }

  return (
    <PoliciesPageShell
      meIsAdmin={meIsAdmin}
      policies={policies}
      registry={registry}
      registryByHandler={registryByHandler}
      appliedHandlers={appliedHandlers}
      addOpen={addOpen}
      setAddOpen={setAddOpen}
      deleteCandidate={deleteCandidate}
      setDeleteCandidate={setDeleteCandidate}
      pendingAction={pendingAction}
      actionError={actionError}
      setActionError={setActionError}
      onRefresh={refresh}
      onTogglePolicy={(policyId, enabled) =>
        updatePolicy.mutate({ policyId, enabled })
      }
      onConfirmDelete={() => void onConfirmDelete()}
    />
  );
}