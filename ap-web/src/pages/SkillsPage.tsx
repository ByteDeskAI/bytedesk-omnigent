import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  FileTextIcon,
  PackageIcon,
  PuzzleIcon,
  RefreshCwIcon,
  SearchIcon,
  Trash2Icon,
} from "lucide-react";
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
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useApplySkillPreview,
  useCreateSkillPreview,
  useInstalledSkills,
  useSearchSkills,
  useSkillSources,
  type SkillPreview,
  type SkillSearchResult,
} from "@/hooks/useSkills";

const INSTALL_MODES = [
  { value: "replace", label: "Replace existing" },
  { value: "skip_existing", label: "Skip existing" },
  { value: "fail_on_existing", label: "Fail on existing" },
] as const;

export function SkillsPage() {
  const agents = useAvailableAgents();
  const sources = useSkillSources();
  const installed = useInstalledSkills();
  const search = useSearchSkills();
  const previewMutation = useCreateSkillPreview();
  const applyMutation = useApplySkillPreview();

  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("skills");
  const [sourceRef, setSourceRef] = useState("");
  const [shellCommand, setShellCommand] = useState("");
  const [installMode, setInstallMode] =
    useState<(typeof INSTALL_MODES)[number]["value"]>("replace");
  const [selectedResult, setSelectedResult] = useState<SkillSearchResult | null>(null);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [removeSkill, setRemoveSkill] = useState<string | null>(null);
  const hasDefaultedAgentSelection = useRef(false);

  const agentRows = useMemo(
    () =>
      (agents.data ?? []).filter(
        (agent) => agent.workflow !== true && Boolean(agent.department || agent.title),
      ),
    [agents.data],
  );
  useEffect(() => {
    if (!hasDefaultedAgentSelection.current && agentRows.length > 0) {
      setSelectedAgentIds(agentRows.map((agent) => agent.id));
      hasDefaultedAgentSelection.current = true;
    }
  }, [agentRows]);

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
  const allSelected = selectedAgentIds.length === agentRows.length && agentRows.length > 0;
  const searchDisabled =
    search.isPending ||
    query.trim().length === 0 ||
    !sourceAvailable ||
    !sourceSupportsSearch ||
    commandMissing;
  const previewDisabled =
    previewMutation.isPending ||
    selectedAgentIds.length === 0 ||
    !sourceAvailable ||
    !sourceSupportsPreview ||
    commandMissing ||
    (!commandSource && installSourceRef.trim().length === 0);

  const installedByName = useMemo(() => {
    const map = new Map<string, number>();
    for (const skill of installed.data ?? []) {
      map.set(skill.name, skill.agents.length);
    }
    return map;
  }, [installed.data]);

  function toggleAgent(agentId: string) {
    setSelectedAgentIds((prev) =>
      prev.includes(agentId) ? prev.filter((id) => id !== agentId) : [...prev, agentId],
    );
  }

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
        target_agent_ids: selectedAgentIds,
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
        target_agent_ids: selectedAgentIds,
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
    <div className="mx-auto flex min-h-full w-full max-w-6xl flex-col gap-6 px-6 py-8 pt-14">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <PuzzleIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />
          <div>
            <h1 className="text-2xl font-semibold">Skills</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Discover, preview, and persist agent skill packages.
            </p>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Refresh installed skills"
          onClick={() => void installed.refetch()}
        >
          <RefreshCwIcon />
        </Button>
      </header>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <main className="flex min-w-0 flex-col gap-4">
          <section className="rounded-lg border border-border bg-background p-4">
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

          <section className="rounded-lg border border-border bg-background">
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
                        <Badge variant={selected ? "default" : "secondary"}>{result.source}</Badge>
                        {result.version && <Badge variant="outline">{result.version}</Badge>}
                      </span>
                      {result.description && (
                        <span className="mt-1 block text-sm text-muted-foreground">
                          {result.description}
                        </span>
                      )}
                    </span>
                    {installedByName.has(result.name) && <Badge variant="outline">installed</Badge>}
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
        </main>

        <aside className="flex min-w-0 flex-col gap-4">
          <section className="rounded-lg border border-border bg-background">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <h2 className="text-sm font-medium">Agents</h2>
              <button
                type="button"
                className="text-xs text-muted-foreground hover:text-foreground"
                onClick={() =>
                  setSelectedAgentIds(allSelected ? [] : agentRows.map((agent) => agent.id))
                }
              >
                {allSelected ? "Clear" : "All"}
              </button>
            </div>
            <div className="max-h-72 overflow-y-auto p-2">
              {agentRows.map((agent) => (
                <label
                  key={agent.id}
                  className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 hover:bg-muted/50"
                >
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={selectedAgentIds.includes(agent.id)}
                    onChange={() => toggleAgent(agent.id)}
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-sm">{agent.display_name}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {agent.name}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </section>

          <section className="rounded-lg border border-border bg-background">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <h2 className="text-sm font-medium">Installed</h2>
              <span className="text-xs text-muted-foreground">{installed.data?.length ?? 0}</span>
            </div>
            <div className="max-h-[30rem] overflow-y-auto divide-y divide-border">
              {(installed.data ?? []).map((skill) => (
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
                      disabled={selectedAgentIds.length === 0}
                    >
                      <Trash2Icon />
                    </Button>
                  </div>
                  <div className="mt-2 text-xs text-muted-foreground">
                    {skill.agents.length} agent{skill.agents.length === 1 ? "" : "s"}
                  </div>
                </div>
              ))}
              {!installed.isLoading && (installed.data ?? []).length === 0 && (
                <div className="px-3 py-6 text-sm text-muted-foreground">No skills installed.</div>
              )}
            </div>
          </section>
        </aside>
      </div>
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
