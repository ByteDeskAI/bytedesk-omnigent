import { cn } from "@/lib/utils";
import { SlashCommandMenu } from "@/components/SlashCommandMenu";
import { ComposerStatusLine } from "../ComposerStatusLine";
import { SubagentComposerTray } from "../SubagentComposerTray";
import { CHAT_COLUMN_WIDTH } from "../chat-utils";
import type { ComposerProps } from "./composer-types";
import { ComposerActionRow } from "./ComposerActionRow";
import { ComposerFileChips } from "./ComposerFileChips";
import { ComposerHighlightOverlay } from "./ComposerHighlightOverlay";
import { ComposerQuoteChips } from "./ComposerQuoteChips";
import { useComposer } from "./useComposer";

export function Composer(props: ComposerProps) {
  const {
    isTerminalFirst = false,
    unreachable = false,
    costRoutingVerdict = null,
    costRoutingEligible = false,
    subAgentLabel = null,
    disabled,
    replyQuotes,
    onRemoveQuote,
    agents,
    agentsLoading,
    selectedAgentId,
    onSelectAgent,
    effortLevels,
    showEffort,
    showModels,
  } = props;

  const c = useComposer(props);

  return (
    <form
      onSubmit={c.handleSubmit}
      className={cn("px-4 md:px-6", isTerminalFirst ? "pb-1.5" : "pb-3")}
    >
      <input
        ref={c.fileInputRef}
        type="file"
        multiple
        accept="image/*,application/pdf,text/*,application/json"
        className="hidden"
        onChange={(e) => {
          if (e.target.files) {
            c.addFiles(Array.from(e.target.files));
            e.target.value = "";
          }
        }}
      />
      {subAgentLabel ? <SubagentComposerTray label={subAgentLabel} /> : null}
      <div
        className={cn(
          "relative mx-auto flex w-full flex-col rounded-2xl border border-border bg-card dark:bg-card-solid shadow-sm transition",
          CHAT_COLUMN_WIDTH,
          c.isDragActive && "ring-2 ring-ring ring-inset",
        )}
        onDrop={c.handleDrop}
        onDragOver={c.handleDragOver}
        onDragEnter={c.handleDragEnter}
        onDragLeave={c.handleDragLeave}
      >
        {c.isDragActive && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-card/80">
            <span className="text-sm font-medium text-ring">Drop files here</span>
          </div>
        )}
        {c.menuOpen && (
          <SlashCommandMenu
            query={c.menuQuery}
            activeIndex={c.menuIndex}
            onSelect={c.applyMenuSelection}
            commands={c.slashCommands}
          />
        )}
        <ComposerQuoteChips quotes={replyQuotes} onRemoveQuote={onRemoveQuote} />
        <div className="relative">
          {c.composerIsCommand && (
            <ComposerHighlightOverlay value={c.value} backdropRef={c.backdropRef} />
          )}
          <textarea
            ref={c.textareaRef}
            value={c.value}
            onChange={(e) => {
              c.setValue(e.target.value);
              c.dirtyRef.current = true;
              if (c.commandError !== null) c.setCommandError(null);
              if (c.recallingRef.current) c.recallingRef.current = false;
              else c.resetCursor();
            }}
            onKeyDown={c.handleKeyDown}
            onPaste={c.handlePaste}
            onScroll={(e) => {
              if (c.backdropRef.current) c.backdropRef.current.scrollTop = e.currentTarget.scrollTop;
            }}
            aria-label="Message the agent"
            placeholder={c.placeholder}
            rows={1}
            disabled={disabled || c.isReadOnly || unreachable || c.hasPendingElicitation}
            data-slash-command={c.composerIsCommand ? "true" : undefined}
            className={cn(
              "relative w-full resize-none bg-transparent px-4 pt-3 pb-2 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-60",
              c.composerIsCommand && "text-transparent caret-foreground",
            )}
          />
        </div>
        <ComposerFileChips files={c.files} onRemoveFile={c.removeFile} />
        {c.commandError !== null && (
          <div className="px-4 pb-2 text-xs text-muted-foreground whitespace-pre-wrap">
            {c.commandError}
          </div>
        )}
        <ComposerActionRow
          disabled={disabled}
          isReadOnly={c.isReadOnly}
          hasPendingElicitation={c.hasPendingElicitation}
          showInterruptButton={c.showInterruptButton}
          hasDraft={c.hasDraft}
          costRoutingEligible={costRoutingEligible}
          costControlModeOverride={c.costControlModeOverride}
          costRoutingVerdict={costRoutingVerdict}
          agents={agents}
          agentsLoading={agentsLoading}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
          effortLevels={effortLevels}
          showEffort={showEffort}
          showModels={showModels}
          pickerOpenNonce={c.pickerOpenNonce}
          onAttachClick={() => c.fileInputRef.current?.click()}
          onTranscript={(text) => {
            c.setValue((prev) => (prev ? `${prev} ${text}` : text));
            c.dirtyRef.current = true;
            if (c.commandError !== null) c.setCommandError(null);
          }}
          onClearCommandError={() => {
            if (c.commandError !== null) c.setCommandError(null);
          }}
          resetCursor={c.resetCursor}
        />
      </div>
      <ComposerStatusLine />
    </form>
  );
}