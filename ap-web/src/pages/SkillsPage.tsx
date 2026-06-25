import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangleIcon,
  BotIcon,
  Building2Icon,
  CheckCircle2Icon,
  FileTextIcon,
  NetworkIcon,
  PackageIcon,
  PuzzleIcon,
  RefreshCwIcon,
  SearchIcon,
  Trash2Icon,
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
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  useApplySkillPreview,
  useCreateSkillPreview,
  useInstalledSkills,
  useSearchSkills,
  useSkillSources,
  type SkillPreview,
  type SkillSearchResult,
} from "@/hooks/useSkills";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";

const INSTALL_MODES = [
  { value: "replace", label: "Replace existing" },
  { value: "skip_existing", label: "Skip existing" },
  { value: "fail_on_existing", label: "Fail on existing" },
] as const;

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

export function SkillsPage() {
  const agents = useAvailableAgents();
  const sources = useSkillSources();
  const installed = useInstalledSkills();
  const search = useSearchSkills();
  const previewMutation = useCreateSkillPreview();
  const applyMutation = useApplySkillPreview();

  const [selectedScope, setSelectedScope] = useState<SkillScope>({
    kind: "organization",
    id: "omnigent",
  });
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("skills");
  const [sourceRef, setSourceRef] = useState("");
  const [shellCommand, setShellCommand] = useState("");
  const [installMode, setInstallMode] =
    useState<(typeof INSTALL_MODES)[number]["value"]>("replace");
  const [selectedResult, setSelectedResult] = useState<SkillSearchResult | null>(null);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [removeSkill, setRemoveSkill] = useState<string | null>(null);
  const hasDefaultedScope = useRef(false);

  const agentRows = useMemo(
    () =>
      (agents.data ?? []).filter(
        (agent) => agent.workflow !== true && Boolean(agent.department || agent.title),
      ),
    [agents.data],
  );

  const departmentGroups = useMemo<DepartmentGroup[]>(() => {
    const groups = new Map<string, AvailableAgent[]>();
    for (const agent of agentRows) {
      const department = departmentId(agent);
      groups.set(department, [...(groups.get(department) ?? []), agent]);
    }
    return [...groups.entries()]
      .map(([id, departmentAgents]) => ({
        id,
        agents: [...departmentAgents].sort((a, b) => a.display_name.localeCompare(b.display_name)),
      }))
      .sort((a, b) => a.id.localeCompare(b.id));
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

  const sourceRows = sources.data ?? [];
  const activeSource = sourceRows.find((item) => item.id === source);
  const commandSource = source === "freeform" || source === "configured";
  const sourceAvailable = activeSource?.available ?? true;
  const sourceSupportsSearch = activeSource?.supports_search ?? true;
  const sourceSupportsPreview = activeSource?.supports_preview ?? true;
  const commandMissing = commandSource && shellCommand.trim().length === 0;
  const installSourceRef = selectedResult?.source_ref ?? sourceRef.trim();
  const searchResults = search.data?.data ?? [];
  const searchErrors = search.data?.errors ?? [];
  const selectedScopeLabel = scopeLabel(selectedScope, agentRows);
  const searchDisabled =
    search.isPending ||
    query.trim().length === 0 ||
    !sourceAvailable ||
    !sourceSupportsSearch ||
    commandMissing;
  const previewDisabled =
    previewMutation.isPending ||
    targetAgentIds.length === 0 ||
    !sourceAvailable ||
    !sourceSupportsPreview ||
    commandMissing ||
    (!commandSource && installSourceRef.trim().length === 0);

  const installedByName = useMemo(() => {
    const map = new Map<string, number>();
    for (const skill of installed.data ?? []) {
      const scopedCount = skill.agents.filter((agent) => targetAgentIds.includes(agent.id)).length;
      if (scopedCount > 0) {
        map.set(skill.name, scopedCount);
      }
    }
    return map;
  }, [installed.data, targetAgentIds]);

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

  function runSearch() {
    setSelectedResult(null);
    setPreview(null);
    search.mutate({
      query,
      sources: [source],
      limit: 20,
      command:
        source === "freeform" || source === "configured"
          ? { shell: shellCommand, timeout_seconds: 60 }
          : undefined,
    });
  }

  function createInstallPreview() {
    const ref = selectedResult?.source_ref ?? sourceRef.trim();
    setRemoveSkill(null);
    previewMutation.mutate(
      {
        operation: "install",
        target_agent_ids: targetAgentIds,
        install_mode: installMode,
        source: selectedResult?.source ?? source,
        source_ref: ref || null,
        command:
          source === "freeform" || source === "configured"
            ? { shell: shellCommand, timeout_seconds: 120 }
            : null,
      },
      { onSuccess: setPreview },
    );
  }

  function createRemovePreview(skillName: string) {
    setRemoveSkill(skillName);
    previewMutation.mutate(
      {
        operation: "remove",
        target_agent_ids: targetAgentIds,
        skill_names: [skillName],
      },
      { onSuccess: setPreview },
    );
  }

  function applyPreview() {
    if (!preview) return;
    applyMutation.mutate({ previewId: preview.id }, { onSuccess: () => void installed.refetch() });
  }

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

        <main className="min-h-0 overflow-auto border-b border-border lg:border-r lg:border-b-0">
          <div className="space-y-4 p-4">
            <section className="rounded-md border border-border bg-background p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate text-sm font-semibold">Discover</h2>
                  <p className="truncate text-xs text-muted-foreground">
                    Preview installs for {selectedScopeLabel}.
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Refresh installed skills"
                  onClick={() => void installed.refetch()}
                >
                  <RefreshCwIcon />
                </Button>
              </div>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <Select
                  value={source}
                  onValueChange={(value) => {
                    setSource(value);
                    setSelectedResult(null);
                    setPreview(null);
                  }}
                >
                  <SelectTrigger className="w-48">
                    <SelectValue placeholder="Source" />
                  </SelectTrigger>
                  <SelectContent>
                    {sourceRows.map((item) => (
                      <SelectItem key={item.id} value={item.id}>
                        {item.label}
                        {item.available === false ? " (unavailable)" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Input
                  className="min-w-52 flex-1"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search skills"
                />
                <Button onClick={runSearch} disabled={searchDisabled}>
                  <SearchIcon /> Search
                </Button>
              </div>
              {!sourceAvailable && (
                <div className="mb-3 flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  <AlertTriangleIcon className="size-4 shrink-0" />
                  {activeSource?.unavailable_reason ?? "This source is unavailable."}
                </div>
              )}
              {sourceAvailable && !sourceSupportsSearch && (
                <p className="mb-3 text-xs text-muted-foreground">
                  Search is not supported for this source. Enter a source ref to preview an install.
                </p>
              )}
              {activeSource?.high_risk && (
                <div className="mb-3 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
                  <AlertTriangleIcon className="size-4 shrink-0" />
                  Commands run on the Omnigent server in a temporary workspace.
                </div>
              )}
              {commandSource && (
                <Textarea
                  value={shellCommand}
                  onChange={(event) => setShellCommand(event.target.value)}
                  placeholder="Command that writes one or more skills into the working directory"
                  className="mb-3 min-h-24 font-mono text-xs"
                />
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  className="max-w-md"
                  value={sourceRef}
                  onChange={(event) => setSourceRef(event.target.value)}
                  placeholder="Source ref for preview"
                />
                <Select
                  value={installMode}
                  onValueChange={(value) => setInstallMode(value as typeof installMode)}
                >
                  <SelectTrigger className="w-44">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {INSTALL_MODES.map((mode) => (
                      <SelectItem key={mode.value} value={mode.value}>
                        {mode.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  variant="secondary"
                  onClick={createInstallPreview}
                  disabled={previewDisabled}
                >
                  <PackageIcon /> Preview install
                </Button>
              </div>
              {search.isError && (
                <p role="alert" className="mt-3 text-sm text-destructive">
                  {search.error instanceof Error ? search.error.message : "Search failed."}
                </p>
              )}
              {searchErrors.length > 0 && (
                <div className="mt-3 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                  {searchErrors.join(" · ")}
                </div>
              )}
            </section>

            <section className="rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border px-4 py-3">
                <h2 className="text-sm font-medium">Search results</h2>
                <span className="text-xs text-muted-foreground">{searchResults.length}</span>
              </div>
              <div className="divide-y divide-border">
                {searchResults.length === 0 && (
                  <div className="px-4 py-8 text-sm text-muted-foreground">No results.</div>
                )}
                {searchResults.map((result) => {
                  const selected = selectedResult === result;
                  return (
                    <button
                      key={`${result.source}:${result.name}:${result.source_ref ?? ""}`}
                      type="button"
                      onClick={() => {
                        setSelectedResult(result);
                        setSourceRef(result.source_ref ?? result.name);
                        setSource(result.source);
                      }}
                      className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left hover:bg-muted/50"
                      data-selected={selected}
                    >
                      <span className="min-w-0">
                        <span className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{result.name}</span>
                          <Badge variant={selected ? "default" : "secondary"}>
                            {result.source}
                          </Badge>
                          {result.version && <Badge variant="outline">{result.version}</Badge>}
                        </span>
                        {result.description && (
                          <span className="mt-1 block text-sm text-muted-foreground">
                            {result.description}
                          </span>
                        )}
                      </span>
                      {installedByName.has(result.name) && (
                        <Badge variant="outline">installed</Badge>
                      )}
                    </button>
                  );
                })}
              </div>
            </section>

            <PreviewPanel
              preview={preview}
              removeSkill={removeSkill}
              isPending={previewMutation.isPending || applyMutation.isPending}
              applyResults={applyMutation.data?.data ?? []}
              error={
                previewMutation.error instanceof Error
                  ? previewMutation.error.message
                  : applyMutation.error instanceof Error
                    ? applyMutation.error.message
                    : null
              }
              onApply={applyPreview}
            />
          </div>
        </main>

        <aside className="min-h-0 overflow-auto">
          <div className="space-y-4 p-3">
            <section className="rounded-md border border-border bg-background">
              <div className="border-b border-border px-3 py-2">
                <h2 className="text-sm font-medium">Selected Scope</h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  {targetAgentIds.length} employee agent
                  {targetAgentIds.length === 1 ? "" : "s"} targeted
                </p>
              </div>
              <div className="p-3">
                <div className="rounded-md border border-border bg-muted/30 px-3 py-2">
                  <div className="truncate text-sm font-medium">{selectedScopeLabel}</div>
                  <div className="truncate text-xs text-muted-foreground">
                    {selectedScope.kind === "organization"
                      ? "All roster employees"
                      : selectedScope.kind === "department"
                        ? "Departmental roster"
                        : "Single employee"}
                  </div>
                </div>
              </div>
            </section>

            <section className="rounded-md border border-border bg-background">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <h2 className="text-sm font-medium">Available Skills</h2>
                <span className="text-xs text-muted-foreground">{scopedInstalled.length}</span>
              </div>
              <div className="max-h-[30rem] overflow-y-auto divide-y divide-border">
                {scopedInstalled.map((skill) => (
                  <div key={skill.name} className="px-3 py-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">{skill.name}</div>
                        <div className="line-clamp-2 text-xs text-muted-foreground">
                          {skill.description}
                        </div>
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove ${skill.name}`}
                        onClick={() => createRemovePreview(skill.name)}
                        disabled={targetAgentIds.length === 0}
                      >
                        <Trash2Icon />
                      </Button>
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

function PreviewPanel({
  preview,
  removeSkill,
  isPending,
  applyResults,
  error,
  onApply,
}: {
  preview: SkillPreview | null;
  removeSkill: string | null;
  isPending: boolean;
  applyResults: { agent_id: string; status: string; error: string | null }[];
  error: string | null;
  onApply: () => void;
}) {
  return (
    <section className="rounded-lg border border-border bg-background">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-medium">Preview</h2>
        {preview && <Badge variant="outline">{preview.operation}</Badge>}
      </div>
      {!preview && !error && (
        <div className="px-4 py-8 text-sm text-muted-foreground">No preview staged.</div>
      )}
      {error && (
        <div
          role="alert"
          className="m-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </div>
      )}
      {preview && (
        <div className="space-y-4 p-4">
          <div className="flex flex-wrap items-center gap-2">
            {(preview.skills.length > 0
              ? preview.skills
              : preview.skill_names.map((name) => ({ name }))
            ).map((skill) => (
              <Badge key={skill.name} variant="secondary">
                {skill.name}
              </Badge>
            ))}
            {removeSkill && <Badge variant="destructive">remove {removeSkill}</Badge>}
          </div>
          {preview.skills.map((skill) => (
            <div key={skill.name} className="rounded-md border border-border/70 bg-muted/30 p-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium">{skill.name}</div>
                  <div className="text-xs text-muted-foreground">{skill.description}</div>
                </div>
                <Badge variant="outline">{formatBytes(skill.total_bytes)}</Badge>
              </div>
              <div className="mt-2 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                {skill.files.slice(0, 6).map((file) => (
                  <div key={file.path} className="flex min-w-0 items-center gap-1.5">
                    <FileTextIcon className="size-3 shrink-0" />
                    <span className="truncate">{file.path}</span>
                    {file.binary && <Badge variant="outline">binary</Badge>}
                  </div>
                ))}
              </div>
            </div>
          ))}
          <div className="rounded-md border border-border/70">
            <div className="border-b border-border px-3 py-2 text-xs font-medium">
              Agent actions
            </div>
            <div className="divide-y divide-border">
              {preview.target_actions.map((action) => (
                <div
                  key={`${action.agent_id}:${action.skill_name}`}
                  className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
                >
                  <span className="min-w-0 truncate">
                    {action.agent_name} · {action.skill_name}
                  </span>
                  <Badge variant={action.action === "conflict" ? "destructive" : "outline"}>
                    {action.action}
                  </Badge>
                </div>
              ))}
            </div>
          </div>
          {applyResults.length > 0 && (
            <div className="rounded-md border border-border/70">
              <div className="border-b border-border px-3 py-2 text-xs font-medium">
                Apply results
              </div>
              <div className="divide-y divide-border">
                {applyResults.map((result) => (
                  <div key={result.agent_id} className="flex items-center gap-2 px-3 py-2 text-sm">
                    {result.status === "failed" ? (
                      <AlertTriangleIcon className="size-4 text-destructive" />
                    ) : (
                      <CheckCircle2Icon className="size-4 text-emerald-400" />
                    )}
                    <span>{result.agent_id}</span>
                    <span className="text-muted-foreground">{result.error ?? result.status}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="flex justify-end">
            <Button onClick={onApply} disabled={isPending}>
              Apply preview
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
