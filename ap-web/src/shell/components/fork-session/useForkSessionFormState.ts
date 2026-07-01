import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import { forkSession, launchRunner } from "@/lib/sessionsApi";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useSessionAgent } from "@/hooks/useAgents";
import { useHosts } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { forkTargetCarriesHistory } from "@/lib/forkHarness";
import { getCliServerUrl } from "@/lib/host";
import {
  isValidWorkspace,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
} from "../new-chat-landing/newChatLandingUtils";
import { SAME_AS_SOURCE } from "./forkSessionConstants";
import { defaultForkTitle } from "./forkSessionUtils";

export function useForkSessionFormState({
  sourceSessionId,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
  upToResponseId,
  onClose,
}: {
  sourceSessionId: string;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
  upToResponseId?: string | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [agentChoice, setAgentChoice] = useState<string>(SAME_AS_SOURCE);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const autoExpandedRef = useRef(false);

  const isCodingSource = Boolean(sourceWorkspace);
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [branchName, setBranchName] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  const [showConnect, setShowConnect] = useState(false);

  const { data: agents } = useAvailableAgents({ enabled: true });
  const { data: sourceAgent } = useSessionAgent(sourceSessionId);
  const { data: hosts } = useHosts({ enabled: isCodingSource });
  const allHosts = hosts ?? [];
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);
  const offlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "offline"), [hosts]);
  const sourceHostOnline = onlineHosts.some((h) => h.host_id === sourceHostId);
  const serverUrl = getCliServerUrl();
  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  const onSourceHost = isCodingSource && selectedHostId !== null && selectedHostId === sourceHostId;
  const onDifferentHost =
    isCodingSource && selectedHostId !== null && selectedHostId !== sourceHostId;

  const sourceAgentName = sourceAgent?.name ?? null;
  const sourceAgentBaseName = sourceAgentName?.replace(/ \(fork [^)]+\)$/, "") ?? null;
  const sourceAgentDisplay =
    (agents ?? []).find(
      (a) =>
        a.id === sourceAgent?.id || a.name === sourceAgentName || a.name === sourceAgentBaseName,
    )?.display_name ??
    sourceAgentBaseName ??
    sourceAgentName ??
    "the original agent";

  const switchableAgents = (agents ?? []).filter(
    (a) =>
      a.id !== sourceAgent?.id &&
      a.name !== sourceAgentName &&
      a.name !== sourceAgentBaseName &&
      forkTargetCarriesHistory(a.harness),
  );
  const switching = agentChoice !== SAME_AS_SOURCE;

  useEffect(() => {
    if (!isCodingSource || selectedHostId !== null) return;
    if (sourceHostId && sourceHostOnline) setSelectedHostId(sourceHostId);
    else if (onlineHosts.length > 0) setSelectedHostId(onlineHosts[0].host_id);
  }, [isCodingSource, selectedHostId, sourceHostId, sourceHostOnline, onlineHosts]);

  useEffect(() => {
    if (onSourceHost && workspace === "" && sourceWorkspace) setWorkspace(sourceWorkspace);
  }, [onSourceHost, workspace, sourceWorkspace]);

  useEffect(() => {
    if (onSourceHost && baseBranch === "" && sourceGitBranch) setBaseBranch(sourceGitBranch);
  }, [onSourceHost, baseBranch, sourceGitBranch]);

  const workspaceTrimmed = normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = isValidWorkspace(workspace);
  const selectedHostOnline =
    selectedHostId !== null && onlineHosts.some((h) => h.host_id === selectedHostId);
  const canSubmit = !isCodingSource || (selectedHostOnline && workspaceValid);

  const { data: directorySessions } = useDirectorySessions(
    isCodingSource && Boolean(selectedHostId),
  );
  const conflictCandidates = useMemo(
    () =>
      isCodingSource
        ? (directorySessions ?? []).filter(
            (s) => s.host_id === selectedHostId && s.workspace != null,
          )
        : [],
    [isCodingSource, directorySessions, selectedHostId],
  );
  const runnerHealth = useRunnerHealthRegistration(conflictCandidates);
  const conflictingSessions = useMemo(
    () =>
      sessionsSharingDirectory(
        conflictCandidates,
        selectedHostId,
        workspaceTrimmed,
        (id) => runnerHealth.get(id) === true,
      ),
    [conflictCandidates, selectedHostId, workspaceTrimmed, runnerHealth],
  );
  const showConflictHint = branchName.trim() === "" && conflictingSessions.length > 0;

  useEffect(() => {
    if (onDifferentHost && !autoExpandedRef.current) {
      autoExpandedRef.current = true;
      setShowAdvanced(true);
    }
  }, [onDifferentHost]);

  const sourceWorkspaceNorm = sourceWorkspace ? normalizeWorkspacePath(sourceWorkspace) : null;
  const hostMismatch =
    sourceHostId != null && selectedHostId !== null && selectedHostId !== sourceHostId;
  const showMismatchWarning =
    isCodingSource &&
    ((hostMismatch && workspaceTrimmed !== "") ||
      (sourceWorkspaceNorm !== null &&
        workspaceTrimmed !== "" &&
        workspaceTrimmed !== sourceWorkspaceNorm));
  const usingSourceDir = onSourceHost && workspaceTrimmed !== "" && !showMismatchWarning;
  const namePlaceholder = defaultForkTitle(sourceTitle) || "Name the cloned session";

  function commitWorkspacePath(path: string): void {
    setWorkspace(path);
    setBrowsing(true);
    setBrowseNonce((n) => n + 1);
  }

  async function handleFork(): Promise<void> {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const trimmed = title.trim();
      const fork = await forkSession(
        sourceSessionId,
        trimmed === "" ? undefined : trimmed,
        switching ? agentChoice : undefined,
        upToResponseId ?? undefined,
      );
      if (isCodingSource && selectedHostId) {
        const trimmedBranch = branchName.trim();
        addRecent(workspaceTrimmed);
        void launchRunner(
          selectedHostId,
          fork.id,
          workspaceTrimmed,
          trimmedBranch
            ? { branchName: trimmedBranch, baseBranch: baseBranch.trim() || undefined }
            : undefined,
        ).catch((e) => {
          console.warn(`Clone ${fork.id}: background runner launch failed`, e);
        });
      }
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      onClose();
      navigate(`/c/${fork.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't clone the session. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return {
    title,
    setTitle,
    agentChoice,
    setAgentChoice,
    submitting,
    error,
    showAdvanced,
    setShowAdvanced,
    isCodingSource,
    selectedHostId,
    setSelectedHostId,
    workspace,
    setWorkspace,
    branchName,
    setBranchName,
    baseBranch,
    setBaseBranch,
    browsing,
    setBrowsing,
    browseNonce,
    showConnect,
    setShowConnect,
    hosts,
    allHosts,
    onlineHosts,
    offlineHosts,
    serverUrl,
    recent,
    sourceAgentDisplay,
    switchableAgents,
    switching,
    workspaceTrimmed,
    canSubmit,
    showConflictHint,
    conflictingSessions,
    showMismatchWarning,
    usingSourceDir,
    namePlaceholder,
    commitWorkspacePath,
    handleFork,
  };
}