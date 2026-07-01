import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { type Agent } from "@/hooks/useAgents";
import { useUserMessageNav } from "@/hooks/useUserMessageNav";
import { type CostRoutingVerdict } from "@/components/CostRoutingControl";
import { isOwnerLevel } from "@/lib/permissionsApi";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type { Bubble } from "@/lib/renderItems";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { MainTerminalView } from "@/shell/MainTerminalView";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { ConversationBubbleList } from "./ConversationBubbleList";
import { Composer } from "./Composer";
import { ConnectionIndicator } from "./ConnectionIndicator";
import {
  ConversationScrollRefBridge,
  type ConversationScroller,
} from "./ConversationScrollRefBridge";
import { HistoryAutoLoader } from "./HistoryAutoLoader";
import { JumpToTopButton } from "./JumpToTopButton";
import { ScrollToBottomOnSend } from "./ScrollToBottomOnSend";
import { SelectionPopup } from "./SelectionPopup";
import { UserMessageNavConnected } from "./UserMessageNavConnected";
import { WorkingStatusPin } from "./WorkingStatusPin";
import {
  CHAT_COLUMN_WIDTH,
  isSystemBubble,
  shouldShowTerminalSurface,
  shouldShowWorkingIndicator,
} from "./chat-utils";

interface MainAgentSurfaceProps {
  /**
   * Active conversation id, or null when on the landing page. Forwarded
   * to MainTerminalView so the inline terminal can target the right
   * session in terminal-first mode.
   */
  conversationId: string | null;
  bubbles: Bubble[];
  status: "idle" | "streaming";
  /** Local stream OR cross-client `session.status: running`. Gates the
   *  composer's Stop/Interrupt button — the parent's OWN turn only. */
  isWorking: boolean;
  /** Display-only main-chat indicator after elicitation/offline gates.
   *  Never includes child-session activity and never gates Stop/Interrupt. */
  showsWorking: boolean;
  /**
   * Strict runner-tunnel liveness, used only to gate the inline terminal
   * view (the PTY dies the moment the runner tunnel drops). The reconnect
   * affordances key off `liveness` instead.
   */
  runnerOnline: boolean | undefined;
  /** Derived open-session liveness — drives the reconnect hint/banner. */
  liveness: SessionLiveness;
  agentsError: unknown;
  disabled: boolean;
  onSend: (text: string, files?: File[]) => void;
  /**
   * Invoke a skill via the `slash_command` event path. Gated off inside
   * `MainAgentSurface` for terminal-first (native) sessions, where `/skill`
   * is sent as plaintext for the vendor TUI to handle. See
   * `ComposerProps.onSendSlashCommand`.
   */
  onSendSlashCommand?: (name: string, args: string) => void;
  onStop: () => void;
  onShowReconnectHelp: () => void;
  agents: Agent[] | undefined;
  agentsLoading: boolean;
  selectedAgentId: string | null;
  onSelectAgent: (id: string) => void;
  /** Whether older messages exist that haven't been loaded yet. */
  hasMoreHistory: boolean;
  /** Whether a load-more fetch is currently in flight. */
  loadingMoreHistory: boolean;
  permissionLevel: number | null;
  /** Forces composer read-only with the given placeholder when non-null. See ``ComposerProps.readOnlyReason``. */
  readOnlyReason: string | null;
  effortLevels: readonly string[];
  /** Show effort controls. */
  showEffort: boolean;
  /** Whether the picker dropdown should include a Models section. */
  showModels: boolean;
  /** Latest advisor verdict for the cost-routing pill; null when none. */
  costRoutingVerdict: CostRoutingVerdict | null;
  /** Session passes `isCostRoutingSession` (polly orchestrator, not a child). */
  costRoutingEligible: boolean;
  /**
   * Sub-agent instance label when the active session is a child, e.g.
   * ``"check-account-eligibility"``; ``null`` for top-level sessions.
   * Drives the composer's "Chatting with sub-agent …" tray and suppresses
   * the scroll-pinned "Working…" tab (the tray takes that slot). See
   * ``subAgentComposerLabel``.
   */
  subAgentLabel: string | null;
}
export function MainAgentSurface({
  conversationId,
  bubbles,
  status,
  isWorking,
  showsWorking,
  runnerOnline,
  liveness,
  agentsError,
  disabled,
  onSend,
  onSendSlashCommand,
  onStop,
  onShowReconnectHelp,
  agents,
  agentsLoading,
  selectedAgentId,
  onSelectAgent,
  hasMoreHistory,
  loadingMoreHistory,
  permissionLevel,
  readOnlyReason,
  effortLevels,
  showEffort,
  showModels,
  costRoutingVerdict,
  costRoutingEligible,
  subAgentLabel,
}: MainAgentSurfaceProps) {
  const terminalFirst = useTerminalFirst();
  // Mirrors ChatPage's `sandboxLaunching`: while the managed-sandbox
  // launch runs, the composer must stay sendable — the server parks
  // the message on the launch rendezvous — even though liveness reads
  // the not-yet-host-bound session as stranded.
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const sandboxLaunching = sandboxStatus !== null && sandboxStatus.stage !== "failed";
  // Render the inline terminal whenever the user has opted in via the
  // connection pill. The terminal surface owns its no-terminal state,
  // including stopped/resumable sessions, and the connection indicator
  // remains below it for offline sessions.
  const showTerminal = shouldShowTerminalSurface(conversationId, terminalFirst, runnerOnline);

  // All hook calls below must run on every render regardless of
  // `showTerminal` — Rules of Hooks. The early return for the terminal
  // branch lives below, after every hook has run.

  // Single nav instance shared by hotkey + buttons (see useUserMessageNav).
  // System-message bubbles (`[System: ...]` notifications rendered via
  // SystemMessageView) are excluded — the hotkey is for navigating real
  // user turns, not runtime markers.
  const userMessageIds = useMemo(
    () =>
      bubbles
        .filter(
          (b): b is Extract<Bubble, { kind: "user" }> => b.kind === "user" && !isSystemBubble(b),
        )
        .map((b) => b.itemId),
    [bubbles],
  );
  const nav = useUserMessageNav(userMessageIds);

  // Cmd+Alt+↑/↓ (Ctrl+Alt on win/linux) — guarded so the composer's
  // own unmodified ArrowUp/Down history-recall still works.
  useEffect(() => {
    // globalThis prefix because React's KeyboardEvent is imported above.
    const handler = (e: globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || !e.altKey) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      const target = e.target;
      if (
        target instanceof HTMLElement &&
        target.closest('textarea, input, [contenteditable="true"]')
      ) {
        return;
      }
      e.preventDefault();
      if (e.key === "ArrowUp") nav.goPrev();
      else nav.goNext();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [nav]);

  // Active reply quotes — each "Reply ↵" click appends; consumed by Composer.
  const [replyQuotes, setReplyQuotes] = useState<string[]>([]);

  // Ref forwarded to SelectionPopup to scope selection detection to the
  // conversation area, preventing selections in the composer from triggering
  // the popup. Mirrored into state (`containerEl`) so JumpToTopButton — which
  // renders inside this wrapper, outside the mask-faded scroll viewport — can
  // attach its hover listeners to the wrapper (the common ancestor of both the
  // scroll area and the pill, so moving the cursor onto the pill keeps it live).
  const conversationRef = useRef<HTMLElement | null>(null);
  const [containerEl, setContainerEl] = useState<HTMLElement | null>(null);
  const setConversationEl = useCallback((el: HTMLDivElement | null) => {
    conversationRef.current = el;
    setContainerEl(el);
  }, []);
  // The conversation's scroll container + the StickToBottom controls needed to
  // override its bottom-lock, lifted out of the context by
  // ConversationScrollRefBridge so the pinned-but-unmasked JumpToTopButton can
  // read and drive the scroll.
  const [scroller, setScroller] = useState<ConversationScroller | null>(null);
  const [sendScrollNonce, setSendScrollNonce] = useState(0);
  const handleSend = useCallback(
    (text: string, files?: File[]) => {
      setSendScrollNonce((n) => n + 1);
      onSend(text, files);
    },
    [onSend],
  );
  // Wrap the slash-command sender the same way (scroll to bottom on send).
  // Gated off for native-wrapper sessions (claude-native / codex-native):
  // there the composer's `/skill` must reach the vendor TUI as plaintext
  // (the server has no slash_command path for native sessions). Undefined
  // → the composer falls through to the plaintext send for these. Keyed
  // on the wrapper label, NOT `isTerminalFirst` — a terminal-first SDK
  // session (embedded Omnigent REPL terminal) runs an in-process harness
  // with the full server-side slash_command path.
  const isTerminalFirst = terminalFirst?.isTerminalFirst === true;
  const isNativeWrapper = terminalFirst?.isNativeWrapper === true;
  const handleSendSlashCommand = useMemo(
    () =>
      onSendSlashCommand && !isNativeWrapper
        ? (name: string, args: string) => {
            setSendScrollNonce((n) => n + 1);
            onSendSlashCommand(name, args);
          }
        : undefined,
    [onSendSlashCommand, isNativeWrapper],
  );

  // "Working…" is shown when the main session is busy, including after a
  // reload that hydrates `running` before any bubbles exist locally. Streaming
  // assistant content and compaction spinners own the in-progress slot once
  // they have rendered.
  const showWorkingIndicator = shouldShowWorkingIndicator(showsWorking, bubbles);

  if (showTerminal && conversationId) {
    return (
      <>
        <MainTerminalView
          conversationId={conversationId}
          initialTerminalKey={terminalFirst?.terminalViewKey}
          // Non-owners attach read-only: a shared PTY can't attribute
          // input per-user, so only the owner may type. They drive the
          // agent via the composer instead. Server enforces this too.
          readOnly={!isOwnerLevel(permissionLevel)}
        />
        <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
      </>
    );
  }

  return (
    <>
      {/* Wrapper div gives us a ref to scope the SelectionPopup to the
          conversation area without requiring Conversation to forward refs. */}
      <div ref={setConversationEl} className="relative flex min-h-0 flex-1 overflow-hidden">
        {/* chat-scroll-fade masks the viewport's top edge so scrolling
            content dissolves into the canvas before reaching the
            ChatHeader overlay's controls (geometry in index.css). */}
        <Conversation className="chat-scroll-fade flex-1">
          {/* gap-4 overrides ConversationContent's default gap-8 so consecutive agent turns read as one thread. */}
          <ConversationContent className={cn("mx-auto w-full gap-4 pt-20 pb-6", CHAT_COLUMN_WIDTH)}>
            {/* Scroll helpers — must live inside StickToBottom to access context. */}
            <ScrollToBottomOnSend nonce={sendScrollNonce} />
            <ConversationScrollRefBridge onScroller={setScroller} />
            <HistoryAutoLoader
              hasMoreHistory={hasMoreHistory}
              loadingMoreHistory={loadingMoreHistory}
              sendScrollNonce={sendScrollNonce}
            />
            <ConversationBubbleList
              bubbles={bubbles}
              showWorkingIndicator={showWorkingIndicator}
              launching={Boolean(
                (terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp) ||
                  sandboxLaunching,
              )}
              emptyState={
                <ConversationEmptyState>
                  <div className="space-y-1.5">
                    <h3 className="text-2xl font-medium tracking-[-0.02em]">
                      What should we work on?
                    </h3>
                    <p className="text-muted-foreground text-base">
                      {agentsError
                        ? `Failed to load agents: ${agentsError instanceof Error ? agentsError.message : String(agentsError)}`
                        : "Send a message to get started."}
                    </p>
                  </div>
                </ConversationEmptyState>
              }
            />
          </ConversationContent>
          <ConversationScrollButton />
          {/* Outside ConversationContent so it's pinned to the viewport, not the scroll. See WorkingStatusPin.
              Suppressed in a sub-agent session: the composer's "Chatting with sub-agent …" tray owns this slot. */}
          <WorkingStatusPin show={showWorkingIndicator} suppress={subAgentLabel != null} />
          <UserMessageNavConnected
            goPrev={nav.goPrev}
            goNext={nav.goNext}
            canPrev={nav.canPrev}
            canNext={nav.canNext}
            hidden={userMessageIds.length === 0}
          />
        </Conversation>
        {/* Hover the top edge to reveal a pill that loads all older history and
            scrolls to the first message. Rendered here (a wrapper sibling of
            Conversation) rather than inside it so it escapes the chat-scroll-fade
            mask and can sit right at the fade border. */}
        <JumpToTopButton
          containerEl={containerEl}
          scroller={scroller}
          hasMoreHistory={hasMoreHistory}
        />
      </div>
      {/* Floating reply button — scoped to the conversation container. */}
      <SelectionPopup
        containerRef={conversationRef}
        onReply={(text) => setReplyQuotes((prev) => [...prev, text])}
      />

      <Composer
        disabled={disabled}
        status={status}
        isWorking={isWorking}
        onSend={handleSend}
        onSendSlashCommand={handleSendSlashCommand}
        onStop={onStop}
        agents={agents}
        agentsLoading={agentsLoading}
        selectedAgentId={selectedAgentId}
        onSelectAgent={onSelectAgent}
        permissionLevel={permissionLevel}
        readOnlyReason={readOnlyReason}
        replyQuotes={replyQuotes}
        onRemoveQuote={(i) => setReplyQuotes((prev) => prev.filter((_, idx) => idx !== i))}
        onClearAllQuotes={() => setReplyQuotes([])}
        effortLevels={effortLevels}
        showEffort={showEffort}
        showModels={showModels}
        isTerminalFirst={isTerminalFirst}
        isNativeWrapper={isNativeWrapper}
        reconnectHint={liveness.kind === "runner_asleep"}
        unreachable={
          !sandboxLaunching &&
          (liveness.kind === "host_offline" || liveness.kind === "local_stranded")
        }
        costRoutingVerdict={costRoutingVerdict}
        costRoutingEligible={costRoutingEligible}
        subAgentLabel={subAgentLabel}
      />

      {/* Chat/Terminal toggle for terminal-first sessions, reconnect-or-
          fork banner when unreachable, nothing otherwise. Sits below the
          composer so its position is consistent with the terminal view. */}
      <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
    </>
  );
}
