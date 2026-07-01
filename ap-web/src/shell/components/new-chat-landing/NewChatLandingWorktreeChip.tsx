import { ChevronDownIcon, GitBranchIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { NewChatLandingState } from "./useNewChatLandingState";

export function NewChatLandingWorktreeChip({ state }: { state: NewChatLandingState }) {
  const { branchName, worktreeLabel, setBranchName, baseBranch, setBaseBranch } = state;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex h-6 items-center gap-1.5 rounded-full px-3 text-13 font-normal text-muted-foreground transition-colors hover:text-foreground"
          data-testid="new-chat-landing-branch-chip"
        >
          <GitBranchIcon className="size-4 shrink-0" />
          <span className={`max-w-32 truncate ${branchName.trim() ? "text-foreground" : ""}`}>
            {worktreeLabel}
          </span>
          <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(20rem,calc(100vw-2rem))] p-3">
        <div className="flex flex-col gap-2">
          <label htmlFor="landing-branch-name" className="text-xs font-medium text-foreground">
            Git worktree branch (optional)
          </label>
          <input
            id="landing-branch-name"
            type="text"
            value={branchName}
            onChange={(e) => setBranchName(e.target.value)}
            placeholder="feature/my-branch"
            className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
            data-testid="new-chat-landing-branch-input"
          />
          {branchName.trim() !== "" && (
            <input
              type="text"
              value={baseBranch}
              onChange={(e) => setBaseBranch(e.target.value)}
              placeholder="Base branch (defaults to current branch)"
              aria-label="Base branch"
              className="rounded-md border border-input bg-background px-3 py-2 text-xs outline-none transition-colors focus-visible:border-ring"
              data-testid="new-chat-landing-base-branch-input"
            />
          )}
          <p className="text-xs text-muted-foreground">
            Creates an isolated git worktree for a new branch. Leave blank to start directly in the
            working directory.
          </p>
        </div>
      </PopoverContent>
    </Popover>
  );
}