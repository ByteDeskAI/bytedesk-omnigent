import { useCallback, useEffect, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import type { ApprovePageState, ElicitationData } from "./approve-types";

export function useApprovePage(sessionId: string | undefined, elicitationId: string | undefined) {
  const [state, setState] = useState<ApprovePageState>({ kind: "loading" });

  useEffect(() => {
    if (!sessionId || !elicitationId) {
      setState({ kind: "error", message: "Missing session or elicitation ID" });
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const res = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}`,
        );
        if (cancelled) return;
        if (!res.ok) {
          setState({ kind: "error", message: `Server error: ${res.status}` });
          return;
        }
        const data: ElicitationData = await res.json();
        if (data.status === "resolved") {
          setState({ kind: "resolved" });
        } else {
          setState({ kind: "pending", data });
        }
      } catch (err) {
        if (!cancelled) {
          setState({ kind: "error", message: `Failed to load: ${String(err)}` });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, elicitationId]);

  const submit = useCallback(
    (action: "accept" | "decline") => {
      if (!sessionId || !elicitationId) return;
      setState({ kind: "submitted", action });
      void (async () => {
        try {
          const res = await authenticatedFetch(
            `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}/resolve`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action }),
            },
          );
          if (!res.ok) {
            setState({ kind: "error", message: `Resolve failed: ${res.status}` });
          }
        } catch (err) {
          setState({ kind: "error", message: `Network error: ${String(err)}` });
        }
      })();
    },
    [sessionId, elicitationId],
  );

  return { state, submit };
}