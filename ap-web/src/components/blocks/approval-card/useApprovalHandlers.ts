import { useChatStore } from "@/store/chatStore";
import type { AskUserQuestionAnswers } from "../ask-user-question-form";
import type { ApprovalCardProps, ApprovalHandlers, SubmitApprovalFn } from "./types";

export function useApprovalHandlers(
  elicitationId: string,
  onSubmit?: ApprovalCardProps["onSubmit"],
): ApprovalHandlers {
  const submit: SubmitApprovalFn =
    onSubmit ??
    ((id, action, content) => {
      void useChatStore.getState().submitApproval(id, action, content);
    });

  return {
    submitBinary: (action) => {
      submit(elicitationId, action);
    },
    submitOption: (label) => {
      submit(elicitationId, "accept", { answer: label });
    },
    submitAnswers: (answers: AskUserQuestionAnswers) => {
      submit(elicitationId, "accept", answers);
    },
    submitExecPolicyAmendment: (amendment) => {
      submit(elicitationId, "accept", { execpolicy_amendment: amendment });
    },
    submitAllowAllEdits: () => {
      submit(elicitationId, "accept", { allow_all_edits: true });
    },
    submitPlanRejection: (feedback) => {
      const trimmed = feedback.trim();
      submit(elicitationId, "decline", trimmed ? { feedback: trimmed } : undefined);
    },
  };
}