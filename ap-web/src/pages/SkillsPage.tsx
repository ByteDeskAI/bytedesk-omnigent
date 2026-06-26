import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  BotIcon,
  Building2Icon,
  NetworkIcon,
  PuzzleIcon,
  XIcon,
} from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  useApplySkillPreview,
  useCreateSkillPreview,
  useInstalledSkills,
  useSearchSkills,
  useSkillMarketplaces,
  useSkillRecommendations,
  useStartSkillsConciergeSession,
  type SkillPreview,
  type SkillSearchResult,
} from "@/hooks/useSkills";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { useHosts } from "@/hooks/useHosts";
import { AgentConversation, AgentComposer } from "@/components/chat";
import { Link } from "@/lib/routing";
import { bindOnlyOnlineRunner, launchRunner } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { cn } from "@/lib/utils";

type SkillScopeKind = "organization" | "department" | "employee";

interface SkillScope {
  kind: SkillScopeKind;
  id: string;
}

interface DepartmentGroup {
  id: string;
  agents: AvailableAgent[];
}

function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

function scopeMatchesAgent(scope: SkillScope, agent: AvailableAgent): boolean {
  if (scope.kind === "organization") return true;
  if (scope.kind === "department") return departmentId(agent) === scope.id;
  return agent.id === scope.id;
}

function scopeLabel(scope: SkillScope, agents: AvailableAgent[]): string {
  if (scope.kind === "organization") return "Organizational";
  if (scope.kind === "department") return scope.id;
  return agents.find((agent) => agent.id === scope.id)?.display_name ?? "Employee";
}

/**
 * The dedicated skills concierge built-in. Seeded agents carry a generated
 * `ag_…` id, so the stable handle is `name` ("skills-concierge"); fall back to
 * id, then a loose display-name match (display_name can be null on built-ins).
 */
function findConcierge(agents: AvailableAgent[]): AvailableAgent | null {
  return (
    agents.find((a) => a.name === "skills-concierge") ??
    agents.find((a) => a.id === "skills-concierge") ??
    agents.find((a) => /skills.?concierge/i.test(a.display_name ?? "")) ??
    null
  );
}

function homeWorkspaceFromEntries(entries: HostFilesystemEntry[]): string | null {
  const first = entries[0];
  if (!first) return null;
  const slash = first.path.lastIndexOf("/");
  if (slash < 0) return null;
  return slash === 0 ? "/" : first.path.slice(0, slash);
}

export function SkillsPage() {
  const agentsQuery = useAvailableAgents();
  const installed = useInstalledSkills();
  const marketplaces = useSkillMarketplaces();
  const { mutateAsync: startConciergeSession } = useStartSkillsConciergeSession();
  const agents = useMemo(() => agentsQuery.data ?? [], [agentsQuery.data]);
  const hosts = useHosts();
  const onlineHost = useMemo(
    () => (hosts.data ?? []).find((host) => host.status === "online") ?? null,
    [hosts.data],
  );
  const homeListing = useHostFilesystem(onlineHost?.host_id ?? null, onlineHost ? "" : null);
  const launchWorkspace = useMemo(
    () => homeWorkspaceFromEntries(homeListing.data?.entries ?? []),
    [homeListing.data],
  );

  const [selectedScope, setSelectedScope] = useState<SkillScope>({
    kind: "organization",
    id: "omnigent",
  });
  const hasDefaultedScope = useRef(false);

  // Employee rows, ordered by department then display name — so the Employee
  // list (which maps agentRows) and every department group read in the same
  // department → name order. Case-insensitive so "Operations"/"engineering"
  // sort naturally rather than by ASCII case.
  const agentRows = useMemo(
    () =>
      agents
        .filter((agent) => agent.workflow !== true && Boolean(agent.department || agent.title))
        .sort(
          (a, b) =>
            departmentId(a).localeCompare(departmentId(b), undefined, { sensitivity: "base" }) ||
            a.display_name.localeCompare(b.display_name, undefined, { sensitivity: "base" }),
        ),
    [agents],
  );

  const departmentGroups = useMemo<DepartmentGroup[]>(() => {
    const groups = new Map<string, AvailableAgent[]>();
    for (const agent of agentRows) {
      const department = departmentId(agent);
      groups.set(department, [...(groups.get(department) ?? []), agent]);
    }
    // agentRows is already department→name ordered, so each group's agents keep
    // that order; sort the departments themselves by name (case-insensitive).
    return [...groups.entries()]
      .map(([id, departmentAgents]) => ({ id, agents: departmentAgents }))
      .sort((a, b) => a.id.localeCompare(b.id, undefined, { sensitivity: "base" }));
  }, [agentRows]);

  const targetAgentIds = useMemo(
    () =>
      agentRows.filter((agent) => scopeMatchesAgent(selectedScope, agent)).map((agent) => agent.id),
    [agentRows, selectedScope],
  );

  useEffect(() => {
    if (!hasDefaultedScope.current && agentRows.length > 0) {
      setSelectedScope({ kind: "organization", id: "omnigent" });
      hasDefaultedScope.current = true;
      return;
    }
    if (
      agentRows.length > 0 &&
      selectedScope.kind !== "organization" &&
      !agentRows.some((agent) => scopeMatchesAgent(selectedScope, agent))
    ) {
      setSelectedScope({ kind: "organization", id: "omnigent" });
    }
  }, [agentRows, selectedScope]);

  const selectedScopeLabel = scopeLabel(selectedScope, agentRows);
  const concierge = useMemo(() => findConcierge(agents), [agents]);
  const scopeSessionKey = `${selectedScope.kind}:${selectedScope.id}`;

  useEffect(() => {
    if (!concierge || targetAgentIds.length === 0) return;
    let cancelled = false;
    void (async () => {
      try {
        const session = await startConciergeSession({
          target_kind: selectedScope.kind,
          target_id: selectedScope.id,
          target_label: selectedScopeLabel,
          target_agent_ids: targetAgentIds,
        });
        if (!cancelled) {
          await useChatStore.getState().switchTo(session.session_id);
          // Reuse a live runner when one exists; otherwise launch on the
          // selected host/home workspace so Skills sessions are host-bound and
          // can use the normal message-time relaunch path.
          const bound = await bindOnlyOnlineRunner(session.session_id);
          if (!bound && onlineHost && launchWorkspace) {
            await launchRunner(onlineHost.host_id, session.session_id, launchWorkspace);
          }
        }
      } catch {
        // Scope seed is best-effort; inline chat still works without it.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    concierge,
    scopeSessionKey,
    selectedScope.kind,
    selectedScope.id,
    selectedScopeLabel,
    startConciergeSession,
    targetAgentIds,
    onlineHost,
    launchWorkspace,
  ]);

  // The Skills panel shares the app's single global chat store. The scope-seed
  // effect above switches to a concierge-bound session once
  // POST /v1/skills/concierge/sessions exists; until then it fails and the panel
  // would otherwise inherit whatever conversation the user last had open (e.g. a
  // chat with Maya). chatStore.send only honors the composer's agentId when
  // STARTING a conversation — an already-active one keeps its bound agent — so an
  // inherited conversation silently routes Skills messages to the wrong agent.
  // Stash the caller's conversation and start a fresh chat on open so the first
  // send binds to the concierge; restore the previous conversation on close.
  useEffect(() => {
    const previousConversationId = useChatStore.getState().conversationId;
    void useChatStore.getState().switchTo(null);
    return () => {
      void useChatStore.getState().switchTo(previousConversationId);
    };
  }, []);

  const scopeContextAgent = useMemo(() => {
    if (selectedScope.kind === "employee") {
      return agentRows.find((agent) => agent.id === selectedScope.id) ?? null;
    }
    if (selectedScope.kind === "department") {
      return agentRows.find((agent) => departmentId(agent) === selectedScope.id) ?? null;
    }
    return agentRows[0] ?? null;
  }, [agentRows, selectedScope]);
  const recommendations = useSkillRecommendations(
    scopeContextAgent?.department ?? null,
    scopeContextAgent?.title ?? null,
  );

  const scopedInstalled = useMemo(
    () =>
      (installed.data ?? [])
        .map((skill) => ({
          ...skill,
          agents: skill.agents.filter((agent) => targetAgentIds.includes(agent.id)),
        }))
        .filter((skill) => skill.agents.length > 0),
    [installed.data, targetAgentIds],
  );

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
                agentsLoading={agentsQuery.isLoading}
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
                  {(marketplaces.data ?? [])
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
                  {(recommendations.data ?? []).slice(0, 5).map((item) => (
                    <div key={item.source_ref} className="rounded-md border border-border px-2.5 py-2">
                      <div className="text-sm font-medium">{item.name}</div>
                      <div className="line-clamp-2 text-xs text-muted-foreground">{item.reason}</div>
                    </div>
                  ))}
                  {!recommendations.isLoading && (recommendations.data ?? []).length === 0 && (
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
                {!installed.isLoading && scopedInstalled.length === 0 && (
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

function CatalogSearchPanel({ targetAgentIds }: { targetAgentIds: string[] }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillSearchResult[]>([]);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const search = useSearchSkills();
  const createPreview = useCreateSkillPreview();
  const applyPreview = useApplySkillPreview();

  const stageHit = async (source: string, sourceRef: string) => {
    setError(null);
    try {
      const staged = await createPreview.mutateAsync({
        target_agent_ids: targetAgentIds,
        source,
        source_ref: sourceRef,
        install_mode: "skip_existing",
      });
      setPreview(staged);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-muted-foreground">Search catalog</div>
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          void search
            .mutateAsync({ query, sources: ["github_marketplace"], limit: 8 })
            .then((response) => {
              setResults(response.data);
              setPreview(null);
            })
            .catch((err: unknown) => {
              setError(err instanceof Error ? err.message : "Search failed");
            });
        }}
      >
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="platform architect"
          aria-label="Search ByteDesk catalog"
        />
        <Button type="submit" size="sm" disabled={search.isPending || !query.trim()}>
          Search
        </Button>
      </form>
      {error && <div className="text-xs text-destructive">{error}</div>}
      <div className="space-y-1.5">
        {results.map((hit) => (
          <CatalogHitRow
            key={`${hit.source}:${hit.source_ref ?? hit.name}`}
            name={hit.name}
            description={hit.description ?? hit.source_ref}
            source={hit.source}
            sourceRef={hit.source_ref ?? ""}
            targetAgentIds={targetAgentIds}
            onStage={() => void stageHit(hit.source, hit.source_ref ?? "")}
            staging={createPreview.isPending}
          />
        ))}
      </div>
      {preview && (
        <div className="space-y-2 rounded-md border border-border bg-muted/20 p-2.5">
          <div className="text-xs font-medium">Preview ready</div>
          <div className="text-xs text-muted-foreground">
            {preview.skills.map((skill) => skill.name).join(", ")} →{" "}
            {preview.target_actions.filter((action) => action.action !== "skip").length} apply
          </div>
          <Button
            size="sm"
            disabled={applyPreview.isPending || targetAgentIds.length === 0}
            onClick={() => {
              void applyPreview
                .mutateAsync({ previewId: preview.id, targetAgentIds })
                .then(() => setPreview(null))
                .catch((err: unknown) => {
                  setError(err instanceof Error ? err.message : "Apply failed");
                });
            }}
          >
            Apply to scope
          </Button>
        </div>
      )}
    </div>
  );
}

function CatalogHitRow({
  name,
  description,
  source,
  sourceRef,
  targetAgentIds,
  onStage,
  staging = false,
}: {
  name: string;
  description: string | null;
  source: string;
  sourceRef: string;
  targetAgentIds: string[];
  onStage?: () => void;
  staging?: boolean;
}) {
  const createPreview = useCreateSkillPreview();
  const stage =
    onStage ??
    (() => {
      void createPreview.mutateAsync({
        target_agent_ids: targetAgentIds,
        source,
        source_ref: sourceRef,
        install_mode: "skip_existing",
      });
    });

  return (
    <button
      type="button"
      className="w-full rounded-md border border-border px-2.5 py-2 text-left hover:bg-muted/40"
      disabled={staging || createPreview.isPending || !sourceRef}
      onClick={stage}
    >
      <div className="text-sm font-medium">{name}</div>
      <div className="line-clamp-2 text-xs text-muted-foreground">{description}</div>
    </button>
  );
}

function ScopePanel({
  selectedScope,
  setSelectedScope,
  agentRows,
  departmentGroups,
  targetCount,
}: {
  selectedScope: SkillScope;
  setSelectedScope: (scope: SkillScope) => void;
  agentRows: AvailableAgent[];
  departmentGroups: DepartmentGroup[];
  targetCount: number;
}) {
  return (
    <aside className="min-h-0 border-b border-border lg:border-r lg:border-b-0">
      <div className="flex h-full min-h-0 flex-col">
        <div className="grid grid-cols-2 gap-2 border-b border-border p-3">
          <Metric value={agentRows.length} label="Employees" />
          <Metric value={targetCount} label="Targeted" />
        </div>
        <div className="shrink-0 border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
          Scope
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-2">
          <ScopeButton
            icon={<Building2Icon className="size-4" />}
            label="Organizational"
            subtitle="All employee agents"
            count={agentRows.length}
            selected={selectedScope.kind === "organization"}
            onClick={() => setSelectedScope({ kind: "organization", id: "omnigent" })}
          />

          <Accordion type="multiple" defaultValue={["departments", "employees"]}>
            <AccordionItem value="departments" className="border-0">
              <AccordionTrigger className="px-2 py-2 text-xs text-muted-foreground hover:no-underline">
                Departmental
              </AccordionTrigger>
              <AccordionContent className="pb-1">
                {departmentGroups.map((department) => (
                  <ScopeButton
                    key={department.id}
                    icon={<NetworkIcon className="size-4" />}
                    label={department.id}
                    subtitle="Department"
                    count={department.agents.length}
                    selected={
                      selectedScope.kind === "department" && selectedScope.id === department.id
                    }
                    onClick={() => setSelectedScope({ kind: "department", id: department.id })}
                  />
                ))}
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="employees" className="border-0">
              <AccordionTrigger className="px-2 py-2 text-xs text-muted-foreground hover:no-underline">
                Employee
              </AccordionTrigger>
              <AccordionContent className="pb-1">
                {agentRows.map((agent) => (
                  <ScopeButton
                    key={agent.id}
                    icon={<BotIcon className="size-4" />}
                    label={agent.display_name}
                    subtitle={agent.title || agent.name}
                    count={undefined}
                    selected={selectedScope.kind === "employee" && selectedScope.id === agent.id}
                    onClick={() => setSelectedScope({ kind: "employee", id: agent.id })}
                  />
                ))}
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </div>
    </aside>
  );
}

function ScopeButton({
  icon,
  label,
  subtitle,
  count,
  selected,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  subtitle: string;
  count: number | undefined;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "mb-1 flex min-h-12 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
        selected
          ? "border-border bg-muted text-foreground"
          : "border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
      )}
      aria-pressed={selected}
    >
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{label}</span>
        <span className="block truncate text-xs text-muted-foreground">{subtitle}</span>
      </span>
      {count !== undefined && <Badge variant="secondary">{count}</Badge>}
    </button>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-muted/30 px-2 py-1.5">
      <div className="truncate text-sm font-semibold tabular-nums">{value}</div>
      <div className="truncate text-[0.68rem] text-muted-foreground">{label}</div>
    </div>
  );
}
