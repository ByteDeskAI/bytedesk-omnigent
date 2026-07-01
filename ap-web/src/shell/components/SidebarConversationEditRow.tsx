import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { CheckIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

interface SidebarConversationEditRowProps {
  initialTitle: string;
  onCommit: (title: string) => void;
  onCancel: () => void;
}

/**
 * Inline-edit shell for a conversation row.
 *
 * Auto-focuses on mount and selects the whole title so the user can
 * start typing to replace. Enter commits, Escape cancels, blur
 * commits — matches the spec's "lose focus or press enter" wording.
 * The blur-commits-on-Escape case is avoided by clearing the value
 * with the dedicated cancel handler before blur fires.
 */
export function SidebarConversationEditRow({
  initialTitle,
  onCommit,
  onCancel,
}: SidebarConversationEditRowProps) {
  const [value, setValue] = useState(initialTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelledRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      onCommit(value);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      cancelledRef.current = true;
      onCancel();
    }
  }

  function handleBlur() {
    if (cancelledRef.current) return;
    onCommit(value);
  }

  return (
    <div className="flex items-center gap-1 rounded-md bg-muted py-1 pr-1 pl-3">
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        data-testid="rename-conversation-input"
        className="min-w-0 flex-1 truncate rounded bg-transparent px-1 py-1 text-sm outline-none"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label="Save rename"
        onMouseDown={(e) => {
          e.preventDefault();
        }}
        onClick={() => onCommit(value)}
      >
        <CheckIcon className="size-3.5" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label="Cancel rename"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          cancelledRef.current = true;
          onCancel();
        }}
      >
        <XIcon className="size-3.5" />
      </Button>
    </div>
  );
}