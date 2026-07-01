// Inline approval / option-picker card rendered when the server
// emits an MCP-shape `response.elicitation_request`.
//
// Render modes (decided in order):
//
//   - **ExitPlanMode plan review** — when the elicitation carries a
//     structured `exitPlanMode` payload (the PermissionRequest
//     endpoint stamps the full tool_input when the gated tool is
//     Claude's built-in ExitPlanMode). Renders the plan markdown
//     plus approve / approve-in-auto-mode / reject-with-feedback
//     actions — see `ExitPlanModeReview`.
//
//   - **AskUserQuestion form** — when the elicitation carries a
//     structured `askUserQuestion` payload (the PermissionRequest
//     endpoint stamps this when the gated tool is Claude's built-in
//     AskUserQuestion). Renders a multi-question form with radio
//     inputs for single-select, checkboxes for multi-select. Submit
//     posts the gathered answers as `content.answers`.
//
//   - **Option buttons** — when `requestedSchema` is
//     `{properties: {answer: {enum: [...]}}}`. Currently no
//     producer emits this for built-in AskUserQuestion, but the
//     branch is kept for future MCP-elicitation flows that ride
//     the same card.
//
//   - **Binary approve/reject** — everything else (policy ASK,
//     PermissionRequest for non-AskUserQuestion tools).
//
// Submit posts through `chatStore.submitApproval`, which:
//   1. optimistically flips the block to "responded" (instant UI),
//   2. calls `approve(targetSessionId, elicitationId, {action, content?})`
//      on `POST /v1/sessions/{id}/elicitations/{eid}/resolve`,
//   3. rolls back to "pending" on network error.

import { deriveApprovalMode } from "./approval-card-utils";
import { ApprovalCardPending } from "./ApprovalCardPending";
import { ApprovalCardResponded } from "./ApprovalCardResponded";
import type { ApprovalCardProps } from "./types";
import { useApprovalHandlers } from "./useApprovalHandlers";

export function ApprovalCard({
  elicitationId,
  message,
  phase,
  policyName,
  contentPreview,
  requestedSchema,
  url,
  status,
  response,
  askUserQuestion,
  exitPlanMode,
  codexCommand,
  allowAllEdits,
  onSubmit,
}: ApprovalCardProps) {
  const handlers = useApprovalHandlers(elicitationId, onSubmit);
  const { askPayload, mode } = deriveApprovalMode({
    elicitationId,
    message,
    phase,
    policyName,
    contentPreview,
    requestedSchema,
    url,
    status,
    response,
    askUserQuestion,
    exitPlanMode,
    codexCommand,
    allowAllEdits,
    onSubmit,
  });

  if (status === "responded" && response) {
    return (
      <ApprovalCardResponded
        message={message}
        policyName={policyName}
        response={response}
        codexCommand={codexCommand}
        mode={mode}
      />
    );
  }

  return (
    <ApprovalCardPending
      message={message}
      phase={phase}
      policyName={policyName}
      url={url}
      codexCommand={codexCommand}
      allowAllEdits={allowAllEdits}
      askPayload={askPayload}
      mode={mode}
      handlers={handlers}
    />
  );
}