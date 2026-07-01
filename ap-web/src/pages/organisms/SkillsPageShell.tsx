import { PuzzleIcon, XIcon } from "lucide-react";
import { AgentConversation, AgentComposer } from "@/components/chat";
import { CatalogSearchPanel, ScopePanel } from "@/components/skills";
import type { DepartmentGroup, SkillScope } from "@/components/skills/skills-utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type {
  InstalledSkill,
  SkillMarketplace,
  SkillRecommendation,
} from "@/hooks/useSkills";
import { Link } from "@/lib/routing";

export interface SkillsPageShellProps {
  selectedScopeLabel: string;
  selectedScope: SkillScope;
  setSelectedScope: (scope: SkillScope) => void;
  agentRows: AvailableAgent[];
  departmentGroups: DepartmentGroup[];
  targetAgentIds: string[];
  concierge: AvailableAgent | null;
  agents: AvailableAgent[];
  agentsLoading: boolean;
  marketplaces: SkillMarketplace[];
  recommendations: SkillRecommendation[];
  recommendationsLoading: boolean;
  scopedInstalled: InstalledSkill[];
  installedLoading: boolean;
}

export function SkillsPageShell({
  selectedScopeLabel,
  selectedScope,
  setSelectedScope,
  agentRows,
  departmentGroups,
  targetAgentIds,
  concierge,
  agents,
  agentsLoading,
  marketplaces,
  recommendations,
  recommendationsLoading,
  scopedInstalled,
  installedLoading,
}: SkillsPageShellProps) {
  return (
    <div className="fixed inset-3 z-50 flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <PuzzleIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Skills</h1>
            <p className="truncate text-xs text-muted-foreground">{selectedScopeLabel}</p>
          </div>
        </div>
        <Button variant="ghost" size="icon" asChild aria-label="Close skills">
          <Link to="/">
            <XIcon />
          </Link>
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[18rem_minmax(0,1fr)_22rem]">
        <ScopePanel
          selectedScope={selectedScope}
          setSelectedScope={setSelectedScope}
          agentRows={agentRows}
          departmentGroups={departmentGroups}
          targetCount={targetAgentIds.length}
        />

        <main className="flex min-h-0 flex-col overflow-hidden border-b border-border lg:border-r lg:border-b-0">
          {concierge ? (
            <>
              <AgentConversation />
              <AgentComposer
                agentId={concierge.id}
                agents={agents}
                agentsLoading={agentsLoading}
              />
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center px-6 text-center">
              <p className="max-w-sm text-sm text-muted-foreground">
                Skills assistant isn't available yet.
              </p>
            </div>
          )}
        </main>

        <aside className="min-h-0 overflow-auto">
          <div className="space-y-4 p-3">
            <section className="rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <h2 className="text-sm font-medium">Catalog</h2>
                <Badge variant="secondary">GitHub</Badge>
              </div>
              <div className="space-y-3 px-3 py-3">
                <div className="flex flex-wrap gap-1.5">
                  {marketplaces
                    .filter((entry) => entry.source_id === "github_marketplace")
                    .map((entry) => (
                      <Badge key={entry.id} variant={entry.default ? "default" : "outline"}>
                        {entry.label}
                      </Badge>
                    ))}
                </div>
                <CatalogSearchPanel targetAgentIds={targetAgentIds} />
                <div className="space-y-2">
                  <div className="text-xs font-medium text-muted-foreground">Suggested plugins</div>
                  {recommendations.slice(0, 5).map((item) => (
                    <div key={item.source_ref} className="rounded-md border border-border px-2.5 py-2">
                      <div className="text-sm font-medium">{item.name}</div>
                      <div className="line-clamp-2 text-xs text-muted-foreground">{item.reason}</div>
                    </div>
                  ))}
                  {!recommendationsLoading && recommendations.length === 0 && (
                    <div className="text-xs text-muted-foreground">No suggestions for this scope.</div>
                  )}
                </div>
              </div>
            </section>

            <section className="rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <h2 className="text-sm font-medium">Installed Skills</h2>
                <span className="text-xs text-muted-foreground">{scopedInstalled.length}</span>
              </div>
              <div className="max-h-[30rem] divide-y divide-border overflow-y-auto">
                {scopedInstalled.map((skill) => (
                  <div key={skill.name} className="px-3 py-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{skill.name}</div>
                      <div className="line-clamp-2 text-xs text-muted-foreground">
                        {skill.description}
                      </div>
                    </div>
                    <div className="mt-2 text-xs text-muted-foreground">
                      {skill.agents.length} in scope
                    </div>
                  </div>
                ))}
                {!installedLoading && scopedInstalled.length === 0 && (
                  <div className="px-3 py-6 text-sm text-muted-foreground">
                    No skills available for this scope.
                  </div>
                )}
              </div>
            </section>
          </div>
        </aside>
      </div>
    </div>
  );
}