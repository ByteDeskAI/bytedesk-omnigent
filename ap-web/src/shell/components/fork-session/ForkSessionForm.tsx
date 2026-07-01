import { AlertTriangleIcon } from "lucide-react";
import { DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ForkSessionAdvancedSection } from "./ForkSessionAdvancedSection";
import { ForkSessionAgentSection } from "./ForkSessionAgentSection";
import { ForkSessionHostSection } from "./ForkSessionHostSection";
import { useForkSessionFormState } from "./useForkSessionFormState";

export function ForkSessionForm({
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
  const state = useForkSessionFormState({
    sourceSessionId,
    sourceTitle,
    sourceWorkspace,
    sourceHostId,
    sourceGitBranch,
    upToResponseId,
    onClose,
  });

  const resetHostFields = () => {
    state.setWorkspace("");
    state.setBranchName("");
    state.setBaseBranch("");
    state.setBrowsing(false);
  };

  return (
    <>
      <div className="-mr-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pr-4 [scrollbar-width:thin] [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent">
        {state.isCodingSource && (
          <ForkSessionHostSection
            hosts={state.hosts}
            allHosts={state.allHosts}
            onlineHosts={state.onlineHosts}
            offlineHosts={state.offlineHosts}
            selectedHostId={state.selectedHostId}
            setSelectedHostId={state.setSelectedHostId}
            onHostChange={resetHostFields}
            serverUrl={state.serverUrl}
            showConnect={state.showConnect}
            setShowConnect={state.setShowConnect}
          />
        )}

        <ForkSessionAgentSection
          agentChoice={state.agentChoice}
          setAgentChoice={state.setAgentChoice}
          sourceAgentDisplay={state.sourceAgentDisplay}
          switchableAgents={state.switchableAgents}
          switching={state.switching}
        />

        {state.usingSourceDir && (
          <p className="text-xs text-muted-foreground" data-testid="fork-session-reuse-dir-hint">
            By default the clone reuses the original session's{" "}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="cursor-pointer underline decoration-dotted underline-offset-2"
                  data-testid="fork-session-reuse-dir-path"
                >
                  working directory
                </button>
              </TooltipTrigger>
              <TooltipContent className="font-mono break-all">{state.workspaceTrimmed}</TooltipContent>
            </Tooltip>
            . Open Advanced settings to change it.
          </p>
        )}

        {state.showConflictHint && (
          <p
            className="flex items-start gap-1.5 text-xs text-warning"
            data-testid="fork-session-conflict-hint"
          >
            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>
              {state.conflictingSessions.length === 1
                ? "1 other agent is"
                : `${state.conflictingSessions.length} other agents are`}{" "}
              working in this directory, so writes may conflict. Name a git branch under Advanced
              settings to work in an isolated copy.
            </span>
          </p>
        )}

        <ForkSessionAdvancedSection
          showAdvanced={state.showAdvanced}
          setShowAdvanced={state.setShowAdvanced}
          title={state.title}
          setTitle={state.setTitle}
          namePlaceholder={state.namePlaceholder}
          isCodingSource={state.isCodingSource}
          selectedHostId={state.selectedHostId}
          workspace={state.workspace}
          setWorkspace={state.setWorkspace}
          workspaceTrimmed={state.workspaceTrimmed}
          browsing={state.browsing}
          setBrowsing={state.setBrowsing}
          browseNonce={state.browseNonce}
          recent={state.recent}
          commitWorkspacePath={state.commitWorkspacePath}
          showMismatchWarning={state.showMismatchWarning}
          branchName={state.branchName}
          setBranchName={state.setBranchName}
          baseBranch={state.baseBranch}
          setBaseBranch={state.setBaseBranch}
          submitting={state.submitting}
          canSubmit={state.canSubmit}
          onSubmit={() => void state.handleFork()}
        />
      </div>

      {state.error !== null && (
        <p data-testid="fork-session-error" className="text-xs text-destructive">
          {state.error}
        </p>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={state.submitting}>
          Cancel
        </Button>
        <Button
          data-testid="fork-session-submit"
          onClick={() => void state.handleFork()}
          disabled={state.submitting || !state.canSubmit}
        >
          {state.submitting
            ? state.isCodingSource
              ? "Starting…"
              : "Cloning…"
            : state.isCodingSource
              ? "Clone & start"
              : "Clone"}
        </Button>
      </DialogFooter>
    </>
  );
}