import type { ChangeEvent } from "react";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";

interface AskUserQuestionSectionProps {
  question: ClaudeQuestion;
  questionKeyValue: string;
  selection: string | string[];
  customSelected: boolean;
  customText: string;
  onSingleSelect: (key: string, label: string) => void;
  onCustomToggleSingle: (key: string) => void;
  onMultiToggle: (key: string, label: string) => void;
  onCustomToggleMulti: (key: string) => void;
  onCustomInput: (key: string, e: ChangeEvent<HTMLTextAreaElement>) => void;
}

export function AskUserQuestionSection({
  question,
  questionKeyValue,
  selection,
  customSelected,
  customText,
  onSingleSelect,
  onCustomToggleSingle,
  onMultiToggle,
  onCustomToggleMulti,
  onCustomInput,
}: AskUserQuestionSectionProps) {
  const selectedLabels: string[] = question.multiSelect
    ? Array.isArray(selection)
      ? selection
      : []
    : !customSelected && typeof selection === "string" && selection
      ? [selection]
      : [];
  const previewsToShow = question.options.filter(
    (opt) => selectedLabels.includes(opt.label) && opt.preview,
  );

  const customRowId = `${questionKeyValue}__custom`;
  const customRowChecked = customSelected;
  const customRowValue = customText;

  return (
    <fieldset
      key={questionKeyValue}
      className="flex flex-col gap-2 mb-2"
      data-testid="ask-user-question-section"
    >
      <legend className="text-foreground text-sm font-medium flex items-center gap-2 mb-2">
        {question.question}
      </legend>
      <div className="flex flex-col gap-2">
        {question.options.map((opt) => {
          const inputId = `${questionKeyValue}-${opt.label}`;
          if (question.multiSelect) {
            const checked = Array.isArray(selection) && selection.includes(opt.label);
            return (
              <label
                key={opt.label}
                htmlFor={inputId}
                className="flex items-start gap-2 cursor-pointer text-sm text-foreground"
              >
                <input
                  type="checkbox"
                  id={inputId}
                  checked={checked}
                  onChange={() => onMultiToggle(questionKeyValue, opt.label)}
                  className="mt-1"
                />
                <span className="flex flex-col">
                  <span>{opt.label}</span>
                  {opt.description && (
                    <span className="text-muted-foreground text-xs">{opt.description}</span>
                  )}
                </span>
              </label>
            );
          }
          const checked = selection === opt.label && !customSelected;
          return (
            <label
              key={opt.label}
              htmlFor={inputId}
              className="flex items-start gap-2 cursor-pointer text-sm text-foreground"
            >
              <input
                type="radio"
                id={inputId}
                name={questionKeyValue}
                checked={checked}
                onChange={() => onSingleSelect(questionKeyValue, opt.label)}
                className="mt-1"
              />
              <span className="flex flex-col">
                <span>{opt.label}</span>
                {opt.description && (
                  <span className="text-muted-foreground text-xs">{opt.description}</span>
                )}
              </span>
            </label>
          );
        })}
        <label
          htmlFor={customRowId}
          className="flex items-start gap-2 cursor-pointer text-sm text-foreground"
        >
          <input
            type={question.multiSelect ? "checkbox" : "radio"}
            id={customRowId}
            name={question.multiSelect ? undefined : questionKeyValue}
            checked={customRowChecked}
            onChange={() =>
              question.multiSelect
                ? onCustomToggleMulti(questionKeyValue)
                : onCustomToggleSingle(questionKeyValue)
            }
            className="mt-1"
            data-testid="ask-user-question-custom-toggle"
          />
          <textarea
            rows={1}
            placeholder="Type something"
            value={customRowValue}
            onChange={(e) => onCustomInput(questionKeyValue, e)}
            data-testid="ask-user-question-custom-input"
            className="field-sizing-content flex-1 resize-none bg-transparent text-sm placeholder:text-muted-foreground focus:outline-none"
          />
        </label>
      </div>
      {previewsToShow.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="ask-user-question-previews">
          {previewsToShow.map((opt) => (
            <pre
              key={opt.label}
              className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap"
            >
              {opt.preview}
            </pre>
          ))}
        </div>
      )}
    </fieldset>
  );
}