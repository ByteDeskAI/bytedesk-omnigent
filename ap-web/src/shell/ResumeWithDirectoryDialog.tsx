import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CliCommandBlock } from "./CliCommandBlock";
import { buildReconnectCommand } from "./ReconnectSessionDialog";
import { ResumeWithDirectoryForm } from "./components/resume-with-directory/ResumeWithDirectoryForm";
import { useResumeWithDirectoryForm } from "./components/resume-with-directory/useResumeWithDirectoryForm";

/**
 * Dialog surfaced when the user tries to chat with an unbound *coding*
 * clone (a fork of a session that had a working directory — it carries
 * the ``omnigent.fork.source_id`` label). Unlike ``ResumeChatDialog``
 * (which only prints a CLI command), this binds the clone to a host +
 * directory in-app via ``POST /v1/hosts/{id}/runners`` (``launchRunner``)
 * and lets the runner start, after which ChatPage replays the queued
 * message.
 */
export function ResumeWithDirectoryDialog({
  open,
  onOpenChange,
  sessionId,
  sourceSessionId,
  serverUrl,
  wrapper,
  onBound,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  sourceSessionId: string;
  serverUrl: string;
  wrapper?: string | null;
  onBound?: () => void;
}) {
  const state = useResumeWithDirectoryForm({
    open,
    sessionId,
    sourceSessionId,
    onOpenChange,
    onBound,
  });

  return (
    <Dialog open={open} onOpenChange={state.handleOpenChange}>
      <DialogContent data-testid="resume-dir-dialog" className="flex flex-col gap-4 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Resume this session</DialogTitle>
          <DialogDescription>
            This clone hasn't picked a working directory yet. Choose a host and directory to
            continue the conversation against your files.
          </DialogDescription>
        </DialogHeader>

        {state.sourceLoading || !state.hostsLoaded ? (
          <p className="text-xs text-muted-foreground" data-testid="resume-dir-loading">
            Loading the original session's directory…
          </p>
        ) : state.showCliFallback ? (
          <div className="flex flex-col gap-2" data-testid="resume-dir-cli-fallback">
            <p className="text-xs text-muted-foreground">
              The original session's host is offline, so there's nothing to launch a runner on.
              Reconnect it from your terminal — then send your message again to pick a directory.
            </p>
            <CliCommandBlock
              command={buildReconnectCommand({
                conversationId: sessionId,
                serverUrl,
                wrapper,
                state: state.sourceHostId ? "host_offline" : "local_stranded",
              })}
              testIdPrefix="resume-dir"
            />
          </div>
        ) : (
          <ResumeWithDirectoryForm state={state} />
        )}
      </DialogContent>
    </Dialog>
  );
}