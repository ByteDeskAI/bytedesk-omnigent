/**
 * Verdict submitter — same signature as `chatStore.submitApproval`.
 * Injectable so surfaces outside the active chat (the Inbox page)
 * can route the verdict to the owning session themselves.
 */
export type SubmitApprovalFn = (
  elicitationId: string,
  action: "accept" | "decline",
  content?: Record<string, unknown>,
) => void;

export interface ApprovalCardProps {
  elicitationId: string;
  message: string;
  phase: string;
  policyName: string;
  contentPreview: string;
  requestedSchema: Record<string, unknown>;
  /**
   * Standalone approval page URL when the elicitation uses URL mode.
   * When present, the pending card renders a link to the approval page
   * instead of inline approve/reject buttons.
   */
  url?: string | null;
  status: "pending" | "responded";
  response: {
    action: "accept" | "decline" | "cancel" | "auto_resolved";
    content?: Record<string, unknown>;
  } | null;
  /**
   * Structured AskUserQuestion payload — set when the server-side
   * PermissionRequest endpoint detected the gated tool is
   * AskUserQuestion. Carries the FULL question + options structure
   * (not truncated like `contentPreview`). Optional/null for
   * other elicitations.
   */
  askUserQuestion?: Record<string, unknown> | null;
  /**
   * Full ExitPlanMode tool_input (untruncated) — set when the
   * server-side PermissionRequest endpoint detected the gated tool
   * is ExitPlanMode. The card renders `plan` as markdown with
   * plan-review actions. Optional/null for other elicitations.
   */
  exitPlanMode?: Record<string, unknown> | null;
  /**
   * Structured Codex command approval details. When present, the
   * card renders command metadata instead of the raw JSON preview.
   */
  codexCommand?: {
    command: string;
    cwd: string | null;
    reason: string | null;
    execPolicyAmendment: string[] | null;
  } | null;
  /**
   * Claude-native edit-tool prompts only: when true, the binary
   * approve/reject card grows a third "Accept & allow all edits"
   * button. Accepting through it asks the server to switch the
   * session into Claude Code's ``acceptEdits`` mode (the web
   * equivalent of the native shift+tab toggle). Absent/false for
   * every other elicitation, so the button never renders where the
   * mode switch would be a no-op.
   */
  allowAllEdits?: boolean;
  /**
   * Verdict submitter override. Defaults to `chatStore.submitApproval`
   * (the in-chat path: optimistic block flip + resolve POST + rollback).
   * The Inbox page passes its own handler because its cards belong to
   * sessions other than the chat store's active one.
   */
  onSubmit?: SubmitApprovalFn;
}

export interface ApprovalHandlers {
  submitBinary: (action: "accept" | "decline") => void;
  submitOption: (label: string) => void;
  submitAnswers: (answers: Record<string, string | string[]>) => void;
  submitExecPolicyAmendment: (amendment: string[]) => void;
  submitAllowAllEdits: () => void;
  submitPlanRejection: (feedback: string) => void;
}

export interface ApprovalMode {
  isExitPlanMode: boolean;
  exitPlanModePlan: string | null;
  isAskUserQuestion: boolean;
  isMultiChoice: boolean;
  isCodexCommandApproval: boolean;
  isExternalUrl: boolean;
  askUserQuestionTitle: string;
  formattedPreview: string;
  optionLabels: string[];
  execPolicyAmendment: string[] | null;
}