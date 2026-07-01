// xterm.js view bridged to a terminal endpoint over a WebSocket.
//
// The xterm + WebSocket lifecycle lives in `TerminalSession` (plain
// JS, outside React). This component is a thin shell: a callback ref
// constructs the session when its container node attaches and
// returns a cleanup that disposes the session when the node detaches
// (or any of the addressing inputs change). React 19 calls the
// returned cleanup directly — no `useEffect` + `useRef` dance, no
// guard against a missing `ref.current`.

import { useCallback, useEffect, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { useOffline } from "@/hooks/useOffline";
import {
  type ConnectionState,
  type TerminalActivityListener,
  type TerminalInputListener,
  isUnexpectedTerminalClose,
  TerminalSession,
} from "../TerminalSession";
import { RECONNECT_BACKOFF_MS, RECONNECT_STABLE_MS } from "./constants";
import { StatusOverlay } from "./StatusOverlay";
import {
  buildAttachUrl,
  isMacPlatform,
  resumeErrorText,
  selectionHintText,
} from "./terminal-view-utils";

interface TerminalViewProps {
  sessionId?: string;
  terminalId?: string;
  attachPath?: string;
  readOnly?: boolean;
  onStateChange?: (state: ConnectionState | null) => void;
  onActivity?: TerminalActivityListener;
  onInput?: TerminalInputListener;
  onResume?: () => void | Promise<void>;
  resumePending?: boolean;
}

export function TerminalView({
  sessionId,
  terminalId,
  attachPath,
  readOnly = false,
  onStateChange,
  onActivity,
  onInput,
  onResume,
  resumePending = false,
}: TerminalViewProps) {
  if (!attachPath && (!sessionId || !terminalId)) {
    throw new Error("TerminalView requires either attachPath or sessionId + terminalId");
  }
  const offline = useOffline();
  const [state, setState] = useState<ConnectionState>({ kind: "connecting" });
  const [connectAttempt, setConnectAttempt] = useState(0);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [reconnectPending, setReconnectPending] = useState(false);
  const reconnectAttemptsRef = useRef(0);
  const connectedAtRef = useRef<number | null>(null);
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  const isDarkRef = useRef(isDark);
  isDarkRef.current = isDark;
  const sessionRef = useRef<TerminalSession | null>(null);
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const onActivityRef = useRef(onActivity);
  onActivityRef.current = onActivity;
  const onInputRef = useRef(onInput);
  onInputRef.current = onInput;

  const notifyState = useCallback((next: ConnectionState) => {
    setState(next);
    onStateChangeRef.current?.(next);
  }, []);

  const notifyActivity = useCallback(() => {
    onActivityRef.current?.();
  }, []);

  const notifyInput = useCallback(() => {
    onInputRef.current?.();
  }, []);

  const disposeActiveSession = useCallback(() => {
    sessionRef.current?.dispose();
    sessionRef.current = null;
  }, []);

  const handleResume = useCallback(async () => {
    if (!onResume) return;
    setResumeError(null);
    try {
      await onResume();
      disposeActiveSession();
      setConnectAttempt((attempt) => attempt + 1);
    } catch (error) {
      setResumeError(resumeErrorText(error));
    }
  }, [onResume, disposeActiveSession]);

  const attachSession = useCallback(
    (node: HTMLDivElement | null) => {
      if (node === null) return;
      if (offline) {
        notifyState({ kind: "closed", code: 1000, reason: "offline" });
        return;
      }
      notifyState({ kind: "connecting" });

      let terminalSession: TerminalSession | null = null;
      let cancelled = false;
      queueMicrotask(() => {
        if (cancelled) return;
        terminalSession = new TerminalSession(
          node,
          buildAttachUrl({ sessionId, terminalId, readOnly, attachPath }),
          notifyState,
          isDarkRef.current,
          notifyActivity,
          notifyInput,
        );
        sessionRef.current = terminalSession;
      });
      return () => {
        cancelled = true;
        terminalSession?.dispose();
        sessionRef.current = null;
        onStateChangeRef.current?.(null);
      };
    },
    [offline, sessionId, terminalId, readOnly, attachPath, notifyState, notifyActivity, notifyInput],
  );

  useEffect(() => {
    sessionRef.current?.setTheme(isDark);
  }, [isDark]);

  useEffect(() => {
    if (state.kind === "connected") {
      connectedAtRef.current = Date.now();
      setReconnectPending(false);
      return;
    }
    if (state.kind !== "closed") return;
    if (!isUnexpectedTerminalClose(state.code)) {
      setReconnectPending(false);
      return;
    }
    if (
      connectedAtRef.current !== null &&
      Date.now() - connectedAtRef.current >= RECONNECT_STABLE_MS
    ) {
      reconnectAttemptsRef.current = 0;
    }
    connectedAtRef.current = null;
    if (reconnectAttemptsRef.current >= RECONNECT_BACKOFF_MS.length) {
      setReconnectPending(false);
      return;
    }
    const delay = RECONNECT_BACKOFF_MS[reconnectAttemptsRef.current];
    reconnectAttemptsRef.current += 1;
    setReconnectPending(true);
    let redialed = false;
    const redial = () => {
      if (redialed) return;
      redialed = true;
      disposeActiveSession();
      setConnectAttempt((attempt) => attempt + 1);
    };
    const timer = window.setTimeout(redial, delay);
    const onVisible = () => {
      if (document.visibilityState === "visible") redial();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [state, disposeActiveSession]);

  return (
    <div
      data-testid="terminal-view"
      data-state={state.kind}
      data-terminal-id={terminalId ?? attachPath}
      className="relative flex min-h-0 flex-1 flex-col"
    >
      <div className="min-h-0 flex-1 overflow-hidden p-1">
        <div key={connectAttempt} ref={attachSession} className="h-full w-full overflow-hidden" />
      </div>
      <div
        data-testid="terminal-selection-hint"
        className="shrink-0 select-none px-2 py-1 text-[10px] text-muted-foreground/70"
      >
        {selectionHintText(isMacPlatform())}
      </div>
      {state.kind !== "connected" && (
        <StatusOverlay
          state={state}
          reconnectPending={reconnectPending}
          onResume={onResume ? handleResume : undefined}
          resumePending={resumePending}
          resumeError={resumeError}
        />
      )}
    </div>
  );
}