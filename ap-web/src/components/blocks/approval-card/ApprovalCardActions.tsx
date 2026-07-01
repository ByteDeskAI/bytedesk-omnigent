import { CheckIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export function BinaryApprovalActions({
  allowAllEdits,
  onAccept,
  onDecline,
  onAcceptAllowAllEdits,
}: {
  allowAllEdits?: boolean;
  onAccept: () => void;
  onDecline: () => void;
  onAcceptAllowAllEdits: () => void;
}) {
  return (
    <div className="flex flex-wrap gap-2 pt-1">
      <Button size="sm" onClick={onAccept}>
        <CheckIcon className="mr-1 size-3.5" />
        Approve
      </Button>
      {allowAllEdits && (
        <Button size="sm" variant="outline" onClick={onAcceptAllowAllEdits}>
          <CheckIcon className="mr-1 size-3.5" />
          Accept & allow all edits
        </Button>
      )}
      <Button size="sm" variant="outline" onClick={onDecline}>
        <XIcon className="mr-1 size-3.5" />
        Reject
      </Button>
    </div>
  );
}

export function CodexCommandActions({
  execPolicyAmendment,
  onAccept,
  onDecline,
  onApproveAndRemember,
}: {
  execPolicyAmendment: string[] | null;
  onAccept: () => void;
  onDecline: () => void;
  onApproveAndRemember: (amendment: string[]) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 pt-1" data-testid="codex-command-actions">
      <Button size="sm" onClick={onAccept}>
        <CheckIcon className="mr-1 size-3.5" />
        Approve
      </Button>
      {execPolicyAmendment && (
        <Button
          size="sm"
          variant="outline"
          onClick={() => onApproveAndRemember(execPolicyAmendment)}
        >
          <CheckIcon className="mr-1 size-3.5" />
          Approve and remember
        </Button>
      )}
      <Button size="sm" variant="outline" onClick={onDecline}>
        <XIcon className="mr-1 size-3.5" />
        Reject
      </Button>
    </div>
  );
}