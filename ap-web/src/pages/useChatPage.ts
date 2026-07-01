import { useEffect, useMemo, useRef, useState } from "react";
import { type Agent, useSessionAgent, useAgents } from "@/hooks/useAgents";
import { useConversations } from "@/hooks/useConversations";
import { usePermissions } from "@/hooks/usePermissions";
import { useOffline } from "@/hooks/useOffline";
import { useNavigate, useParams } from "@/lib/routing";
import { derivePermissionLevel } from "@/lib/permissionsApi";
import {
  type Bubble,
  type BubbleCache,
  buildBubbles,
  createBubbleCache,
} from "@/lib/renderItems";
import { getCurrentAuthorId } from "@/lib/identity";
import {
  consumePendingInitialPrompt,
  type PendingInitialPrompt,
  useChatStore,
} from "@/store/chatStore";
import { useSession } from "@/hooks/useSession";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import {
  livenessRowFromSession,
  useSessionLiveness,
} from "@/hooks/useSessionLiveness";
import { useMarkConversationSeen } from "@/hooks/useUnseenConversations";
import { isCostRoutingSession, parseCostRoutingVerdict } from "@/components/CostRoutingControl";
import { UNTITLED_CONVERSATION_LABEL } from "@/shell/sidebarNav";
import {
  buildPendingBubbles,
  computeIsWorking,
  computeShowsWorking,
  dispatchInitialPrompt,
  isSessionSharedWithOthers,
  isUnboundCodingFork,
  mergePendingBubbles,
  readOnlyReasonForSessionLabels,
  reorderCommittedRequestElicitations,
  shouldSendInitialPrompt,
  subAgentComposerLabel,
  truncateTitle,
} from "@/components/chat/chat-utils";
import type { ChatPageShellProps } from "./organisms/ChatPageShell";

export function useChatPage(): ChatPageShellProps {
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const networkOffline = useOffline();
  const [initialPrompt, setInitialPrompt] = useState<{
    conversationId: string;
    prompt: PendingInitialPrompt;
  } | null>(null);
  const initialPromptSentForConvRef = useRef<string | null>(null);
  const consumedInitialPromptRef = useRef<{
    conversationId: string;
    prompt: PendingInitialPrompt | null;
  } | null>(null);
  const {
    data: agents,
    isLoading: agentsLoading,
    error: agentsError,
    refetch: refetchAgents,
  } = useAgents();
  const { data: conversationsData } = useConversations();
  const conversations = useMemo(
    () => conversationsData?.pages.flatMap((p) => p.data),
    [conversationsData],
  );

  useMarkConversationSeen(urlConvId, conversations?.find((c) => c.id === urlConvId)?.updated_at);

  useEffect(() => {
    void useChatStore.getState().switchTo(urlConvId ?? null);
  }, [urlConvId]);

  useEffect(() => {
    if (!urlConvId) {
      setInitialPrompt(null);
      return;
    }
    const cached = consumedInitialPromptRef.current;
    const prompt =
      cached?.conversationId === urlConvId ? cached.prompt : consumePendingInitialPrompt(urlConvId);
    consumedInitialPromptRef.current = { conversationId: urlConvId, prompt };
    setInitialPrompt(prompt === null ? null : { conversationId: urlConvId, prompt });
  }, [urlConvId]);

  const blocks = useChatStore((s) => s.blocks);
  const pendingUserMessages = useChatStore((s) => s.pendingUserMessages);
  const activeResponse = useChatStore((s) => s.activeResponse);
  const interruptedResponseIds = useChatStore((s) => s.interruptedResponseIds);
  const status = useChatStore((s) => s.status);
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const sandboxLaunching = sandboxStatus !== null && sandboxStatus.stage !== "failed";
  const runnerOnline = useSessionRunnerOnline(urlConvId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const conversationLoadError = useChatStore((s) => s.conversationLoadError);
  const boundAgentId = useChatStore((s) => s.boundAgentId);
  const boundAgentName = useChatStore((s) => s.boundAgentName);
  const { data: boundAgentBySession } = useSessionAgent(urlConvId ?? null);
  const hasMoreHistory = useChatStore((s) => s.hasMoreHistory);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);

  const bubbleCacheRef = useRef<BubbleCache>(createBubbleCache());
  const bubbles = useMemo<Bubble[]>(() => {
    const committed = reorderCommittedRequestElicitations(
      buildBubbles(blocks, activeResponse, bubbleCacheRef.current, interruptedResponseIds),
    );
    if (pendingUserMessages.length === 0) return committed;
    return mergePendingBubbles(
      committed,
      buildPendingBubbles(pendingUserMessages, getCurrentAuthorId()),
    );
  }, [blocks, activeResponse, interruptedResponseIds, pendingUserMessages]);

  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const agentId = selectedAgentId ?? agents?.[0]?.id ?? null;

  useEffect(() => {
    if (boundAgentId === null) return;
    setSelectedAgentId(boundAgentId);
    if (agents && !agents.some((a) => a.id === boundAgentId)) {
      void refetchAgents();
    }
  }, [boundAgentId, agents, refetchAgents]);

  useEffect(() => {
    if (
      !shouldSendInitialPrompt({
        initialPrompt: initialPrompt?.prompt.text ?? null,
        promptConversationId: initialPrompt?.conversationId ?? null,
        sentForConversationId: initialPromptSentForConvRef.current,
        conversationId: urlConvId,
        loadingConversation,
        agentId,
      })
    ) {
      return;
    }
    if (initialPrompt === null || !agentId || !urlConvId) return;
    initialPromptSentForConvRef.current = urlConvId;
    const { send, sendSlashCommand } = useChatStore.getState();
    dispatchInitialPrompt(initialPrompt.prompt, agentId, send, sendSlashCommand);
  }, [initialPrompt, urlConvId, loadingConversation, agentId]);

  const [resumeDirDialogOpen, setResumeDirDialogOpen] = useState(false);
  const [pendingResumePrompt, setPendingResumePrompt] = useState<{
    sessionId: string;
    text: string;
    files: File[];
  } | null>(null);
  const [reconnectDialogOpen, setReconnectDialogOpen] = useState(false);

  const hasPendingElicitation = useMemo(
    () => blocks.some((b) => b.type === "elicitation" && b.status === "pending"),
    [blocks],
  );

  const { session: activeSession, isLoading: sessionLoading } = useSession(urlConvId ?? null);

  const activeSessionLabels = activeSession?.labels;
  const costRoutingVerdict = useMemo(
    () => parseCostRoutingVerdict(activeSessionLabels),
    [activeSessionLabels],
  );
  const costRoutingEligible = isCostRoutingSession(activeSession);
  const subAgentLabel = subAgentComposerLabel(activeSession);
  const activeConv = urlConvId ? conversations?.find((c) => c.id === urlConvId) : null;

  const isWorking = !hasPendingElicitation && computeIsWorking(sessionStatus);
  const showsWorking = computeShowsWorking(sessionStatus, { hasPendingElicitation, runnerOnline });

  const forkSourceId =
    activeSession?.labels?.["omnigent.fork.source_id"] ??
    activeConv?.labels?.["omnigent.fork.source_id"] ??
    null;
  const isUnboundFork = isUnboundCodingFork({
    forkSourceId,
    workspace: activeSession?.workspace ?? activeConv?.workspace ?? null,
  });

  const viewerId = getCurrentAuthorId();
  const sessionOwner = activeConv?.owner ?? null;
  const viewerOwnsSession = sessionOwner !== null && sessionOwner === viewerId;
  const { data: ownerGrants } = usePermissions(viewerOwnsSession ? (urlConvId ?? null) : null);
  const isSessionShared = isSessionSharedWithOthers(sessionOwner, viewerId, ownerGrants);

  const livenessRow = activeConv ?? livenessRowFromSession(activeSession);
  const liveness = useSessionLiveness(urlConvId ?? undefined, livenessRow, {
    turnActive: status === "streaming",
  });

  useEffect(() => {
    const fallback = urlConvId ? UNTITLED_CONVERSATION_LABEL : "Omnigent";
    const base = truncateTitle(activeConv?.title ?? fallback);
    document.title = showsWorking ? `● ${base}` : base;
  }, [activeConv?.title, showsWorking, urlConvId]);

  useEffect(() => {
    if (pendingResumePrompt === null || !agentId || !urlConvId) return;
    if (pendingResumePrompt.sessionId !== urlConvId) return;
    if (runnerOnline !== true) return;
    const { text, files } = pendingResumePrompt;
    setPendingResumePrompt(null);
    void useChatStore.getState().send(text, agentId, files);
  }, [pendingResumePrompt, runnerOnline, agentId, urlConvId]);

  if (urlConvId) {
    if (loadingConversation) return { kind: "hydrating" };
    if (conversationLoadError) {
      return {
        kind: "error",
        conversationId: urlConvId,
        error: conversationLoadError,
      };
    }
  }

  const isUnreachable =
    !sandboxLaunching && (liveness.kind === "host_offline" || liveness.kind === "local_stranded");

  function onSend(text: string, files?: File[]) {
    if (!agentId) return;
    if (urlConvId && runnerOnline === false && isUnboundFork) {
      setPendingResumePrompt({ sessionId: urlConvId, text, files: files ?? [] });
      setResumeDirDialogOpen(true);
      return;
    }
    if (urlConvId && isUnreachable) {
      setReconnectDialogOpen(true);
      return;
    }
    void useChatStore.getState().send(text, agentId, files, {
      onConversationCreated: (newId) => {
        navigate(`/c/${newId}`, { replace: true });
      },
    });
  }

  function onSendSlashCommand(name: string, args: string) {
    if (!agentId) return;
    if (urlConvId && runnerOnline === false && isUnboundFork) {
      setResumeDirDialogOpen(true);
      return;
    }
    if (urlConvId && isUnreachable) {
      setReconnectDialogOpen(true);
      return;
    }
    void useChatStore.getState().sendSlashCommand(name, args, agentId, {
      onConversationCreated: (newId) => {
        navigate(`/c/${newId}`, { replace: true });
      },
    });
  }

  function onStop() {
    useChatStore.getState().stop();
  }

  const permissionLevel = derivePermissionLevel(
    activeSession,
    sessionLoading,
    activeConv,
    urlConvId,
    conversationsData !== undefined,
  );
  const readOnlyReason = readOnlyReasonForSessionLabels(activeSession, activeConv);
  const capabilitySource = {
    labels: activeSession ? (activeSession.labels ?? {}) : (activeConv?.labels ?? {}),
  };

  const visibleAgents = boundAgentId
    ? boundAgentBySession
      ? [boundAgentBySession]
      : boundAgentName
        ? [{ id: boundAgentId, name: boundAgentName } as Agent]
        : agents?.filter((a) => a.id === boundAgentId)
    : agents;

  if (!urlConvId) return { kind: "landing" };

  const reconnectState = liveness.kind === "host_offline" ? "host_offline" : "local_stranded";
  const reconnectIsOwner = liveness.kind === "host_offline" ? liveness.isOwner : true;

  return {
    kind: "session",
    urlConvId,
    isSessionShared,
    bubbles,
    status,
    isWorking,
    showsWorking,
    runnerOnline,
    liveness,
    agentsError,
    disabled: !agentId || agentsError !== null || networkOffline,
    onSend,
    onSendSlashCommand,
    onStop,
    onShowReconnectHelp: () => {
      if (isUnboundFork) setResumeDirDialogOpen(true);
      else setReconnectDialogOpen(true);
    },
    visibleAgents,
    agentsLoading,
    agentId,
    onSelectAgent: setSelectedAgentId,
    hasMoreHistory,
    loadingMoreHistory,
    permissionLevel,
    readOnlyReason,
    capabilitySource,
    costRoutingVerdict,
    costRoutingEligible,
    subAgentLabel,
    reconnectDialogOpen,
    setReconnectDialogOpen,
    reconnectState,
    reconnectIsOwner,
    activeConvTitle: activeConv?.title,
    activeSessionTitle: activeSession?.title,
    activeSessionWorkspace: activeSession?.workspace,
    activeSessionHostId: activeSession?.hostId,
    activeSessionGitBranch: activeSession?.gitBranch,
    activeConvWrapper: activeConv?.labels?.["omnigent.wrapper"],
    isUnboundFork,
    forkSourceId,
    resumeDirDialogOpen,
    setResumeDirDialogOpen,
  };
}