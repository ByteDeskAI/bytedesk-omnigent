import { CircleHelpIcon, ChevronDownIcon, GitBranchIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingSandboxRepoChip({ state }: { state: NewChatLandingState }) {
  const {
    sandboxRepoLabel,
    sandboxRepoName,
    sandboxRepoUrl,
    setSandboxRepoUrl,
    sandboxRepoBranch,
    setSandboxRepoBranch,
    databricksGitCredentialsTooltipContent,
  } = state;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
          data-testid="new-chat-landing-repo-chip"
        >
          <GitBranchIcon className="size-4 shrink-0" />
          <span
            className={`max-w-40 truncate ${sandboxRepoName ? "text-foreground" : "text-muted-foreground"}`}
          >
            {sandboxRepoLabel}
          </span>
          <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-96 p-3">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-1.5">
            <label htmlFor="landing-repo-url" className="text-xs font-medium text-foreground">
              Repository (optional)
            </label>
            {databricksGitCredentialsTooltipContent && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    className="inline-flex size-4 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground"
                    aria-label="How to set up Databricks git credentials"
                  >
                    <CircleHelpIcon className="size-3.5" />
                  </button>
                </TooltipTrigger>
                <TooltipContent className="max-w-64">
                  {databricksGitCredentialsTooltipContent}
                </TooltipContent>
              </Tooltip>
            )}
          </div>
          <input
            id="landing-repo-url"
            type="text"
            value={sandboxRepoUrl}
            onChange={(e) => setSandboxRepoUrl(e.target.value)}
            placeholder="https://github.com/org/repo"
            className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
            data-testid="new-chat-landing-repo-input"
          />
          <input
            type="text"
            value={sandboxRepoBranch}
            onChange={(e) => setSandboxRepoBranch(e.target.value)}
            placeholder="Branch (defaults to the repo's default)"
            aria-label="Repository branch"
            className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
            data-testid="new-chat-landing-repo-branch-input"
          />
          <p className="text-xs text-muted-foreground">
            Cloned into the sandbox as the session's working directory. Leave blank to start in an
            empty workspace.
          </p>
        </div>
      </PopoverContent>
    </Popover>
  );
}