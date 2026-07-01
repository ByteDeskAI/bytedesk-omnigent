import { cn } from "@/lib/utils";
import { NewChatLandingAttachmentSection } from "./NewChatLandingAttachmentSection";
import { NewChatLandingComposerActions } from "./NewChatLandingComposerActions";
import { NewChatLandingTextareaSection } from "./NewChatLandingTextareaSection";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingComposer({ state }: { state: NewChatLandingState }) {
  const s = state;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void s.handleCreate();
      }}
      onDrop={s.handleDrop}
      onDragOver={s.handleDragOver}
      onDragEnter={s.handleDragEnter}
      onDragLeave={s.handleDragLeave}
      className={cn(
        "relative z-10 flex w-full flex-col rounded-2xl border border-border bg-card dark:bg-card-solid shadow-[0_12px_20px_-20px_rgba(0,0,0,0.14),0_20px_28px_-28px_rgba(0,0,0,0.1)] transition-[border-color,box-shadow] duration-150 has-[textarea:focus]:border-foreground",
        s.isDragActive && "ring-2 ring-ring ring-inset",
      )}
      data-testid="new-chat-landing-composer"
    >
      {s.isDragActive && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-card/80">
          <span className="text-sm font-medium text-ring">Drop files here</span>
        </div>
      )}
      <NewChatLandingTextareaSection state={s} />
      <NewChatLandingAttachmentSection state={s} />
      <NewChatLandingComposerActions state={s} />
    </form>
  );
}