import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useHosts } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { getSessionSlim, launchRunner } from "@/lib/sessionsApi";
import {
  isValidWorkspace,
  normalizeWorkspacePath,
  sessionsSharingDirectory,
} from "../../NewChatDialog";

export interface UseResumeWithDirectoryFormArgs {
  open: boolean;
  sessionId: string;
  sourceSessionId: string;
  onOpenChange: (open: boolean) => void;
  onBound?: () => void;
}

export function useResumeWithDirectoryForm({
  open,
  sessionId,
  sourceSessionId,
  onOpenChange,
  onBound,
}: UseResumeWithDirectoryFormArgs) {
  const queryClient = useQueryClient();

  const { data: source, isLoading: sourceLoading } = useQuery({
    queryKey: ["session", sourceSessionId],
    queryFn: () => getSessionSlim(sourceSessionId),
    enabled: open,
  });
  const { data: hosts } = useHosts({ enabled: open });

  const sourceHostId = source?.hostId ?? null;
  const sourceHost = useMemo(
    () => hosts?.find((h) => h.host_id === sourceHostId) ?? null,
    [hosts, sourceHostId],
  );
  const sourceHostOnline = sourceHost?.status === "online";
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);

  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [branchName, setBranchName] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [browseNonce, setBrowseNonce] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  useEffect(() => {
    if (open && selectedHostId === null && sourceHostId && sourceHostOnline) {
      setSelectedHostId(sourceHostId);
    }
  }, [open, selectedHostId, sourceHostId, sourceHostOnline]);

  useEffect(() => {
    if (open && workspace === "" && source?.workspace) {
      setWorkspace(source.workspace);
    }
  }, [open, workspace, source?.workspace]);

  useEffect(() => {
    if (open && baseBranch === "" && source?.gitBranch) {
      setBaseBranch(source.gitBranch);
    }
  }, [open, baseBranch, source?.gitBranch]);

  function resetTransientState() {
    setSelectedHostId(null);
    setWorkspace("");
    setBranchName("");
    setBaseBranch("");
    setBrowsing(false);
    setError(null);
    setSubmitting(false);
  }

  function handleOpenChange(next: boolean): void {
    if (!next) resetTransientState();
    onOpenChange(next);
  }

  const workspaceTrimmed = normalizeWorkspacePath(workspace) ?? "";
  const workspaceValid = isValidWorkspace(workspace);

  const { data: directorySessions } = useDirectorySessions(open && Boolean(selectedHostId));
  const conflictCandidates = useMemo(
    () =>
      open
        ? (directorySessions ?? []).filter(
            (s) => s.host_id === selectedHostId && s.workspace != null,
          )
        : [],
    [open, directorySessions, selectedHostId],
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

  const sourceWorkspaceNorm = source?.workspace ? normalizeWorkspacePath(source.workspace) : null;
  const hostMismatch =
    sourceHostId !== null && selectedHostId !== null && selectedHostId !== sourceHostId;
  const showMismatchWarning =
    (hostMismatch && workspaceTrimmed !== "") ||
    (sourceWorkspaceNorm !== null &&
      workspaceTrimmed !== "" &&
      workspaceTrimmed !== sourceWorkspaceNorm);

  function commitWorkspacePath(path: string): void {
    setWorkspace(path);
    setBrowsing(true);
    setBrowseNonce((n) => n + 1);
  }

  async function handleBind(): Promise<void> {
    if (!selectedHostId || !workspaceValid) return;
    setSubmitting(true);
    setError(null);
    try {
      const trimmedBranch = branchName.trim();
      await launchRunner(
        selectedHostId,
        sessionId,
        workspaceTrimmed,
        trimmedBranch
          ? { branchName: trimmedBranch, baseBranch: baseBranch.trim() || undefined }
          : undefined,
      );
      addRecent(workspaceTrimmed);
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      handleOpenChange(false);
      onBound?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't start the session. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const hostsLoaded = hosts !== undefined;
  const showCliFallback = !sourceLoading && hostsLoaded && source != null && !sourceHostOnline;

  return {
    source,
    sourceHostId,
    sourceLoading,
    hostsLoaded,
    showCliFallback,
    onlineHosts,
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
    submitting,
    error,
    recent,
    workspaceTrimmed,
    workspaceValid,
    showConflictHint,
    conflictingSessions,
    showMismatchWarning,
    commitWorkspacePath,
    handleBind,
    handleOpenChange,
  };
}