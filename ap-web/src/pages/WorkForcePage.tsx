import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  BotIcon,
  ChevronRightIcon,
  FileTextIcon,
  FolderIcon,
  PlugIcon,
  PuzzleIcon,
  RefreshCwIcon,
  SaveIcon,
  SearchIcon,
  ShieldAlertIcon,
  SlidersHorizontalIcon,
  TerminalIcon,
  UsersIcon,
  WorkflowIcon,
} from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
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
import {
  useUpdateWorkforceAgentInstructions,
  useUpdateWorkforceInstructions,
  useUpsertWorkforceAgentOverride,
  useUpsertWorkforceConnector,
  useUpsertWorkforceSkill,
  useUpsertWorkforceTool,
  useWorkforceAgentEffective,
  useWorkforceScope,
  useWorkforceScopes,
  useWorkforceToolCatalog,
} from "@/hooks/useWorkforce";
import { getMe } from "@/lib/accountsApi";
import { groupAgentsByTier, tierForAgent, type AgentTier } from "@/lib/agentTiers";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { AgentImageUpdate } from "@/lib/agentImagesApi";
import type { ConnectorConnection, ConnectorManifest, ConnectorTool } from "@/lib/connectorsApi";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type {
  WorkforceEffectiveConnector,
  WorkforceEffectiveSkill,
  WorkforceEffectiveTool,
  WorkforceScopeKind,
  WorkforceToolCatalogItem,
} from "@/lib/workforceApi";

type WorkForceTab = "overview" | "config" | "permissions" | "skills" | "connectors" | "files";

interface PendingSave {
  body: AgentImageUpdate;
  label: string;
  tier: AgentTier;
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

function workforceScopeSlug(value: string | null | undefined): string | null {
  const cleaned = value
    ?.trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || null;
}

function isWorkForceEmployee(agent: AvailableAgent): boolean {
  return tierForAgent(agent) === "employee" && Boolean(agent.department?.trim());
}

function workForceRosterAgents(agents: readonly AvailableAgent[]): AvailableAgent[] {
  return agents.filter((agent) => {
    const tier = tierForAgent(agent);
    return (
      tier === "system" || tier === "harness" || tier === "workflow" || isWorkForceEmployee(agent)
    );
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
  if (tier === "harness") return "Harnesses";
  if (tier === "workflow") return "Workflows";
  return "Employees";
}

function iconForTier(tier: AgentTier) {
  if (tier === "system") return <ShieldAlertIcon className="size-4" />;
  if (tier === "harness") return <TerminalIcon className="size-4" />;
  if (tier === "workflow") return <WorkflowIcon className="size-4" />;
  return <BotIcon className="size-4" />;
}

// Tier accent — fast visual scanning across the roster and detail hero.
// system=purple (AI/governance), harness=cyan (tooling), workflow=amber
// (automation), employee=blue (the default interactive accent).
function tierAccentTextClass(tier: AgentTier): string {
  if (tier === "system") return "text-accent-purple";
  if (tier === "harness") return "text-accent-cyan";
  if (tier === "workflow") return "text-accent-amber";
  return "text-accent-blue";
}

function tierAccentRingClass(tier: AgentTier): string {
  if (tier === "system") return "ring-accent-purple/40";
  if (tier === "harness") return "ring-accent-cyan/40";
  if (tier === "workflow") return "ring-accent-amber/40";
  return "ring-accent-blue/40";
}

function tierAccentBorderClass(tier: AgentTier): string {
  if (tier === "system") return "border-accent-purple/40";
  if (tier === "harness") return "border-accent-cyan/40";
  if (tier === "workflow") return "border-accent-amber/40";
  return "border-accent-blue/40";
}

function tierInitials(agent: AvailableAgent): string {
  const name = agentDisplayName(agent).trim();
  if (!name) return "?";
  const parts = name.split(/\s+/).filter(Boolean);
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return `${parts[0]![0]}${parts[parts.length - 1]![0]}`.toUpperCase();
}

function tierRequiresEditConfirmation(tier: AgentTier): boolean {
  return tier === "system" || tier === "harness";
}

function editConfirmationTitle(tier: AgentTier | undefined): string {
  return tier === "harness" ? "Confirm harness edit" : "Confirm system agent edit";
}

function editConfirmationDescription(tier: AgentTier | undefined): string {
  const label = tier === "harness" ? "harness" : "system agent";
  return `This change updates a ${label} image and becomes live for new sessions.`;
}

function editConfirmationButtonLabel(tier: AgentTier | undefined): string {
  return tier === "harness" ? "Save harness" : "Save system agent";
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
        <div className="mx-auto mt-14 w-full max-w-5xl px-6">
          <div className="mc-fade-up mc-surface flex items-start gap-3 p-5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-md border border-accent-red/40 bg-accent-red/10 text-accent-red">
              <ShieldAlertIcon className="size-4" />
            </span>
            <div>
              <h1 className="text-2xl font-semibold">Work Force</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                You don't have permission to manage agents.
              </p>
            </div>
          </div>
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
        "group/roster flex min-h-14 w-full items-center gap-2.5 rounded-md border px-2.5 py-2 text-left transition-all duration-150",
        selected
          ? cn("bg-muted text-foreground", tierAccentBorderClass(tier))
          : "border-transparent text-muted-foreground hover:translate-x-0.5 hover:bg-muted/50 hover:text-foreground",
      )}
    >
      <Avatar
        size="sm"
        className={cn(
          "ring-2 transition-all",
          selected ? tierAccentRingClass(tier) : "ring-transparent group-hover/roster:ring-border",
        )}
      >
        <AvatarFallback className={cn("bg-background", tierAccentTextClass(tier))}>
          {tier === "employee" ? tierInitials(agent) : iconForTier(tier)}
        </AvatarFallback>
      </Avatar>
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

// Department/tier accordion open-state survives reloads — sections start
// collapsed (empty set) the very first time, then remember whatever the
// admin last expanded.
const ROSTER_SECTIONS_STORAGE_KEY = "workforce-roster-open-sections";

function loadOpenRosterSections(): string[] {
  try {
    const raw = localStorage.getItem(ROSTER_SECTIONS_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

function saveOpenRosterSections(sections: string[]) {
  try {
    localStorage.setItem(ROSTER_SECTIONS_STORAGE_KEY, JSON.stringify(sections));
  } catch {
    // quota / private-mode — state still updates in memory, just won't persist
  }
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
  const harnessAgents = useMemo(
    () => [...groups.harness].sort(compareAgentsByName),
    [groups.harness],
  );
  const workflowAgents = useMemo(
    () => [...groups.workflow].sort(compareAgentsByName),
    [groups.workflow],
  );

  const [openSections, setOpenSections] = useState<string[]>(() => loadOpenRosterSections());
  function handleOpenSectionsChange(next: string[]) {
    setOpenSections(next);
    saveOpenRosterSections(next);
  }

  const employeesRef = useRef<HTMLElement>(null);
  const systemRef = useRef<HTMLElement>(null);
  const harnessRef = useRef<HTMLElement>(null);
  const workflowRef = useRef<HTMLElement>(null);
  const jumpTargets: Record<AgentTier, RefObject<HTMLElement | null>> = {
    employee: employeesRef,
    system: systemRef,
    harness: harnessRef,
    workflow: workflowRef,
  };
  function jumpTo(tier: AgentTier) {
    jumpTargets[tier].current?.scrollIntoView({ behavior: "smooth", block: "start" });
    const sectionId = `tier:${tier}`;
    if (tier !== "employee" && !openSections.includes(sectionId)) {
      handleOpenSectionsChange([...openSections, sectionId]);
    }
  }

  return (
    <aside
      aria-label="Agent roster"
      className="min-h-0 border-b border-border bg-background lg:border-r lg:border-b-0"
    >
      <div className="flex h-full min-h-0 flex-col">
        <header className="mc-surface m-2 mb-0 shrink-0 rounded-b-none border-b-0 px-4 py-4">
          <div className="flex items-center gap-2.5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-md border border-accent-blue/40 bg-accent-blue/10 text-accent-blue shadow-[var(--shadow-glow-blue)]">
              <UsersIcon className="size-4" />
            </span>
            <div className="min-w-0">
              <h1 className="truncate text-base font-semibold">Work Force</h1>
              <p className="mc-label truncate text-accent-blue/70">Agent directory control</p>
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
          <div className="mt-3 flex flex-wrap gap-1.5">
            {[
              {
                tier: "employee" as const,
                count: employeeCount,
                accent: "text-accent-blue",
                icon: <UsersIcon className="size-3.5" />,
              },
              {
                tier: "system" as const,
                count: systemAgents.length,
                accent: "text-accent-purple",
                icon: <ShieldAlertIcon className="size-3.5" />,
              },
              {
                tier: "harness" as const,
                count: harnessAgents.length,
                accent: "text-accent-cyan",
                icon: <TerminalIcon className="size-3.5" />,
              },
              {
                tier: "workflow" as const,
                count: workflowAgents.length,
                accent: "text-accent-amber",
                icon: <WorkflowIcon className="size-3.5" />,
              },
            ].map((chip) => (
              <button
                key={chip.tier}
                type="button"
                onClick={() => jumpTo(chip.tier)}
                aria-label={`${tierLabel(chip.tier)} — ${chip.count}`}
                title={tierLabel(chip.tier)}
                className="flex items-center gap-1 rounded-full border border-border-dimmer bg-bg-subtle px-2 py-1 transition-colors hover:border-border-stronger hover:bg-muted/50"
              >
                <span className={chip.accent}>{chip.icon}</span>
                <span className="mc-value text-2xs">{chip.count}</span>
              </button>
            ))}
          </div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          <Accordion
            type="multiple"
            value={openSections}
            onValueChange={handleOpenSectionsChange}
            className="gap-1"
          >
            <section ref={employeesRef} className="mb-3 scroll-mt-2">
              <div className="mc-label mb-1 flex items-center justify-between px-2">
                <span>Employees</span>
                <span className="mc-value">{employeeCount}</span>
              </div>
              {departmentGroups.length > 0 ? (
                departmentGroups.map((group) => (
                  <AccordionItem
                    key={group.department}
                    value={`department:${group.department}`}
                    className="border-0"
                  >
                    <AccordionTrigger
                      aria-label={`Department ${group.department}`}
                      className="rounded-md px-2 py-2 text-xs text-muted-foreground hover:bg-muted/40 hover:no-underline"
                    >
                      <span className="flex flex-1 items-center justify-between pr-2">
                        <span>{group.department}</span>
                        <Badge variant="secondary">{group.agents.length}</Badge>
                      </span>
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
                ))
              ) : (
                <div className="px-2 py-3 text-xs text-muted-foreground">No employees.</div>
              )}
            </section>

            {(
              [
                { tier: "system" as const, agents: systemAgents, ref: systemRef },
                { tier: "harness" as const, agents: harnessAgents, ref: harnessRef },
                { tier: "workflow" as const, agents: workflowAgents, ref: workflowRef },
              ] satisfies { tier: AgentTier; agents: AvailableAgent[]; ref: typeof systemRef }[]
            ).map((section) => (
              <section key={section.tier} ref={section.ref} className="mb-3 scroll-mt-2">
                <AccordionItem value={`tier:${section.tier}`} className="border-0">
                  <AccordionTrigger
                    aria-label={tierLabel(section.tier)}
                    className="rounded-md px-2 py-2 text-xs text-muted-foreground hover:bg-muted/40 hover:no-underline"
                  >
                    <span className="flex flex-1 items-center justify-between pr-2">
                      <span className={cn("mc-label", tierAccentTextClass(section.tier))}>
                        {tierLabel(section.tier)}
                      </span>
                      <span className="mc-value">{section.agents.length}</span>
                    </span>
                  </AccordionTrigger>
                  <AccordionContent className="space-y-1 pb-1">
                    {section.agents.length > 0 ? (
                      section.agents.map((agent) => (
                        <RosterButton
                          key={agent.id}
                          agent={agent}
                          selected={selectedAgentId === agent.id}
                          onSelect={() => setSelectedAgentId(agent.id)}
                        />
                      ))
                    ) : (
                      <div className="px-2 py-3 text-xs text-muted-foreground">No agents.</div>
                    )}
                  </AccordionContent>
                </AccordionItem>
              </section>
            ))}
          </Accordion>
        </div>
      </div>
    </aside>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <span className="mc-surface flex items-center gap-1.5 px-2 py-1">
      <span className="mc-value text-xs">{value}</span>
      <span className="mc-label text-2xs text-muted-foreground">{label}</span>
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
    <header className="mc-fade-up shrink-0 border-b border-border-dimmer bg-bg-subtle px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <Avatar size="lg" className={cn("ring-2 shrink-0", tierAccentRingClass(tier))}>
            <AvatarFallback className={cn("bg-background", tierAccentTextClass(tier))}>
              {tier === "employee" ? tierInitials(agent) : iconForTier(tier)}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-xl font-semibold">{agentDisplayName(agent)}</h2>
              <Badge variant={editable ? "secondary" : "outline"} className="gap-1.5">
                {editable && (
                  <span
                    className="size-1.5 rounded-full bg-accent-green mc-live-dot"
                    aria-hidden="true"
                  />
                )}
                {editable ? "Editable" : "Read-only"}
              </Badge>
              <Badge variant="outline" className={tierAccentTextClass(tier)}>
                {tierLabel(tier)}
              </Badge>
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
    <div className="mc-fade-up grid gap-4 p-4 xl:grid-cols-2">
      <section className="mc-surface p-4">
        <h3 className="mc-label mb-3">Identity</h3>
        <dl className="grid gap-2 text-sm">
          <InfoRow label="Name" value={agent.name} />
          <InfoRow label="Display" value={agentDisplayName(agent)} />
          <InfoRow label="Category" value={tier} />
          <InfoRow label="Department" value={agent.department || "Unassigned"} />
          <InfoRow label="Title" value={agent.title || "None"} />
        </dl>
      </section>
      <section className="mc-surface p-4">
        <h3 className="mc-label mb-3">Image</h3>
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
  dirty,
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
  dirty: boolean;
}) {
  return (
    <div className="mc-fade-up grid min-h-0 gap-4 p-4 xl:grid-cols-2">
      <section className="mc-surface flex min-h-[34rem] flex-col">
        <div className="mc-label border-b border-border-dimmer px-3 py-2">Instructions</div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={instructionsText}
          onChange={(event) => setInstructionsText(event.target.value)}
          disabled={!editable}
          aria-label="Agent instructions"
        />
      </section>
      <section className="mc-surface flex min-h-[34rem] flex-col">
        <div className="mc-label border-b border-border-dimmer px-3 py-2">Config JSON</div>
        <Textarea
          className="min-h-0 flex-1 resize-none rounded-none border-0 font-mono text-xs focus-visible:ring-0"
          value={configText}
          onChange={(event) => setConfigText(event.target.value)}
          disabled={!editable}
          aria-label="Agent config"
        />
      </section>
      <div className="xl:col-span-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-h-5 items-center gap-2 text-sm">
          {error && <span className="text-destructive">{error}</span>}
          {!error && notice && <span className="text-muted-foreground">{notice}</span>}
          {!error && !notice && dirty && editable && (
            <Badge variant="outline" className="text-accent-amber">
              Unsaved changes
            </Badge>
          )}
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
    <div className="mc-fade-up grid gap-4 p-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="mc-surface">
        <div className="mc-label border-b border-border-dimmer px-3 py-2">Catalog</div>
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
            <div className="mc-surface bg-bg-elevated p-3">
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

      <section className="mc-surface">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div className="mc-label">Installed</div>
          <Badge variant="secondary">{installed.data?.length ?? 0}</Badge>
        </div>
        <div className="max-h-[42rem] divide-y divide-border-dimmer overflow-y-auto">
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
    <div className="mc-fade-up space-y-4 p-4">
      {rows.map(({ provider, connection, tools }) => {
        const selected = selectedByConnection[connection.id] ?? [];
        return (
          <section key={connection.id} className="mc-surface">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
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
        <div className="mc-surface p-4 text-sm text-muted-foreground">
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

function scopeDisplayName(scopeKind: WorkforceScopeKind, department: string | null): string {
  return scopeKind === "organization" ? "Organization" : department || "Department";
}

function inheritedSourceLabel(item: WorkforceEffectiveConnector | WorkforceEffectiveSkill): string {
  return item.inheritedFrom
    .map((source) => (source.scopeKind === "organization" ? "Organization" : "Department"))
    .join(", ");
}

function sameConnectionSelection(
  a: Record<string, string[]>,
  b: Record<string, string[]>,
): boolean {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;
  return aKeys.every((key) => {
    const aValues = a[key] ?? [];
    const bValues = b[key] ?? [];
    return (
      aValues.length === bValues.length && aValues.every((value, index) => value === bValues[index])
    );
  });
}

function PermissionsTab({ agent, editable }: { agent: AvailableAgent; editable: boolean }) {
  const department = agent.department?.trim() || null;
  const departmentScopeId = workforceScopeSlug(department);
  const [scopeKind, setScopeKind] = useState<WorkforceScopeKind>(
    departmentScopeId ? "department" : "organization",
  );
  const scopeId = scopeKind === "department" ? departmentScopeId : null;
  const scopes = useWorkforceScopes();
  const scope = useWorkforceScope(scopeKind, scopeId, editable);
  const effective = useWorkforceAgentEffective(agent.id, editable);
  const connectorCatalog = useConnectorsCatalog();
  const toolCatalog = useWorkforceToolCatalog();
  const updateInstructions = useUpdateWorkforceInstructions();
  const updateAgentInstructions = useUpdateWorkforceAgentInstructions();
  const upsertConnector = useUpsertWorkforceConnector();
  const upsertSkill = useUpsertWorkforceSkill();
  const upsertTool = useUpsertWorkforceTool();
  const upsertOverride = useUpsertWorkforceAgentOverride();
  const skillSearch = useSearchSkills();
  const [instructionDraft, setInstructionDraft] = useState("");
  const [agentInstructionDraft, setAgentInstructionDraft] = useState("");
  const [selectedByConnection, setSelectedByConnection] = useState<Record<string, string[]>>({});
  const [skillQuery, setSkillQuery] = useState("");
  const [skillResults, setSkillResults] = useState<SkillSearchResult[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rows = useMemo(
    () =>
      (connectorCatalog.data ?? []).flatMap((provider) =>
        provider.connections.map((connection) => ({
          provider,
          connection,
          tools: toolsForConnection(provider, connection),
        })),
      ),
    [connectorCatalog.data],
  );

  useEffect(() => {
    setScopeKind(departmentScopeId ? "department" : "organization");
  }, [agent.id, departmentScopeId]);

  useEffect(() => {
    setInstructionDraft(scope.data?.instruction?.body ?? "");
    setNotice(null);
    setError(null);
  }, [scope.data?.instruction?.body, scopeKind, scopeId]);

  useEffect(() => {
    const agentInstruction = (effective.data?.instructions ?? []).find(
      (item) => item.scopeKind === "agent",
    );
    setAgentInstructionDraft(agentInstruction?.body ?? "");
  }, [agent.id, effective.data?.instructions]);

  useEffect(() => {
    const next: Record<string, string[]> = {};
    for (const row of rows) {
      next[row.connection.id] = (scope.data?.connectors ?? [])
        .filter((item) => item.connectionId === row.connection.id && item.enabled)
        .map((item) => `${item.serviceKey}:${item.toolKey}`);
    }
    setSelectedByConnection((prev) => (sameConnectionSelection(prev, next) ? prev : next));
  }, [rows, scope.data?.connectors]);

  const scopeSummary = (scopes.data?.scopes ?? []).find(
    (item) => item.scopeKind === scopeKind && item.scopeId === (scopeId ?? "organization"),
  );
  const scopeLabel = scopeDisplayName(scopeKind, department);
  const effectiveSkills = [...(effective.data?.skills ?? [])].sort((a, b) =>
    compareText(a.skillName, b.skillName),
  );
  const effectiveConnectors = [...(effective.data?.connectors ?? [])].sort((a, b) =>
    compareText(a.itemKey, b.itemKey),
  );
  const scopeSkills = [...(scope.data?.skills ?? [])].sort((a, b) =>
    compareText(a.skillName, b.skillName),
  );
  const scopeTools = [...(scope.data?.tools ?? [])].sort((a, b) =>
    compareText(a.toolKey, b.toolKey),
  );
  const effectiveTools = [...(effective.data?.tools ?? [])].sort((a, b) =>
    compareText(a.label, b.label),
  );
  const effectiveToolByKey = new Map(effectiveTools.map((item) => [item.toolKey, item]));
  const scopeToolByKey = new Map(scopeTools.map((item) => [item.toolKey, item]));
  const toolCatalogRows = [...(toolCatalog.data?.tools ?? [])].sort(
    (a, b) => compareText(a.group, b.group) || compareText(a.label, b.label),
  );

  function setTool(connectionId: string, token: string, checked: boolean) {
    setSelectedByConnection((prev) => {
      const current = new Set(prev[connectionId] ?? []);
      if (checked) current.add(token);
      else current.delete(token);
      return { ...prev, [connectionId]: Array.from(current) };
    });
  }

  function connectorLabel(item: WorkforceEffectiveConnector): string {
    const row = rows.find((candidate) => candidate.connection.id === item.connectionId);
    const token = `${item.serviceKey}:${item.toolKey}`;
    const tool = row?.tools.find((candidate) => candidate.token === token);
    if (!row || !tool) return item.itemKey;
    return `${row.connection.displayName} · ${tool.name}`;
  }

  async function saveInstructions() {
    setNotice(null);
    setError(null);
    try {
      await updateInstructions.mutateAsync({ scopeKind, scopeId, body: instructionDraft });
      setNotice(`${scopeLabel} instructions saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    }
  }

  async function saveAgentInstructions() {
    setNotice(null);
    setError(null);
    try {
      await updateAgentInstructions.mutateAsync({
        agentId: agent.id,
        body: agentInstructionDraft,
      });
      setNotice("Agent instructions saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    }
  }

  async function saveConnector(connectionId: string, tools: string[], enabled: boolean) {
    setNotice(null);
    setError(null);
    try {
      await upsertConnector.mutateAsync({
        scopeKind,
        scopeId,
        connectionId,
        tools,
        enabled,
        replace: true,
        reconcile: true,
        materialize: true,
      });
      setNotice(`${scopeLabel} connector actions saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connector save failed");
    }
  }

  async function searchSkills() {
    setNotice(null);
    setError(null);
    try {
      const response = await skillSearch.mutateAsync({
        query: skillQuery,
        sources: ["github_marketplace"],
        limit: 8,
      });
      setSkillResults(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    }
  }

  async function saveSkill(hit: SkillSearchResult, enabled = true) {
    if (!hit.source_ref) return;
    setNotice(null);
    setError(null);
    try {
      await upsertSkill.mutateAsync({
        scopeKind,
        scopeId,
        skillName: hit.name,
        source: hit.source,
        sourceRef: hit.source_ref,
        enabled,
        reconcile: true,
      });
      setNotice(`${scopeLabel} skill saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Skill save failed");
    }
  }

  async function toggleScopeSkill(skillName: string, enabled: boolean) {
    const current = scopeSkills.find((item) => item.skillName === skillName);
    if (!current) return;
    setNotice(null);
    setError(null);
    try {
      await upsertSkill.mutateAsync({
        scopeKind,
        scopeId,
        skillName,
        source: current.source,
        sourceRef: current.sourceRef,
        enabled,
        reconcile: true,
      });
      setNotice(`${scopeLabel} skill saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Skill save failed");
    }
  }

  async function setScopeTool(tool: WorkforceToolCatalogItem, enabled: boolean) {
    setNotice(null);
    setError(null);
    try {
      await upsertTool.mutateAsync({
        scopeKind,
        scopeId,
        toolKey: tool.toolKey,
        enabled,
        reconcile: true,
      });
      setNotice(`${scopeLabel} tool permission saved.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Tool permission save failed");
    }
  }

  async function toggleOverride(
    itemKind: "connector" | "skill" | "tool",
    itemKey: string,
    enabled: boolean,
  ) {
    setNotice(null);
    setError(null);
    try {
      await upsertOverride.mutateAsync({
        agentId: agent.id,
        itemKind,
        itemKey,
        enabled,
        reconcile: true,
      });
      setNotice("Agent override saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Override save failed");
    }
  }

  function toolStateLabel(tool: WorkforceToolCatalogItem): string {
    const assignment = scopeToolByKey.get(tool.toolKey);
    if (!assignment) return "Not set here";
    return assignment.enabled ? "Granted here" : "Denied here";
  }

  function inheritedToolLabel(tool: WorkforceEffectiveTool | undefined): string {
    if (!tool) return "No inherited grant";
    if (!tool.inherited) return "Agent override";
    const last = tool.inheritedFrom[tool.inheritedFrom.length - 1];
    if (!last) return "Inherited";
    return last.scopeKind === "organization" ? "Organization" : last.scopeId;
  }

  return (
    <div className="mc-fade-up space-y-4 p-4">
      <section className="mc-surface flex flex-wrap items-center justify-between gap-3 p-3">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setScopeKind("organization")}
            disabled={!editable}
            className={cn(
              "mc-label rounded-full px-2.5 py-1.5 transition-colors disabled:opacity-50",
              scopeKind === "organization"
                ? "bg-accent-orange/15 text-accent-orange"
                : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            )}
          >
            Organization
          </button>
          {departmentScopeId && (
            <>
              <ChevronRightIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
              <button
                type="button"
                onClick={() => setScopeKind("department")}
                disabled={!editable}
                className={cn(
                  "mc-label rounded-full px-2.5 py-1.5 transition-colors disabled:opacity-50",
                  scopeKind === "department"
                    ? "bg-accent-orange/15 text-accent-orange"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
              >
                {department}
              </button>
            </>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="mc-value">
            {scopeSummary?.agentIds.length ?? 0} agents
          </Badge>
          <Badge variant="outline" className="mc-value">
            rev {scope.data?.revision ?? scopes.data?.revision ?? "-"}
          </Badge>
        </div>
      </section>

      <Tabs defaultValue="tools" className="space-y-4">
        <TabsList variant="line" className="flex-wrap" aria-label="Permission groups">
          <TabsTrigger value="tools">
            <TerminalIcon /> Tools
          </TabsTrigger>
          <TabsTrigger value="instructions">
            <FileTextIcon /> Instructions
          </TabsTrigger>
          <TabsTrigger value="skills">
            <PuzzleIcon /> Skills
          </TabsTrigger>
          <TabsTrigger value="connectors">
            <PlugIcon /> Connectors
          </TabsTrigger>
        </TabsList>

        <TabsContent value="tools" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_25rem]">
            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div>
                  <div className="mc-label">{scopeLabel} Builtin Tools</div>
                  <div className="text-xs text-muted-foreground">
                    Explicit grant or deny rows for this inheritance level.
                  </div>
                </div>
                <Badge variant="secondary">{scopeTools.length}</Badge>
              </div>
              <div className="grid max-h-[32rem] gap-2 overflow-y-auto p-3 md:grid-cols-2">
                {toolCatalogRows.map((tool) => {
                  const assignment = scopeToolByKey.get(tool.toolKey);
                  return (
                    <div
                      key={tool.toolKey}
                      data-testid={`scope-tool-row-${tool.toolKey}`}
                      className="flex min-w-0 flex-col gap-2 rounded-md border border-border/70 px-3 py-2 text-sm"
                    >
                      <div className="min-w-0">
                        <div className="flex min-w-0 items-center justify-between gap-2">
                          <span className="truncate font-medium">{tool.label}</span>
                          <Badge variant={assignment?.enabled ? "default" : "outline"}>
                            {toolStateLabel(tool)}
                          </Badge>
                        </div>
                        <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                          {tool.toolKey} · {tool.description}
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant={assignment?.enabled ? "secondary" : "outline"}
                          disabled={!editable || upsertTool.isPending}
                          onClick={() => void setScopeTool(tool, true)}
                        >
                          Grant here
                        </Button>
                        <Button
                          size="sm"
                          variant={assignment && !assignment.enabled ? "secondary" : "outline"}
                          disabled={!editable || upsertTool.isPending}
                          onClick={() => void setScopeTool(tool, false)}
                        >
                          Deny here
                        </Button>
                      </div>
                    </div>
                  );
                })}
                {!toolCatalog.isLoading && toolCatalogRows.length === 0 && (
                  <div className="text-sm text-muted-foreground">No builtin tools reported.</div>
                )}
              </div>
            </section>

            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div>
                  <div className="mc-label text-accent-cyan">Effective Agent Tools</div>
                  <div className="text-xs text-muted-foreground">{agentDisplayName(agent)}</div>
                </div>
                <Badge variant="secondary">
                  {effectiveTools.filter((item) => item.enabled).length}
                </Badge>
              </div>
              <div className="max-h-[32rem] divide-y divide-border-dimmer overflow-y-auto">
                {toolCatalogRows.map((catalogItem) => {
                  const tool = effectiveToolByKey.get(catalogItem.toolKey);
                  const enabled = tool?.enabled ?? false;
                  return (
                    <div
                      key={catalogItem.toolKey}
                      data-testid={`effective-tool-row-${catalogItem.toolKey}`}
                      className="p-3"
                    >
                      <div className="flex min-w-0 items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium">{catalogItem.label}</div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {catalogItem.toolKey} · {inheritedToolLabel(tool)}
                          </div>
                        </div>
                        <Badge variant={enabled ? "default" : "outline"}>
                          {enabled ? "Enabled" : "Disabled"}
                        </Badge>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="mt-2"
                        disabled={!editable || upsertOverride.isPending}
                        onClick={() => void toggleOverride("tool", catalogItem.toolKey, !enabled)}
                      >
                        {enabled ? "Disable for agent" : "Enable for agent"}
                      </Button>
                    </div>
                  );
                })}
                {!toolCatalog.isLoading && toolCatalogRows.length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">
                    No builtin tools reported.
                  </div>
                )}
              </div>
            </section>
          </div>
        </TabsContent>

        <TabsContent value="instructions" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_23rem]">
            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div className="mc-label">{scopeLabel} Instructions</div>
                <Button
                  size="sm"
                  disabled={!editable || updateInstructions.isPending}
                  onClick={() => void saveInstructions()}
                >
                  <SaveIcon /> Save
                </Button>
              </div>
              <Textarea
                className="min-h-52 resize-y rounded-none border-0 font-mono text-xs focus-visible:ring-0"
                value={instructionDraft}
                onChange={(event) => setInstructionDraft(event.target.value)}
                disabled={!editable}
                aria-label={`${scopeLabel} instructions`}
              />
            </section>

            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div className="mc-label text-accent-cyan">Inherited Instructions</div>
                <Badge variant="secondary">{effective.data?.instructions?.length ?? 0}</Badge>
              </div>
              <div className="max-h-64 divide-y divide-border-dimmer overflow-y-auto">
                {(effective.data?.instructions ?? []).map((item) => (
                  <div key={item.id} className="p-3">
                    <div className="text-xs font-medium text-muted-foreground">
                      {item.scopeKind === "organization" ? "Organization" : item.scopeId}
                    </div>
                    <div className="mt-1 line-clamp-3 text-xs">{item.body}</div>
                  </div>
                ))}
                {!effective.isLoading && (effective.data?.instructions ?? []).length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">
                    No inherited instructions.
                  </div>
                )}
              </div>
            </section>
          </div>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div>
                <div className="mc-label">Agent Instructions</div>
                <div className="text-xs text-muted-foreground">{agentDisplayName(agent)}</div>
              </div>
              <Button
                size="sm"
                disabled={!editable || updateAgentInstructions.isPending}
                onClick={() => void saveAgentInstructions()}
              >
                <SaveIcon /> Save
              </Button>
            </div>
            <Textarea
              className="min-h-40 resize-y rounded-none border-0 font-mono text-xs focus-visible:ring-0"
              value={agentInstructionDraft}
              onChange={(event) => setAgentInstructionDraft(event.target.value)}
              disabled={!editable}
              aria-label="Agent instructions"
            />
          </section>
        </TabsContent>

        <TabsContent value="skills" className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_23rem]">
            <section className="mc-surface">
              <div className="mc-label border-b border-border-dimmer px-3 py-2">
                {scopeLabel} Skills
              </div>
              <div className="space-y-3 p-3">
                <form
                  className="flex gap-2"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void searchSkills();
                  }}
                >
                  <Input
                    value={skillQuery}
                    onChange={(event) => setSkillQuery(event.target.value)}
                    placeholder="Search skills"
                    aria-label="Search inherited skills"
                  />
                  <Button
                    type="submit"
                    disabled={!editable || !skillQuery.trim() || skillSearch.isPending}
                  >
                    <SearchIcon /> Search
                  </Button>
                </form>
                <div className="grid gap-2 md:grid-cols-2">
                  {skillResults.map((hit) => (
                    <button
                      key={`${hit.source}:${hit.source_ref ?? hit.name}`}
                      type="button"
                      className="rounded-md border border-border px-3 py-2 text-left hover:bg-muted/40 disabled:opacity-50"
                      disabled={!editable || !hit.source_ref || upsertSkill.isPending}
                      onClick={() => void saveSkill(hit, true)}
                    >
                      <div className="truncate text-sm font-medium">{hit.name}</div>
                      <div className="line-clamp-2 text-xs text-muted-foreground">
                        {hit.description || hit.source_ref}
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            </section>

            <section className="mc-surface">
              <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
                <div className="mc-label">Scope Skills</div>
                <Badge variant="secondary">
                  {scopeSkills.filter((item) => item.enabled).length}
                </Badge>
              </div>
              <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
                {scopeSkills.map((item) => (
                  <div key={item.skillName} className="p-3">
                    <div className="truncate text-sm font-medium">{item.skillName}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {item.sourceRef || item.source}
                    </div>
                    <Button
                      className="mt-2"
                      size="xs"
                      variant="outline"
                      disabled={!editable || upsertSkill.isPending}
                      onClick={() => void toggleScopeSkill(item.skillName, !item.enabled)}
                    >
                      {item.enabled ? "Disable" : "Enable"}
                    </Button>
                  </div>
                ))}
                {!scope.isLoading && scopeSkills.length === 0 && (
                  <div className="p-4 text-sm text-muted-foreground">No scope skills.</div>
                )}
              </div>
            </section>
          </div>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div className="mc-label text-accent-cyan">Effective Skills</div>
              <Badge variant="secondary">
                {effectiveSkills.filter((item) => item.enabled).length}
              </Badge>
            </div>
            <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
              {effectiveSkills.map((item) => (
                <div key={item.itemKey} className="flex items-start justify-between gap-3 p-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{item.skillName}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {inheritedSourceLabel(item)}
                    </div>
                  </div>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={!editable || upsertOverride.isPending}
                    onClick={() => void toggleOverride("skill", item.itemKey, !item.enabled)}
                  >
                    {item.enabled ? "Disable for agent" : "Enable for agent"}
                  </Button>
                </div>
              ))}
              {!effective.isLoading && effectiveSkills.length === 0 && (
                <div className="p-4 text-sm text-muted-foreground">No inherited skills.</div>
              )}
            </div>
          </section>
        </TabsContent>

        <TabsContent value="connectors" className="space-y-4">
          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div>
                <div className="mc-label">{scopeLabel} Connector Actions</div>
                <div className="text-xs text-muted-foreground">
                  {(scope.data?.connectors ?? []).filter((item) => item.enabled).length} active
                </div>
              </div>
            </div>
            <div className="space-y-3 p-3">
              {rows.map(({ provider, connection, tools }) => {
                const selected = selectedByConnection[connection.id] ?? [];
                return (
                  <div key={connection.id} className="rounded-md border border-border-dimmer">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
                      <div>
                        <div className="text-sm font-medium">{connection.displayName}</div>
                        <div className="text-xs text-muted-foreground">
                          {provider.name} · {selected.length}/{tools.length} permissions
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!editable || tools.length === 0 || upsertConnector.isPending}
                          onClick={() => void saveConnector(connection.id, [], false)}
                        >
                          Disable all
                        </Button>
                        <Button
                          size="sm"
                          disabled={
                            !editable || selected.length === 0 || upsertConnector.isPending
                          }
                          onClick={() => void saveConnector(connection.id, selected, true)}
                        >
                          <PlugIcon /> Save permissions
                        </Button>
                      </div>
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
                            onChange={(event) =>
                              setTool(connection.id, tool.token, event.target.checked)
                            }
                            aria-label={`${scopeLabel} ${connection.displayName} ${tool.name}`}
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
                  </div>
                );
              })}
              {!connectorCatalog.isLoading && rows.length === 0 && (
                <div className="text-sm text-muted-foreground">No connector connections.</div>
              )}
            </div>
          </section>

          <section className="mc-surface">
            <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
              <div className="mc-label text-accent-cyan">Effective Connector Permissions</div>
              <Badge variant="secondary">
                {effectiveConnectors.filter((item) => item.enabled).length}
              </Badge>
            </div>
            <div className="max-h-80 divide-y divide-border-dimmer overflow-y-auto">
              {effectiveConnectors.map((item) => (
                <div key={item.itemKey} className="flex items-start justify-between gap-3 p-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{connectorLabel(item)}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {inheritedSourceLabel(item)}
                    </div>
                  </div>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={!editable || upsertOverride.isPending}
                    onClick={() => void toggleOverride("connector", item.itemKey, !item.enabled)}
                  >
                    {item.enabled ? "Disable for agent" : "Enable for agent"}
                  </Button>
                </div>
              ))}
              {!effective.isLoading && effectiveConnectors.length === 0 && (
                <div className="p-4 text-sm text-muted-foreground">
                  No inherited connector permissions.
                </div>
              )}
            </div>
          </section>
        </TabsContent>
      </Tabs>

      <div className="min-h-5 text-sm">
        {error && <span className="text-destructive">{error}</span>}
        {!error && notice && <span className="text-muted-foreground">{notice}</span>}
      </div>
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
    <div className="mc-fade-up grid min-h-0 gap-4 p-4 xl:grid-cols-[20rem_minmax(0,1fr)]">
      <section className="mc-surface min-h-[34rem]">
        <div className="flex items-center justify-between border-b border-border-dimmer px-3 py-2">
          <div className="mc-value truncate text-xs">{tree.data?.path || "."}</div>
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
                <FolderIcon className="size-4 shrink-0 text-accent-amber" />
              ) : (
                <FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              {entry.type === "file" && <span className="mc-value text-2xs">{entry.size}</span>}
            </button>
          ))}
          {tree.isError && (
            <div className="p-2 text-sm text-destructive">
              {tree.error instanceof Error ? tree.error.message : "File tree unavailable"}
            </div>
          )}
        </div>
      </section>
      <section className="mc-surface flex min-h-[34rem] flex-col">
        <div className="flex items-center justify-between gap-2 border-b border-border-dimmer px-3 py-2">
          <div className="mc-value min-w-0 truncate text-xs">{selectedPath || "Select file"}</div>
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
          [...grouped.harness].sort(compareAgentsByName)[0]?.id ??
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
  const workforceEditable = Boolean(selectedAgent && selectedTier !== "workflow");

  const isConfigDirty = imageSnapshot
    ? configText !== JSON.stringify(imageSnapshot.image.config, null, 2) ||
      instructionsText !== (imageSnapshot.image.instructions ?? "")
    : false;

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
    if (tierRequiresEditConfirmation(selectedTier)) {
      setPendingSave({ body, label, tier: selectedTier });
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
        <main className="flex min-h-0 flex-col overflow-hidden bg-bg-base">
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
                <div className="border-b border-accent-red/30 bg-accent-red/10 px-5 py-2 text-sm text-accent-red">
                  {image.error instanceof Error ? image.error.message : "Agent image unavailable"}
                </div>
              )}
              <Tabs
                value={tab}
                onValueChange={(value) => setTab(value as WorkForceTab)}
                className="min-h-0 flex-1 overflow-hidden"
              >
                <TabsList variant="line" className="mx-5 mt-3 flex-wrap">
                  <TabsTrigger value="overview">
                    <BotIcon /> Overview
                  </TabsTrigger>
                  <TabsTrigger value="config" disabled={!editable}>
                    <SlidersHorizontalIcon /> Config
                    {isConfigDirty && editable && (
                      <span className="size-1.5 rounded-full bg-accent-amber" aria-hidden="true" />
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="permissions" disabled={!workforceEditable}>
                    <UsersIcon /> Permissions
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
                      dirty={isConfigDirty}
                    />
                  </TabsContent>
                  <TabsContent value="permissions">
                    {selectedAgent && (
                      <PermissionsTab agent={selectedAgent} editable={workforceEditable} />
                    )}
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
            <DialogTitle>{editConfirmationTitle(pendingSave?.tier)}</DialogTitle>
            <DialogDescription>{editConfirmationDescription(pendingSave?.tier)}</DialogDescription>
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
              {editConfirmationButtonLabel(pendingSave?.tier)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AccessGate>
  );
}
