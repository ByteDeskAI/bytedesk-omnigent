// Interactive form for Claude Code's built-in ``AskUserQuestion``
// tool. Rendered inside the existing ``ApprovalCard`` when the
// PermissionRequest carries a structured ``ask_user_question``
// payload — see :file:`@/lib/askUserQuestion`.

import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, XIcon } from "lucide-react";
import { type ChangeEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";
import { AskUserQuestionSection } from "./AskUserQuestionSection";
import { answerForQuestion, questionKey } from "./ask-user-question-form-utils";
import type { AskUserQuestionAnswers } from "./types";

interface AskUserQuestionFormProps {
  questions: ClaudeQuestion[];
  onSubmit: (answers: AskUserQuestionAnswers) => void;
  onReject: () => void;
}

export function AskUserQuestionForm({ questions, onSubmit, onReject }: AskUserQuestionFormProps) {
  const [currentIndex, setCurrentIndex] = useState(0);

  const [selections, setSelections] = useState<Record<string, string | string[]>>(() => {
    const initial: Record<string, string | string[]> = {};
    for (const q of questions) {
      initial[questionKey(q)] = q.multiSelect ? [] : "";
    }
    return initial;
  });

  const [customSelected, setCustomSelected] = useState<Record<string, boolean>>(() => {
    const initial: Record<string, boolean> = {};
    for (const q of questions) initial[questionKey(q)] = false;
    return initial;
  });
  const [customInputs, setCustomInputs] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const q of questions) initial[questionKey(q)] = "";
    return initial;
  });

  const handleSingleSelect = (key: string, label: string) => {
    setSelections((prev) => ({ ...prev, [key]: label }));
    setCustomSelected((prev) => ({ ...prev, [key]: false }));
  };

  const handleCustomToggleSingle = (key: string) => {
    setCustomSelected((prev) => ({ ...prev, [key]: true }));
    setSelections((prev) => ({ ...prev, [key]: "" }));
  };

  const handleMultiToggle = (key: string, label: string) => {
    setSelections((prev) => {
      const current = prev[key];
      const set = new Set(Array.isArray(current) ? current : []);
      if (set.has(label)) {
        set.delete(label);
      } else {
        set.add(label);
      }
      return { ...prev, [key]: Array.from(set) };
    });
  };

  const handleCustomToggleMulti = (key: string) => {
    setCustomSelected((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const handleCustomInput = (key: string, e: ChangeEvent<HTMLTextAreaElement>) => {
    const text = e.target.value;
    setCustomInputs((prev) => ({ ...prev, [key]: text }));
    if (text && !customSelected[key]) {
      const question = questions.find((q) => questionKey(q) === key);
      if (question && !question.multiSelect) {
        handleCustomToggleSingle(key);
      } else {
        setCustomSelected((prev) => ({ ...prev, [key]: true }));
      }
    }
  };

  const allAnswered = questions.every((q) => {
    const key = questionKey(q);
    return (
      answerForQuestion(
        q,
        selections[key] ?? "",
        customSelected[key] ?? false,
        customInputs[key] ?? "",
      ) !== null
    );
  });

  const handleSubmit = () => {
    const finalAnswers: AskUserQuestionAnswers = {};
    for (const q of questions) {
      const key = questionKey(q);
      const answer = answerForQuestion(
        q,
        selections[key] ?? "",
        customSelected[key] ?? false,
        customInputs[key] ?? "",
      );
      if (answer === null) return;
      finalAnswers[key] = answer;
    }
    onSubmit(finalAnswers);
  };

  const current = questions[currentIndex];
  if (!current) return null;
  const currentKey = questionKey(current);
  const isFirst = currentIndex === 0;
  const isLast = currentIndex === questions.length - 1;

  return (
    <div className="flex flex-col gap-2 text-foreground" data-testid="ask-user-question-form">
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span data-testid="ask-user-question-progress">
          Question {currentIndex + 1} of {questions.length}:
        </span>
        {current.header && (
          <span className="text-muted-foreground text-xs rounded bg-muted px-1.5 py-0.5">
            {current.header}
          </span>
        )}
      </div>

      <AskUserQuestionSection
        question={current}
        questionKeyValue={currentKey}
        selection={selections[currentKey] ?? ""}
        customSelected={customSelected[currentKey] ?? false}
        customText={customInputs[currentKey] ?? ""}
        onSingleSelect={handleSingleSelect}
        onCustomToggleSingle={handleCustomToggleSingle}
        onMultiToggle={handleMultiToggle}
        onCustomToggleMulti={handleCustomToggleMulti}
        onCustomInput={handleCustomInput}
      />

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setCurrentIndex((i) => i - 1)}
          disabled={isFirst}
          data-testid="ask-user-question-prev"
        >
          <ChevronLeftIcon className="mr-1 size-3.5" />
          Prev
        </Button>
        {!isLast && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setCurrentIndex((i) => i + 1)}
            data-testid="ask-user-question-next"
          >
            Next
            <ChevronRightIcon className="ml-1 size-3.5" />
          </Button>
        )}
        {isLast && (
          <Button
            size="sm"
            onClick={handleSubmit}
            disabled={!allAnswered}
            data-testid="ask-user-question-submit"
          >
            <CheckIcon className="mr-1 size-3.5" />
            Submit
          </Button>
        )}
        <Button size="sm" variant="outline" onClick={onReject} className="ml-auto">
          <XIcon className="mr-1 size-3.5" />
          Cancel
        </Button>
      </div>
    </div>
  );
}