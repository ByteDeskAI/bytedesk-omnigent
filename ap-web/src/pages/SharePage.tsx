import { useEffect } from "react";
import { useNavigate, useSearchParams } from "@/lib/routing";
import {
  buildComposerPrefillFromShare,
  hasShareTargetContent,
  parseShareTargetSearch,
} from "@/lib/pwa/shareTarget";

const SHARE_DRAFT_KEY = "omnigent:share-draft";

/** Inbound Web Share Target — stash shared text and open the composer on /. */
export function SharePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();

  useEffect(() => {
    const parsed = parseShareTargetSearch(`?${params.toString()}`);
    if (hasShareTargetContent(parsed)) {
      sessionStorage.setItem(SHARE_DRAFT_KEY, buildComposerPrefillFromShare(parsed));
    }
    navigate("/", { replace: true });
  }, [navigate, params]);

  return null;
}

export function consumeShareDraft(): string | null {
  if (typeof sessionStorage === "undefined") return null;
  const draft = sessionStorage.getItem(SHARE_DRAFT_KEY);
  if (draft) sessionStorage.removeItem(SHARE_DRAFT_KEY);
  return draft;
}