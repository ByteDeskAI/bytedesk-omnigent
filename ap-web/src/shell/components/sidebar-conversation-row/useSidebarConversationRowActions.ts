import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "@/lib/routing";
import {
  type Conversation,
  useArchiveConversation,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
} from "@/hooks/useConversations";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { isSessionStoppable } from "@/lib/sessionStop";
import { isOwnedByViewer } from "../sidebarConversationConstants";

export function useSidebarConversationRowActions(conversation: Conversation) {
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const activeIdRef = useRef(activeId);
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  const rename = useRenameConversation();
  const del = useStopAndDeleteConversation();
  const archive = useArchiveConversation();
  const stopForArchive = useStopSession();
  const stopSession = useStopSession();

  const isArchived = conversation.archived === true;
  const [isEditing, setIsEditing] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [isArchiving, setIsArchiving] = useState(false);

  const gitBranch = conversation.git_branch ?? null;
  const isOwner = isOwnedByViewer(conversation);
  const canEdit = conversation.permission_level === null || conversation.permission_level >= 2;
  const canManage = conversation.permission_level === null || conversation.permission_level >= 3;
  const runnerOnline = useSessionRunnerOnline(conversation.id);
  const canStop =
    isSessionStoppable({
      labels: conversation.labels,
      hostId: conversation.host_id,
      runnerId: conversation.runner_id,
    }) && runnerOnline !== false;

  function runDelete(args: { id: string; deleteBranch?: boolean }) {
    del.mutate(args, {
      onSuccess: () => {
        if (activeIdRef.current === conversation.id) navigate("/", { replace: true });
      },
    });
  }

  function confirmDelete() {
    const args = { id: conversation.id, deleteBranch: gitBranch !== null && deleteBranch };
    setDeleteOpen(false);
    setDeleteBranch(false);
    runDelete(args);
  }

  function runArchive() {
    const nextArchived = !isArchived;
    if (!nextArchived) {
      archive.mutate({ id: conversation.id, archived: false });
      return;
    }
    setIsArchiving(true);
    stopForArchive.mutate(conversation.id, {
      onSettled: () => {
        archive.mutate(
          { id: conversation.id, archived: true },
          { onSettled: () => setIsArchiving(false) },
        );
      },
    });
  }

  return {
    activeId,
    rename,
    del,
    isArchived,
    isEditing,
    setIsEditing,
    deleteOpen,
    setDeleteOpen,
    stopOpen,
    setStopOpen,
    deleteBranch,
    setDeleteBranch,
    shareOpen,
    setShareOpen,
    isArchiving,
    gitBranch,
    isOwner,
    canEdit,
    canManage,
    canStop,
    stopSession,
    runDelete,
    confirmDelete,
    runArchive,
  };
}