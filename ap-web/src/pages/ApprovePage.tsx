/**
 * Standalone approval page for URL-mode elicitations.
 */

import { useParams } from "@/lib/routing";
import { ApprovePageContent, useApprovePage } from "@/components/approve";

export function ApprovePage() {
  const { sessionId, elicitationId } = useParams<{
    sessionId: string;
    elicitationId: string;
  }>();
  const { state, submit } = useApprovePage(sessionId, elicitationId);

  return <ApprovePageContent state={state} onSubmit={(action) => submit(action)} />;
}