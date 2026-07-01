import { AlertTriangleIcon, ChevronDownIcon, ChevronUpIcon, GitBranchIcon } from "lucide-react";
import { WorkspacePicker, isNavigablePath } from "../../WorkspacePicker";
import { WorkspacePathField } from "../../WorkspacePathField";

export function ForkSessionAdvancedSection({
  showAdvanced,
  setShowAdvanced,
  title,
  setTitle,
  namePlaceholder,
  isCodingSource,
  selectedHostId,
  workspace,
  setWorkspace,
  workspaceTrimmed,
  browsing,
  setBrowsing,
  browseNonce,
  recent,
  commitWorkspacePath,
  showMismatchWarning,
  branchName,
  setBranchName,
  baseBranch,
  setBaseBranch,
  submitting,
  canSubmit,
  onSubmit,
}: {
  showAdvanced: boolean;
  setShowAdvanced: (v: boolean | ((prev: boolean) => boolean)) => void;
  title: string;
  setTitle: (v: string) => void;
  namePlaceholder: string;
  isCodingSource: boolean;
  selectedHostId: string | null;
  workspace: string;
  setWorkspace: (v: string) => void;
  workspaceTrimmed: string;
  browsing: boolean;
  setBrowsing: (v: boolean | ((prev: boolean) => boolean)) => void;
  browseNonce: number;
  recent: string[];
  commitWorkspacePath: (path: string) => void;
  showMismatchWarning: boolean;
  branchName: string;
  setBranchName: (v: string) => void;
  baseBranch: string;
  setBaseBranch: (v: string) => void;
  submitting: boolean;
  canSubmit: boolean;
  onSubmit: () => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <button
        type="button"
        onClick={() => setShowAdvanced((v) => !v)}
        className="flex cursor-pointer items-center gap-1 self-start text-xs font-medium text-foreground transition hover:text-foreground"
        data-testid="fork-session-advanced-toggle"
        aria-expanded={showAdvanced}
        aria-controls="fork-session-advanced-content"
      >
        {showAdvanced ? <ChevronUpIcon className="size-3.5" /> : <ChevronDownIcon className="size-3.5" />}
        Advanced settings
      </button>

      {showAdvanced && (
        <div
          id="fork-session-advanced-content"
          className="flex flex-col gap-4"
          data-testid="fork-session-advanced-content"
        >
          <div className="flex flex-col gap-1.5">
            <label htmlFor="fork-session-title" className="text-xs font-medium text-muted-foreground">
              Name (optional)
            </label>
            <input
              id="fork-session-title"
              data-testid="fork-session-title-input"
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !submitting && canSubmit) onSubmit();
              }}
              placeholder={namePlaceholder}
              className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
            />
          </div>

          {isCodingSource && (
            <>
              <div className="flex flex-col gap-2">
                <span className="text-xs font-medium text-muted-foreground">Working directory</span>
                {selectedHostId ? (
                  <>
                    <WorkspacePathField
                      hostId={selectedHostId}
                      value={workspace}
                      onChange={setWorkspace}
                      onBrowse={() => setBrowsing((v) => !v)}
                      onCommit={commitWorkspacePath}
                      recent={recent}
                      dropdownDisabled={browsing}
                    />
                    {browsing && (
                      <WorkspacePicker
                        key={browseNonce}
                        hostId={selectedHostId}
                        initialPath={
                          isNavigablePath(workspaceTrimmed) ? workspaceTrimmed : undefined
                        }
                        onSelect={(path) => {
                          setWorkspace(path);
                          setBrowsing(false);
                        }}
                        onClose={() => setBrowsing(false)}
                      />
                    )}
                    {showMismatchWarning && (
                      <p
                        className="flex items-start gap-1.5 text-xs text-warning"
                        data-testid="fork-session-mismatch-warning"
                      >
                        <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
                        <span>
                          This directory differs from the original session's. Earlier file references
                          in the transcript may not apply — the agent will need to re-orient.
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
                  htmlFor="fork-session-branch"
                  className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
                >
                  <GitBranchIcon className="size-3.5" />
                  Git worktree (optional)
                </label>
                <input
                  id="fork-session-branch"
                  type="text"
                  value={branchName}
                  onChange={(e) => setBranchName(e.target.value)}
                  placeholder="feature/my-branch"
                  data-testid="fork-session-branch-input"
                  className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                />
                {branchName.trim() !== "" && (
                  <input
                    id="fork-session-base-branch"
                    type="text"
                    value={baseBranch}
                    onChange={(e) => setBaseBranch(e.target.value)}
                    placeholder="Base branch (defaults to the current branch)"
                    aria-label="Base branch"
                    data-testid="fork-session-base-branch-input"
                    className="rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none transition-colors focus-visible:border-ring"
                  />
                )}
                <p className="text-xs text-muted-foreground">
                  Creates a git worktree for a new branch in an isolated directory — keeps the clone
                  from fighting the original over the same files. Leave blank to start in the picked
                  directory.
                </p>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}