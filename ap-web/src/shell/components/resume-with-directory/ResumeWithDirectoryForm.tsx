import { AlertTriangleIcon, GitBranchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DialogFooter } from "@/components/ui/dialog";
import { ForkSessionHostLabel } from "../fork-session/ForkSessionHostLabel";
import { WorkspacePicker, isNavigablePath } from "../../WorkspacePicker";
import { WorkspacePathField } from "../../WorkspacePathField";
import type { useResumeWithDirectoryForm } from "./useResumeWithDirectoryForm";

type FormState = ReturnType<typeof useResumeWithDirectoryForm>;

export function ResumeWithDirectoryForm({ state }: { state: FormState }) {
  return (
    <>
      <div className="flex flex-col gap-2">
        <span className="text-xs font-medium text-muted-foreground">Host</span>
        <Select value={state.selectedHostId ?? ""} onValueChange={(v) => state.setSelectedHostId(v)}>
          <SelectTrigger className="w-full text-xs" data-testid="resume-dir-host-select">
            <SelectValue placeholder="Select a host" />
          </SelectTrigger>
          <SelectContent>
            {state.onlineHosts.map((host) => (
              <SelectItem
                key={host.host_id}
                value={host.host_id}
                data-testid={`resume-dir-host-option-${host.host_id}`}
              >
                <ForkSessionHostLabel host={host} />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-xs font-medium text-muted-foreground">Working directory</span>
        {state.selectedHostId ? (
          <>
            <WorkspacePathField
              hostId={state.selectedHostId}
              value={state.workspace}
              onChange={state.setWorkspace}
              onBrowse={() => state.setBrowsing((v) => !v)}
              onCommit={state.commitWorkspacePath}
              recent={state.recent}
              dropdownDisabled={state.browsing}
            />
            {state.browsing && (
              <WorkspacePicker
                key={state.browseNonce}
                hostId={state.selectedHostId}
                initialPath={
                  isNavigablePath(state.workspaceTrimmed) ? state.workspaceTrimmed : undefined
                }
                onSelect={(path) => {
                  state.setWorkspace(path);
                  state.setBrowsing(false);
                }}
                onClose={() => state.setBrowsing(false)}
              />
            )}
            {state.showConflictHint && (
              <p
                className="flex items-start gap-1.5 text-xs text-warning"
                data-testid="resume-dir-conflict-hint"
              >
                <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                <span>
                  {state.conflictingSessions.length === 1
                    ? "1 other agent is"
                    : `${state.conflictingSessions.length} other agents are`}{" "}
                  working in this directory. Write operations may conflict. Name a git branch below
                  to work in an isolated copy.
                </span>
              </p>
            )}
            {state.showMismatchWarning && (
              <p
                className="flex items-start gap-1.5 text-xs text-warning"
                data-testid="resume-dir-mismatch-warning"
              >
                <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                <span>
                  This directory differs from the original session's. Earlier file references in the
                  transcript may not apply — the agent will need to re-orient.
                </span>
              </p>
            )}
          </>
        ) : (
          <p className="text-xs text-muted-foreground">Select a host to choose a directory.</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor="resume-dir-branch"
          className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
        >
          <GitBranchIcon className="size-3.5" />
          Git worktree (optional)
        </label>
        <input
          id="resume-dir-branch"
          type="text"
          value={state.branchName}
          onChange={(e) => state.setBranchName(e.target.value)}
          placeholder="feature/my-branch"
          data-testid="resume-dir-branch-input"
          className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
        />
        {state.branchName.trim() !== "" && (
          <input
            id="resume-dir-base-branch"
            type="text"
            value={state.baseBranch}
            onChange={(e) => state.setBaseBranch(e.target.value)}
            placeholder="Base branch (defaults to the current branch)"
            aria-label="Base branch"
            data-testid="resume-dir-base-branch-input"
            className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
          />
        )}
        <p className="text-xs text-muted-foreground">
          Creates a git worktree for a new branch in an isolated directory — keeps the clone from
          fighting the original over the same files. Leave blank to start in the picked directory.
        </p>
      </div>

      {state.error !== null && (
        <p className="text-xs text-destructive" data-testid="resume-dir-error">
          {state.error}
        </p>
      )}

      <DialogFooter>
        <Button
          data-testid="resume-dir-bind-button"
          disabled={!state.selectedHostId || !state.workspaceValid || state.submitting}
          onClick={() => void state.handleBind()}
        >
          {state.submitting ? "Starting…" : "Start session"}
        </Button>
      </DialogFooter>
    </>
  );
}