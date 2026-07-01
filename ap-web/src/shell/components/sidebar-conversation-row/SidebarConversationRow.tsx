import { type MouseEvent } from "react";
import { type Conversation } from "@/hooks/useConversations";
import { getSessionState } from "@/hooks/useSessionState";
import { isConversationUnseen } from "@/hooks/useUnseenConversations";
import { conversationDisplayLabel } from "../../sidebarNav";
import { SidebarArchivingRow } from "../SidebarArchivingRow";
import { SidebarConversationEditRow } from "../SidebarConversationEditRow";
import { SidebarConversationRowControls } from "./SidebarConversationRowControls";
import { SidebarConversationRowDialogs } from "./SidebarConversationRowDialogs";
import { SidebarConversationRowLink } from "./SidebarConversationRowLink";
import { SidebarDeletingRow } from "../SidebarDeletingRow";
import { useSidebarConversationRowActions } from "./useSidebarConversationRowActions";

export function SidebarConversationRow({
  conversation,
  isPinned,
  onClick,
  onTogglePinned,
}: {
  conversation: Conversation;
  isPinned: boolean;
  onClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
}) {
  const actions = useSidebarConversationRowActions(conversation);
  const {
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
  } = actions;

  const isActive = activeId === conversation.id;
  const label = conversationDisplayLabel(conversation);
  const hasUnseenMessages =
    !isActive &&
    isConversationUnseen(conversation.id, conversation.updated_at, conversation.status);
  const derivedState = getSessionState(conversation);
  const sessionState =
    derivedState?.kind === "awaiting"
      ? derivedState
      : hasUnseenMessages
        ? { kind: "unseen" as const }
        : derivedState;

  if (isEditing) {
    return (
      <li>
        <SidebarConversationEditRow
          initialTitle={conversation.title ?? ""}
          onCommit={(title) => {
            const trimmed = title.trim();
            if (trimmed && trimmed !== (conversation.title ?? "")) {
              rename.mutate({ id: conversation.id, title: trimmed });
            }
            setIsEditing(false);
          }}
          onCancel={() => setIsEditing(false)}
        />
      </li>
    );
  }

  if (del.isPending || del.isError) {
    return (
      <li>
        <SidebarDeletingRow
          label={label}
          isError={del.isError}
          onRetry={() => del.variables && runDelete(del.variables)}
          onDismiss={() => del.reset()}
        />
      </li>
    );
  }

  if (isArchiving) {
    return (
      <li>
        <SidebarArchivingRow label={label} />
      </li>
    );
  }

  return (
    <li className="group relative">
      <SidebarConversationRowLink
        conversation={conversation}
        isActive={isActive}
        hasUnseenMessages={hasUnseenMessages}
        sessionStateKind={sessionState?.kind}
        canEdit={canEdit}
        onClick={onClick}
        onDoubleClickRename={() => setIsEditing(true)}
      />
      <SidebarConversationRowControls
        conversation={conversation}
        isPinned={isPinned}
        sessionState={sessionState}
        isOwner={isOwner}
        canManage={canManage}
        canEdit={canEdit}
        canStop={canStop}
        isArchived={isArchived}
        onTogglePinned={onTogglePinned}
        onArchive={runArchive}
        onShare={() => setShareOpen(true)}
        onRename={() => setIsEditing(true)}
        onStop={() => {
          stopSession.reset();
          setStopOpen(true);
        }}
        onDelete={() => setDeleteOpen(true)}
      />
      <SidebarConversationRowDialogs
        conversationId={conversation.id}
        label={label}
        gitBranch={gitBranch}
        shareOpen={shareOpen}
        setShareOpen={setShareOpen}
        deleteOpen={deleteOpen}
        setDeleteOpen={setDeleteOpen}
        deleteBranch={deleteBranch}
        setDeleteBranch={setDeleteBranch}
        deletePending={del.isPending}
        onConfirmDelete={confirmDelete}
        stopOpen={stopOpen}
        setStopOpen={setStopOpen}
        stopPending={stopSession.isPending}
        stopError={stopSession.isError}
        onConfirmStop={() =>
          stopSession.mutate(conversation.id, { onSuccess: () => setStopOpen(false) })
        }
      />
    </li>
  );
}