import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  BotIcon,
  FileTextIcon,
  FolderIcon,
  PlugIcon,
  PuzzleIcon,
  RefreshCwIcon,
  SaveIcon,
  SearchIcon,
  ShieldAlertIcon,
  SlidersHorizontalIcon,
  UsersIcon,
  WorkflowIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  useAgentImage,
  useAgentImageTree,
  useReadAgentImageFile,
  useUpdateAgentImage,
} from "@/hooks/useAgentImages";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  useConnectorAgentGrants,
  useConnectorsCatalog,
  useGrantConnectorToAgent,
} from "@/hooks/useConnectors";
import {
  useApplySkillPreview,
  useCreateSkillPreview,
  useInstalledSkills,
  useSearchSkills,
  type SkillPreview,
  type SkillSearchResult,
} from "@/hooks/useSkills";
import { getMe } from "@/lib/accountsApi";
import { groupAgentsByTier, tierForAgent, type AgentTier } from "@/lib/agentTiers";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { AgentImageUpdate } from "@/lib/agentImagesApi";
import type { ConnectorConnection, ConnectorManifest, ConnectorTool } from "@/lib/connectorsApi";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";

type WorkForceTab = "overview" | "config" | "skills" | "connectors" | "files";

interface PendingSave {
  body: AgentImageUpdate;
  label: string;
}

interface AvailableConnectorTool extends ConnectorTool {
  providerName: string;
  serviceKey: string;
  serviceName: string;
  token: string;
}

interface DepartmentGroup {
  department: string;
  agents: AvailableAgent[];
}

function agentDisplayName(agent: AvailableAgent): string {
  return agent.display_name || agent.name;
}

function compareText(a: string, b: string): number {
  return a.localeCompare(b, undefined, { sensitivity: "base" });
}

function compareAgentsByName(a: AvailableAgent, b: AvailableAgent): number {
  return compareText(agentDisplayName(a), agentDisplayName(b)) || compareText(a.name, b.name);
}

function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

function isWorkForceEmployee(agent: AvailableAgent): boolean {
  return tierForAgent(agent) === "employee" && Boolean(agent.department?.trim());
}

function workForceRosterAgents(agents: readonly AvailableAgent[]): AvailableAgent[] {
  return agents.filter((agent) => {
    const tier = tierForAgent(agent);
    return tier === "system" || tier === "workflow" || isWorkForceEmployee(agent);
  });
}

function groupEmployeesByDepartment(agents: readonly AvailableAgent[]): DepartmentGroup[] {
  const groups = new Map<string, AvailableAgent[]>();
  for (const agent of agents) {
    if (!isWorkForceEmployee(agent)) continue;
    const department = departmentId(agent);
    groups.set(department, [...(groups.get(department) ?? []), agent]);
  }
  return Array.from(groups, ([department, departmentAgents]) => ({
    department,
    agents: [...departmentAgents].sort(compareAgentsByName),
  })).sort((a, b) => compareText(a.department, b.department));
}

function tierLabel(tier: AgentTier): string {
  if (tier === "system") return "System Agents";
  if (tier === "workflow") return "Workflows";
  return "Employees";
}

function iconForTier(tier: AgentTier) {
  if (tier === "system") return <ShieldAlertIcon className="size-4" />;
  if (tier === "workflow") return <WorkflowIcon className="size-4" />;
  return <BotIcon className="size-4" />;
}

function parentPath(path: string): string {
  if (!path || path === ".") return "";
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

function useWorkForceAdminAccess() {
  const navigate = useNavigate();
  const info = useServerInfo();
  const [allowed, setAllowed] = useState<boolean | null>(null);

  useEffect(() => {
    if (info === "loading") return;
    if (!info.accounts_enabled) {
      setAllowed(true);
      return;
    }
    void (async () => {
      const me = await getMe();
      if (me === null) {
        navigate("/login", { replace: true });
        return;
      }
      setAllowed(me.is_admin);
    })();
  }, [info, navigate]);

  return allowed;
}

function WorkForceShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <div className="grid min-h-0 w-full grid-cols-1 lg:grid-cols-[19rem_minmax(0,1fr)]">
        {children}
      </div>
    </div>
  );
}

function AccessGate({ allowed, children }: { allowed: boolean | null; children: ReactNode }) {
  if (allowed === null) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }
  if (!allowed) {
    return (
      <div className="flex min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-5xl px-6 pt-14">
          <h1 className="text-2xl font-semibold">Work Force</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            You don't have permission to manage agents.
          </p>
        </div>
      </div>
    );
  }
  return children;
}

function RosterButton({
  agent,
  selected,
  onSelect,
}: {
  agent: AvailableAgent;
  selected: boolean;
  onSelect: () => void;
}) {
  const tier = tierForAgent(agent);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex min-h-14 w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors",
        selected
          ? "border-border bg-muted text-foreground"
          : "border-transparent text-muted-foreground hover:bg-muted/50 hover:text-foreground",
      )}
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        {iconForTier(tier)}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{agentDisplayName(agent)}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {agent.title || agent.department || agent.name}
        </span>
      </span>
      {tier === "workflow" && <Badge variant="outline">Read-only</Badge>}
    </button>
  );
}

function RosterPanel({
  agents,
  selectedAgentId,
  setSelectedAgentId,
  query,
  setQuery,
}: {
  agents: AvailableAgent[];
  selectedAgentId: string | null;
  setSelectedAgentId: (id: string) => void;
  query: string;
  setQuery: (query: string) => void;
}) {
  const groups = useMemo(() => groupAgentsByTier(agents), [agents]);
  const departmentGroups = useMemo(() => groupEmployeesByDepartment(agents), [agents]);
  const employeeCount = departmentGroups.reduce((count, group) => count + group.agents.length, 0);
  const systemAgents = useMemo(() => [...groups.system].sort(compareAgentsByName), [groups.system]);
  const workflowAgents = useMemo(
    () => [...groups.workflow].sort(compareAgentsByName),
    [groups.workflow],
  );
  const openDepartments = useMemo(
    () => departmentGroups.map((group) => `department:${group.department}`),
    [departmentGroups],
  );

  return (
    <aside
      aria-label="Agent roster"
      className="min-h-0 border-b border-border bg-background lg:border-r lg:border-b-0"
    >
      <div className="flex h-full min-h-0 flex-col">
        <header className="shrink-0 border-b border-border px-4 py-4">
          <div className="flex items-center gap-2">
            <span className="flex size-8 items-center justify-center rounded-md border border-border bg-muted">
              <UsersIcon className="size-4" />
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-base font-semibold">Work Force</h1>
              <p className="truncate text-xs text-muted-foreground">Agent directory control</p>
            </div>
          </div>
          <div className="relative mt-3">
            <SearchIcon className="absolute top-2 left-2 size-4 text-muted-foreground" />
            <Input
              className="pl-8"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search agents"
              aria-label="Search agents"
            />
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          <section className="mb-3">
            <div className="mb-1 flex items-center justify-between px-2 text-xs font-medium text-muted-foreground">
              <span>Employees</span>
              <span>{employeeCount}</span>
            </div>
            {departmentGroups.length > 0 ? (
              <Accordion type="multiple" defaultValue={openDepartments} className="gap-1">
                {departmentGroups.map((group) => (
                  <AccordionItem
                    key={group.department}
                    value={`department:${group.department}`}
                    className="border-0"
                  >
                    <AccordionTrigger
                      aria-label={`Department ${group.department}`}
                      className="rounded-md px-2 py-2 text-xs text-muted-foreground hover:bg-muted/40 hover:no-underline"
                    >
                      <span>{group.department}</span>
                      <Badge variant="secondary">{group.agents.length}</Badge>
                    </AccordionTrigger>
                    <AccordionContent className="space-y-1 pb-1">
                      {group.agents.map((agent) => (
                        <RosterButton
                          key={agent.id}
                          agent={agent}
                          selected={selectedAgentId === agent.id}
                          onSelect={() => setSelectedAgentId(agent.id)}
                        />
                      ))}
                    </AccordionContent>
                  </AccordionItem>
                ))}
              </Accordion>
            ) : (
              <div className="px-2 py-3 text-xs text-muted-foreground">No employees.</div>
            )}
          </section>

          {(
            [
              { tier: "system" as const, agents: systemAgents },
              { tier: "workflow" as const, agents: workflowAgents },
            ] satisfies { tier: AgentTier; agents: AvailableAgent[] }[]
          ).map((section) => (
            <section key={section.tier} className="mb-3">
              <div className="mb-1 flex items-center justify-between px-2 text-xs font-medium text-muted-foreground">
                <span>{tierLabel(section.tier)}</span>
                <span>{section.agents.length}</span>
              </div>
              <div className="space-y-1">
                {section.agents.map((agent) => (
                  <RosterButton
                    key={agent.id}
                    agent={agent}
                    selected={selectedAgentId === agent.id}
                    onSelect={() => setSelectedAgentId(agent.id)}
                  />
                ))}
                {section.agents.length === 0 && (
                  <div className="px-2 py-3 text-xs text-muted-foreground">No agents.</div>
                )}
              </div>
            </section>
          ))}
        </div>
      </div>
    </aside>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="rounded-md border border-border bg-muted/20 px-2 py-1 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{value}</span> {label}
    </span>
  );
}

function DetailHeader({
  agent,
  tier,
  editable,
  refetch,
}: {
  agent: AvailableAgent;
  tier: AgentTier;
  editable: boolean;
  refetch: () => void;
}) {
  return (
    <header className="shrink-0 border-b border-border px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-xl font-semibold">{agentDisplayName(agent)}</h2>
            <Badge variant={editable ? "secondary" : "outline"}>
              {editable ? "Editable" : "Read-only"}
            </Badge>
            <Badge variant="outline">{tierLabel(tier)}</Badge>
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            {agent.description || agent.title || agent.name}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Metric label="id" value={agent.id} />
            <Metric label="harness" value={agent.harness || "unknown"} />
            <Metric label="department" value={agent.department || "Unassigned"} />
          </div>
        </div>
        <Button variant="ghost" size="icon" onClick={refetch} aria-label="Refresh agent">
          <RefreshCwIcon />
        </Button>
      </div>
    </header>
  );
}

function OverviewTab({
  agent,
  tier,
  imageVersion,
  sotTier,
  imageLoaded,
}: {
  agent: AvailableAgent;
  tier: AgentTier;
  imageVersion: number | null;
  sotTier: string | null;
  imageLoaded: boolean;
}) {
  return (
    <div className="grid gap-4 p-4 xl:grid-cols-2">
      <section className="rounded-md border border-border bg-background p-4">
        <h3 className="mb-3 text-sm font-medium">Identity</h3>
        <dl className="grid gap-2 text-sm">
          <InfoRow label="Name" value={agent.name} />
          <InfoRow label="Display" value={agentDisplayName(agent)} />
          <InfoRow label="Category" value={tier} />
          <InfoRow label="Department" value={agent.department || "Unassigned"} />
          <InfoRow label="Title" value={agent.title || "None"} />
        </dl>
      </section>
      <section className="rounded-md border border-border bg-background p-4">
        <h3 className="mb-3 text-sm font-medium">Image</h3>
        <dl className="grid gap-2 text-sm">
          <InfoRow label="Editable image" value={imageLoaded ? "Available" : "Unavailable"} />
          <InfoRow label="Version" value={imageVersion === null ? "Unknown" : imageVersion} />
          <InfoRow label="Source tier" value={sotTier || "default"} />
          <InfoRow label="Harness" value={agent.harness || "unknown"} />
          <InfoRow label="Bundled skills" value={agent.skills.length} />
        </dl>
      </section>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_minmax(0,1fr)] gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-foreground">{value}</dd>
    </div>
  );
}

function ConfigTab({
  editable,
  configText,
  setConfigText,
  instructionsText,
  setInstructionsText,
  onSave,
  busy,
  error,
  notice,
}: {
  editable: boolean;
  configText: string;
  setConfigText: (value: string) => void;
  instructionsText: string;
  setInstructionsText: (value: string) => void;
  onSave: () => void;
  busy: boolean;
  error: string | null;
  notice: string | null;
}) {
  return (
    <div className="grid min-h-0 gap-4 p-4 xl:grid-cols-2">
      <section className="flex min-h-[34rem] flex-col rounded-md border border-border bg-background">
        <div className="border-b border-border px-3 py-2 text-sm font-medium">Instructions</div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={instructionsText}
          onChange={(event) => setInstructionsText(event.target.value)}
          disabled={!editable}
          aria-label="Agent instructions"
        />
      </section>
      <section className="flex min-h-[34rem] flex-col rounded-md border border-border bg-background">
        <div className="border-b border-border px-3 py-2 text-sm font-medium">Config JSON</div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={configText}
          onChange={(event) => setConfigText(event.target.value)}
          disabled={!editable}
          aria-label="Agent config"
        />
      </section>
      <div className="xl:col-span-2 flex flex-wrap items-center justify-between gap-2">
        <div className="min-h-5 text-sm">
          {error && <span className="text-destructive">{error}</span>}
          {!error && notice && <span className="text-muted-foreground">{notice}</span>}
        </div>
        <Button onClick={onSave} disabled={!editable || busy}>
          <SaveIcon /> Save image
        </Button>
      </div>
    </div>
  );
}

function SkillsTab({ agentId, editable }: { agentId: string; editable: boolean }) {
  const installed = useInstalledSkills(agentId);
  const search = useSearchSkills();
  const createPreview = useCreateSkillPreview();
  const applyPreview = useApplySkillPreview();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillSearchResult[]>([]);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function stageInstall(hit: SkillSearchResult) {
    if (!hit.source_ref) return;
    setError(null);
    try {
      setPreview(
        await createPreview.mutateAsync({
          target_agent_ids: [agentId],
          source: hit.source,
          source_ref: hit.source_ref,
          install_mode: "skip_existing",
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    }
  }

  async function stageRemove(skillName: string) {
    setError(null);
    try {
      setPreview(
        await createPreview.mutateAsync({
          operation: "remove",
          target_agent_ids: [agentId],
          skill_names: [skillName],
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    }
  }

  async function applyStaged() {
    if (!preview) return;
    setError(null);
    try {
      await applyPreview.mutateAsync({ previewId: preview.id, targetAgentIds: [agentId] });
      setPreview(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Apply failed");
    }
  }

  return (
    <div className="grid gap-4 p-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="rounded-md border border-border bg-background">
        <div className="border-b border-border px-3 py-2 text-sm font-medium">Catalog</div>
        <div className="space-y-3 p-3">
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
                .catch((err: unknown) =>
                  setError(err instanceof Error ? err.message : "Search failed"),
                );
            }}
          >
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search skills"
              aria-label="Search skills"
            />
            <Button type="submit" disabled={!editable || !query.trim() || search.isPending}>
              <SearchIcon /> Search
            </Button>
          </form>
          <div className="grid gap-2 md:grid-cols-2">
            {results.map((hit) => (
              <button
                key={`${hit.source}:${hit.source_ref ?? hit.name}`}
                type="button"
                className="rounded-md border border-border px-3 py-2 text-left hover:bg-muted/40 disabled:opacity-50"
                disabled={!editable || !hit.source_ref || createPreview.isPending}
                onClick={() => void stageInstall(hit)}
              >
                <div className="truncate text-sm font-medium">{hit.name}</div>
                <div className="line-clamp-2 text-xs text-muted-foreground">
                  {hit.description || hit.source_ref}
                </div>
              </button>
            ))}
          </div>
          {error && <div className="text-sm text-destructive">{error}</div>}
          {preview && (
            <div className="rounded-md border border-border bg-muted/20 p-3">
              <div className="text-sm font-medium">Preview ready</div>
              <div className="mt-1 text-xs text-muted-foreground">
                {preview.operation} {preview.skill_names.join(", ")}
              </div>
              <Button
                className="mt-3"
                size="sm"
                disabled={!editable || applyPreview.isPending}
                onClick={() => void applyStaged()}
              >
                Apply preview
              </Button>
            </div>
          )}
        </div>
      </section>

      <section className="rounded-md border border-border bg-background">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div className="text-sm font-medium">Installed</div>
          <Badge variant="secondary">{installed.data?.length ?? 0}</Badge>
        </div>
        <div className="max-h-[42rem] divide-y divide-border overflow-y-auto">
          {(installed.data ?? []).map((skill) => (
            <div key={skill.name} className="p-3">
              <div className="truncate text-sm font-medium">{skill.name}</div>
              <div className="line-clamp-2 text-xs text-muted-foreground">{skill.description}</div>
              <Button
                className="mt-2"
                size="xs"
                variant="outline"
                disabled={!editable || createPreview.isPending}
                onClick={() => void stageRemove(skill.name)}
              >
                Remove
              </Button>
            </div>
          ))}
          {!installed.isLoading && (installed.data ?? []).length === 0 && (
            <div className="p-4 text-sm text-muted-foreground">No installed skills.</div>
          )}
        </div>
      </section>
    </div>
  );
}

function toolsForConnection(provider: ConnectorManifest, connection: ConnectorConnection) {
  const serviceDefs = new Map(provider.services.map((service) => [service.key, service]));
  return connection.services.flatMap((state) => {
    if (!state.enabled) return [];
    const service = serviceDefs.get(state.serviceKey);
    if (!service) return [];
    return service.tools.map<AvailableConnectorTool>((tool) => ({
      ...tool,
      providerName: provider.name,
      serviceKey: state.serviceKey,
      serviceName: service.name,
      token: `${state.serviceKey}:${tool.key}`,
    }));
  });
}

function ConnectorsTab({ agentId, editable }: { agentId: string; editable: boolean }) {
  const catalog = useConnectorsCatalog();
  const grants = useConnectorAgentGrants(agentId);
  const grantMutation = useGrantConnectorToAgent();
  const [selectedByConnection, setSelectedByConnection] = useState<Record<string, string[]>>({});

  const rows = useMemo(
    () =>
      (catalog.data ?? []).flatMap((provider) =>
        provider.connections.map((connection) => ({
          provider,
          connection,
          tools: toolsForConnection(provider, connection),
        })),
      ),
    [catalog.data],
  );

  useEffect(() => {
    const next: Record<string, string[]> = {};
    for (const row of rows) {
      next[row.connection.id] = (grants.data ?? [])
        .filter((grant) => grant.connectionId === row.connection.id && grant.enabled)
        .map((grant) => `${grant.serviceKey}:${grant.toolKey}`);
    }
    setSelectedByConnection(next);
  }, [agentId, grants.data, rows]);

  function setTool(connectionId: string, token: string, checked: boolean) {
    setSelectedByConnection((prev) => {
      const current = new Set(prev[connectionId] ?? []);
      if (checked) current.add(token);
      else current.delete(token);
      return { ...prev, [connectionId]: Array.from(current) };
    });
  }

  return (
    <div className="space-y-4 p-4">
      {rows.map(({ provider, connection, tools }) => {
        const selected = selectedByConnection[connection.id] ?? [];
        return (
          <section key={connection.id} className="rounded-md border border-border bg-background">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
              <div>
                <div className="text-sm font-medium">{connection.displayName}</div>
                <div className="text-xs text-muted-foreground">
                  {provider.name} · {connection.status} · {selected.length}/{tools.length} actions
                </div>
              </div>
              <Button
                size="sm"
                disabled={!editable || selected.length === 0 || grantMutation.isPending}
                onClick={() =>
                  grantMutation.mutate({
                    connectionId: connection.id,
                    agentId,
                    tools: selected,
                  })
                }
              >
                <PlugIcon /> Save actions
              </Button>
            </div>
            <div className="grid gap-2 p-3 md:grid-cols-2 xl:grid-cols-3">
              {tools.map((tool) => (
                <label
                  key={tool.token}
                  className="flex items-start gap-2 rounded-md border border-border/70 px-3 py-2 text-sm"
                >
                  <input
                    type="checkbox"
                    className="mt-1 size-3.5"
                    checked={selected.includes(tool.token)}
                    disabled={!editable}
                    onChange={(event) => setTool(connection.id, tool.token, event.target.checked)}
                    aria-label={`${connection.displayName} ${tool.name}`}
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{tool.name}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {tool.serviceName} · {tool.mcpTool}
                    </span>
                  </span>
                </label>
              ))}
              {tools.length === 0 && (
                <div className="text-sm text-muted-foreground">No enabled actions.</div>
              )}
            </div>
          </section>
        );
      })}
      {!catalog.isLoading && rows.length === 0 && (
        <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
          No connector connections are configured.
        </div>
      )}
      {grantMutation.isError && (
        <div className="text-sm text-destructive">
          {grantMutation.error instanceof Error ? grantMutation.error.message : "Grant failed"}
        </div>
      )}
    </div>
  );
}

function FilesTab({
  agentId,
  editable,
  etag,
  commitSave,
  busy,
}: {
  agentId: string;
  editable: boolean;
  etag: string | null | undefined;
  commitSave: (body: AgentImageUpdate, label: string) => void;
  busy: boolean;
}) {
  const [directory, setDirectory] = useState("");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const tree = useAgentImageTree(agentId, directory, editable);
  const readFile = useReadAgentImageFile();

  useEffect(() => {
    setDirectory("");
    setSelectedPath(null);
    setContent("");
  }, [agentId]);

  async function openFile(path: string) {
    const file = await readFile.mutateAsync({ agentId, path });
    setSelectedPath(file.path);
    setContent(file.content);
  }

  return (
    <div className="grid min-h-0 gap-4 p-4 xl:grid-cols-[20rem_minmax(0,1fr)]">
      <section className="min-h-[34rem] rounded-md border border-border bg-background">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <div className="truncate text-sm font-medium">{tree.data?.path || "."}</div>
          <Button
            variant="ghost"
            size="xs"
            disabled={!directory}
            onClick={() => setDirectory(parentPath(directory))}
          >
            Up
          </Button>
        </div>
        <div className="max-h-[42rem] overflow-y-auto p-2">
          {(tree.data?.entries ?? []).map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="mb-1 flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted/50"
              disabled={!editable}
              onClick={() =>
                entry.type === "directory" ? setDirectory(entry.path) : void openFile(entry.path)
              }
            >
              {entry.type === "directory" ? (
                <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
              ) : (
                <FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              {entry.type === "file" && (
                <span className="text-xs text-muted-foreground">{entry.size}</span>
              )}
            </button>
          ))}
          {tree.isError && (
            <div className="p-2 text-sm text-destructive">
              {tree.error instanceof Error ? tree.error.message : "File tree unavailable"}
            </div>
          )}
        </div>
      </section>
      <section className="flex min-h-[34rem] flex-col rounded-md border border-border bg-background">
        <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
          <div className="min-w-0 truncate text-sm font-medium">
            {selectedPath || "Select file"}
          </div>
          <div className="flex gap-2">
            <Button
              size="xs"
              variant="outline"
              disabled={!editable || !selectedPath || selectedPath === "config.yaml" || busy}
              onClick={() =>
                selectedPath && commitSave({ remove: [selectedPath] }, `Remove ${selectedPath}`)
              }
            >
              Remove
            </Button>
            <Button
              size="xs"
              disabled={!editable || !selectedPath || busy}
              onClick={() =>
                selectedPath &&
                commitSave({ files: { [selectedPath]: content } }, `Save ${selectedPath}`)
              }
            >
              <SaveIcon /> Save file
            </Button>
          </div>
        </div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          disabled={!editable || !selectedPath}
          aria-label="Agent image file"
        />
        {etag === null && (
          <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground">
            Save uses the latest loaded agent image version.
          </div>
        )}
      </section>
    </div>
  );
}

export function WorkForcePage() {
  const allowed = useWorkForceAdminAccess();
  const agentsQuery = useAvailableAgents({ includeSessionAgents: false });
  const updateImage = useUpdateAgentImage();
  const [query, setQuery] = useState("");
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [tab, setTab] = useState<WorkForceTab>("overview");
  const [configText, setConfigText] = useState("");
  const [instructionsText, setInstructionsText] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const [pendingSave, setPendingSave] = useState<PendingSave | null>(null);

  const filteredAgents = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = workForceRosterAgents(agentsQuery.data ?? []);
    if (!needle) return rows;
    return rows.filter((agent) =>
      [agent.name, agentDisplayName(agent), agent.department, agent.title]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [agentsQuery.data, query]);

  useEffect(() => {
    if (filteredAgents.length === 0) {
      setSelectedAgentId(null);
      return;
    }
    if (!selectedAgentId || !filteredAgents.some((agent) => agent.id === selectedAgentId)) {
      const grouped = groupAgentsByTier(filteredAgents);
      const employeeGroups = groupEmployeesByDepartment(filteredAgents);
      setSelectedAgentId(
        employeeGroups[0]?.agents[0]?.id ??
          [...grouped.system].sort(compareAgentsByName)[0]?.id ??
          [...grouped.workflow].sort(compareAgentsByName)[0]?.id ??
          null,
      );
    }
  }, [filteredAgents, selectedAgentId]);

  const selectedAgent = filteredAgents.find((agent) => agent.id === selectedAgentId) ?? null;
  const selectedTier = selectedAgent ? tierForAgent(selectedAgent) : "employee";
  const imageEnabled = Boolean(selectedAgent && selectedTier !== "workflow");
  const image = useAgentImage(selectedAgent?.id, imageEnabled);
  const imageSnapshot = image.data;
  const editable = Boolean(selectedAgent && selectedTier !== "workflow" && imageSnapshot);

  useEffect(() => {
    if (!imageSnapshot) {
      setConfigText("");
      setInstructionsText("");
      return;
    }
    setConfigText(JSON.stringify(imageSnapshot.image.config, null, 2));
    setInstructionsText(imageSnapshot.image.instructions ?? "");
    setSaveError(null);
    setSaveNotice(null);
  }, [imageSnapshot]);

  async function doSave(body: AgentImageUpdate, label: string) {
    if (!selectedAgent) return;
    setSaveError(null);
    setSaveNotice(null);
    try {
      await updateImage.mutateAsync({
        agentId: selectedAgent.id,
        body,
        etag: imageSnapshot?.etag,
      });
      setSaveNotice(`${label} saved.`);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Save failed");
    }
  }

  function commitSave(body: AgentImageUpdate, label: string) {
    if (selectedTier === "system") {
      setPendingSave({ body, label });
      return;
    }
    void doSave(body, label);
  }

  function saveConfig() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(configText) as Record<string, unknown>;
    } catch {
      setSaveError("Config must be valid JSON.");
      return;
    }
    commitSave({ config: parsed, instructions: instructionsText }, "Agent image");
  }

  return (
    <AccessGate allowed={allowed}>
      <WorkForceShell>
        <RosterPanel
          agents={filteredAgents}
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          query={query}
          setQuery={setQuery}
        />
        <main className="flex min-h-0 flex-col overflow-hidden bg-muted/10">
          {selectedAgent ? (
            <>
              <DetailHeader
                agent={selectedAgent}
                tier={selectedTier}
                editable={editable}
                refetch={() => {
                  void agentsQuery.refetch();
                  void image.refetch();
                }}
              />
              {selectedTier !== "workflow" && image.isError && (
                <div className="border-b border-border bg-destructive/10 px-5 py-2 text-sm text-destructive">
                  {image.error instanceof Error ? image.error.message : "Agent image unavailable"}
                </div>
              )}
              <Tabs
                value={tab}
                onValueChange={(value) => setTab(value as WorkForceTab)}
                className="min-h-0 flex-1 overflow-hidden"
              >
                <TabsList variant="line" className="mx-5 mt-3">
                  <TabsTrigger value="overview">
                    <BotIcon /> Overview
                  </TabsTrigger>
                  <TabsTrigger value="config" disabled={!editable}>
                    <SlidersHorizontalIcon /> Config
                  </TabsTrigger>
                  <TabsTrigger value="skills" disabled={!editable}>
                    <PuzzleIcon /> Skills
                  </TabsTrigger>
                  <TabsTrigger value="connectors" disabled={!editable}>
                    <PlugIcon /> Connectors
                  </TabsTrigger>
                  <TabsTrigger value="files" disabled={!editable}>
                    <FileTextIcon /> Files
                  </TabsTrigger>
                </TabsList>
                <div className="min-h-0 flex-1 overflow-y-auto">
                  <TabsContent value="overview">
                    <OverviewTab
                      agent={selectedAgent}
                      tier={selectedTier}
                      imageVersion={imageSnapshot?.image.version ?? null}
                      sotTier={imageSnapshot?.image.sot_tier ?? null}
                      imageLoaded={Boolean(imageSnapshot)}
                    />
                  </TabsContent>
                  <TabsContent value="config">
                    <ConfigTab
                      editable={editable}
                      configText={configText}
                      setConfigText={setConfigText}
                      instructionsText={instructionsText}
                      setInstructionsText={setInstructionsText}
                      onSave={saveConfig}
                      busy={updateImage.isPending}
                      error={saveError}
                      notice={saveNotice}
                    />
                  </TabsContent>
                  <TabsContent value="skills">
                    {selectedAgentId && <SkillsTab agentId={selectedAgentId} editable={editable} />}
                  </TabsContent>
                  <TabsContent value="connectors">
                    {selectedAgentId && (
                      <ConnectorsTab agentId={selectedAgentId} editable={editable} />
                    )}
                  </TabsContent>
                  <TabsContent value="files">
                    {selectedAgentId && (
                      <FilesTab
                        agentId={selectedAgentId}
                        editable={editable}
                        etag={imageSnapshot?.etag}
                        commitSave={commitSave}
                        busy={updateImage.isPending}
                      />
                    )}
                  </TabsContent>
                </div>
              </Tabs>
            </>
          ) : (
            <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
              No agents match the current filter.
            </div>
          )}
        </main>
      </WorkForceShell>
      <Dialog open={pendingSave !== null} onOpenChange={(open) => !open && setPendingSave(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm system agent edit</DialogTitle>
            <DialogDescription>
              This change updates a system agent image and becomes live for new sessions.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingSave(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={updateImage.isPending || pendingSave === null}
              onClick={() => {
                if (!pendingSave) return;
                const next = pendingSave;
                setPendingSave(null);
                void doSave(next.body, next.label);
              }}
            >
              Save system agent
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AccessGate>
  );
}
