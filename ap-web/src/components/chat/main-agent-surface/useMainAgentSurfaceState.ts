import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useUserMessageNav } from "@/hooks/useUserMessageNav";
import type { Bubble } from "@/lib/renderItems";
import { useChatStore } from "@/store/chatStore";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import type { ConversationScroller } from "../ConversationScrollRefBridge";
import { isSystemBubble, shouldShowTerminalSurface, shouldShowWorkingIndicator } from "../chat-utils";
import type { MainAgentSurfaceProps } from "./types";

export function useMainAgentSurfaceState({
  conversationId,
  bubbles,
  showsWorking,
  runnerOnline,
  onSend,
  onSendSlashCommand,
}: Pick<
  MainAgentSurfaceProps,
  "conversationId" | "bubbles" | "showsWorking" | "runnerOnline" | "onSend" | "onSendSlashCommand"
>) {
  const terminalFirst = useTerminalFirst();
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const sandboxLaunching = sandboxStatus !== null && sandboxStatus.stage !== "failed";
  const showTerminal = shouldShowTerminalSurface(conversationId, terminalFirst, runnerOnline);

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

  useEffect(() => {
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

  const [replyQuotes, setReplyQuotes] = useState<string[]>([]);
  const conversationRef = useRef<HTMLElement | null>(null);
  const [containerEl, setContainerEl] = useState<HTMLElement | null>(null);
  const setConversationEl = useCallback((el: HTMLDivElement | null) => {
    conversationRef.current = el;
    setContainerEl(el);
  }, []);
  const [scroller, setScroller] = useState<ConversationScroller | null>(null);
  const [sendScrollNonce, setSendScrollNonce] = useState(0);

  const handleSend = useCallback(
    (text: string, files?: File[]) => {
      setSendScrollNonce((n) => n + 1);
      onSend(text, files);
    },
    [onSend],
  );

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

  const showWorkingIndicator = shouldShowWorkingIndicator(showsWorking, bubbles);
  const launching = Boolean(
    (terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp) || sandboxLaunching,
  );

  return {
    terminalFirst,
    showTerminal,
    sandboxLaunching,
    userMessageIds,
    nav,
    replyQuotes,
    setReplyQuotes,
    conversationRef,
    containerEl,
    setConversationEl,
    scroller,
    setScroller,
    sendScrollNonce,
    handleSend,
    handleSendSlashCommand,
    isTerminalFirst,
    isNativeWrapper,
    showWorkingIndicator,
    launching,
  };
}