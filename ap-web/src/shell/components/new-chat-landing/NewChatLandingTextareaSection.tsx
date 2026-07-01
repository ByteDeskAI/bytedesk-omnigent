import { SlashCommandMenu } from "@/components/SlashCommandMenu";
import { SkillPills } from "@/components/SkillPills";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingTextareaSection({ state }: { state: NewChatLandingState }) {
  const s = state;

  return (
    <>
      {s.slashMenuOpen && (
        <SlashCommandMenu
          query={s.slashMenuQuery}
          activeIndex={s.slashMenuIndex}
          onSelect={s.applySlashSelection}
          commands={s.skillCommands}
        />
      )}
      <textarea
        ref={s.textareaRef}
        value={s.message}
        onChange={(e) => s.setMessage(e.target.value)}
        onKeyDown={(e) => {
          if (s.slashMenuOpen && s.slashMenuMatches.length > 0) {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              s.setSlashMenuIndex((i) => (i + 1) % s.slashMenuMatches.length);
              return;
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              s.setSlashMenuIndex((i) => (i <= 0 ? s.slashMenuMatches.length - 1 : i - 1));
              return;
            }
            if (
              (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) &&
              s.slashMenuIndex >= 0
            ) {
              e.preventDefault();
              s.applySlashSelection(s.slashMenuMatches[s.slashMenuIndex]!);
              return;
            }
            if (e.key === "Escape") {
              e.preventDefault();
              s.setMessage("");
              s.setSlashMenuIndex(-1);
              return;
            }
          }
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void s.handleCreate();
          }
        }}
        onPaste={(e) => {
          const pasted = Array.from(e.clipboardData.items)
            .filter((item) => item.kind === "file")
            .map((item) => item.getAsFile())
            .filter((f): f is File => f !== null);
          if (pasted.length > 0) {
            e.preventDefault();
            s.addFiles(pasted);
          }
        }}
        placeholder={s.pillSkills.length > 0 ? "" : "Describe a task to start a new session…"}
        aria-label="Describe a task to start a new session"
        rows={1}
        autoFocus
        data-testid="new-chat-landing-input"
        className="max-h-[200px] min-h-[60px] w-full resize-none overflow-y-auto bg-transparent px-4 pt-4 pb-1 font-sans text-sm leading-5 text-foreground outline-none placeholder:text-muted-foreground"
      />
      {s.pillSkills.length > 0 && s.message.length === 0 && (
        <div className="pointer-events-none absolute inset-x-4 top-4 flex flex-wrap items-center gap-2">
          <span className="font-sans text-sm leading-5 text-muted-foreground">
            Describe a task, or try a skill
          </span>
          <SkillPills skills={s.pillSkills} onPick={s.applySkillPill} />
        </div>
      )}
    </>
  );
}