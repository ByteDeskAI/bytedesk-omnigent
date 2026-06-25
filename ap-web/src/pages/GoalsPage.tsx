import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangleIcon,
  BotIcon,
  Building2Icon,
  CheckCircle2Icon,
  CheckIcon,
  CircleDashedIcon,
  CircleDotIcon,
  FlagIcon,
  ListChecksIcon,
  NetworkIcon,
  PauseCircleIcon,
  PlusIcon,
  TargetIcon,
  XIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  useActivateGoal,
  useAddGoalDependency,
  useCreateGoal,
  useGoalEvents,
  useGoals,
  useUpdateGoal,
  useUpdateGoalDependency,
} from "@/hooks/useGoals";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import type {
  GoalActivationState,
  GoalReadinessKind,
  GoalRecord,
  GoalStatus,
  GoalTargetKind,
} from "@/lib/goalsApi";

type ScopeKind = "all" | GoalTargetKind;
type GoalView = "active" | "waiting" | "done";

interface ScopeOption {
  key: string;
  kind: ScopeKind;
  id: string;
  label: string;
  subtitle: string;
  count: number;
}

interface TargetOption {
  key: string;
  kind: GoalTargetKind;
  id: string;
  label: string;
  subtitle: string;
}

const STATUS_OPTIONS: GoalStatus[] = ["open", "assigned", "in_progress", "blocked", "done"];
const PRIORITY_OPTIONS = [1, 2, 3, 4, 5];

function scopeKey(kind: ScopeKind, id: string) {
  return `${kind}:${id}`;
}

function targetKey(kind: GoalTargetKind, id: string) {
  return `${kind}:${id}`;
}

function splitTargetKey(value: string): { kind: GoalTargetKind; id: string } {
  const [kind, ...rest] = value.split(":");
  return { kind: kind as GoalTargetKind, id: rest.join(":") };
}

function departmentId(agent: AvailableAgent): string {
  return agent.department?.trim() || "Unassigned";
}

function displayTarget(goal: GoalRecord): string {
  return goal.target_label || goal.target_id;
}

function statusLabel(status: GoalStatus): string {
  if (status === "in_progress") return "In progress";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function readinessLabel(readiness: GoalReadinessKind): string {
  return readiness.charAt(0).toUpperCase() + readiness.slice(1);
}

function activationLabel(activation: GoalActivationState): string {
  return activation.charAt(0).toUpperCase() + activation.slice(1);
}

function formattedTime(epochSeconds: number): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(epochSeconds * 1000));
}

function goalMatchesScope(goal: GoalRecord, scope: ScopeOption): boolean {
  if (scope.kind === "all") return true;
  return goal.target_kind === scope.kind && goal.target_id === scope.id;
}

function goalMatchesView(goal: GoalRecord, view: GoalView): boolean {
  if (view === "done") return goal.status === "done";
  if (view === "waiting") return goal.status !== "done" && goal.activation_state !== "ready";
  return goal.status !== "done" && goal.activation_state === "ready";
}

function pendingDependencyCount(goal: GoalRecord): number {
  return goal.dependencies.filter((dependency) => dependency.status === "pending").length;
}

function targetOptionsForAgents(agents: AvailableAgent[]): TargetOption[] {
  const departments = Array.from(new Set(agents.map(departmentId))).sort((a, b) =>
    a.localeCompare(b),
  );
  return [
    {
      key: targetKey("organization", "omnigent"),
      kind: "organization",
      id: "omnigent",
      label: "Organization",
      subtitle: "All Omnigent work",
    },
    ...departments.map((department) => ({
      key: targetKey("department", department),
      kind: "department" as const,
      id: department,
      label: department,
      subtitle: "Department goal",
    })),
    ...agents.map((agent) => ({
      key: targetKey("agent", agent.id),
      kind: "agent" as const,
      id: agent.id,
      label: agent.display_name,
      subtitle: agent.title || agent.name,
    })),
  ];
}

function scopeOptionsForGoals(agents: AvailableAgent[], goals: GoalRecord[]): ScopeOption[] {
  const targets = targetOptionsForAgents(agents);
  return [
    {
      key: scopeKey("all", "all"),
      kind: "all",
      id: "all",
      label: "All scopes",
      subtitle: "Organization, departments, agents",
      count: goals.length,
    },
    ...targets.map((target) => ({
      key: scopeKey(target.kind, target.id),
      kind: target.kind,
      id: target.id,
      label: target.label,
      subtitle: target.subtitle,
      count: goals.filter(
        (goal) => goal.target_kind === target.kind && goal.target_id === target.id,
      ).length,
    })),
  ];
}

function iconForScope(kind: ScopeKind, className = "size-4") {
  if (kind === "organization") return <Building2Icon className={className} />;
  if (kind === "department") return <NetworkIcon className={className} />;
  if (kind === "agent") return <BotIcon className={className} />;
  return <TargetIcon className={className} />;
}

function activationIcon(goal: GoalRecord) {
  if (goal.status === "blocked") return <AlertTriangleIcon className="size-3.5" />;
  if (goal.status === "done") return <CheckCircle2Icon className="size-3.5" />;
  if (goal.activation_state === "paused") return <PauseCircleIcon className="size-3.5" />;
  if (goal.activation_state === "waiting") return <CircleDashedIcon className="size-3.5" />;
  return <CircleDotIcon className="size-3.5" />;
}

export function GoalsPage() {
  const agents = useAvailableAgents();
  const goals = useGoals({ include_dependencies: true });
  const createGoal = useCreateGoal();
  const updateGoal = useUpdateGoal();
  const activateGoal = useActivateGoal();
  const addDependency = useAddGoalDependency();
  const updateDependency = useUpdateGoalDependency();
  useGoalEvents(true);

  const agentRows = useMemo(() => agents.data ?? [], [agents.data]);
  const goalRows = useMemo(() => goals.data ?? [], [goals.data]);
  const targetOptions = useMemo(() => targetOptionsForAgents(agentRows), [agentRows]);
  const scopeOptions = useMemo(
    () => scopeOptionsForGoals(agentRows, goalRows),
    [agentRows, goalRows],
  );

  const [selectedScopeKey, setSelectedScopeKey] = useState(scopeKey("all", "all"));
  const [view, setView] = useState<GoalView>("active");
  const [selectedGoalId, setSelectedGoalId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState("3");
  const [targetValue, setTargetValue] = useState(targetKey("organization", "omnigent"));
  const [readiness, setReadiness] = useState<GoalReadinessKind>("immediate");
  const [dependencyDraft, setDependencyDraft] = useState("");
  const [newDependency, setNewDependency] = useState("");
  const [error, setError] = useState<string | null>(null);

  const selectedScope =
    scopeOptions.find((scope) => scope.key === selectedScopeKey) ?? scopeOptions[0];
  const selectedGoal = selectedGoalId
    ? goalRows.find((goal) => goal.id === selectedGoalId) ?? null
    : null;

  const filteredGoals = useMemo(
    () =>
      goalRows
        .filter((goal) => (selectedScope ? goalMatchesScope(goal, selectedScope) : true))
        .filter((goal) => goalMatchesView(goal, view))
        .sort((a, b) => a.priority - b.priority || b.updated_at - a.updated_at),
    [goalRows, selectedScope, view],
  );

  const stats = useMemo(
    () => ({
      total: goalRows.length,
      ready: goalRows.filter((goal) => goal.activation_state === "ready" && goal.status !== "done")
        .length,
      waiting: goalRows.filter(
        (goal) => goal.activation_state !== "ready" && goal.status !== "done",
      ).length,
      blocked: goalRows.filter((goal) => goal.status === "blocked").length,
    }),
    [goalRows],
  );

  useEffect(() => {
    if (selectedScope?.kind && selectedScope.kind !== "all") {
      setTargetValue(targetKey(selectedScope.kind, selectedScope.id));
    }
  }, [selectedScope?.id, selectedScope?.kind]);

  useEffect(() => {
    if (selectedGoalId && !goalRows.some((goal) => goal.id === selectedGoalId)) {
      setSelectedGoalId(null);
    }
  }, [goalRows, selectedGoalId]);

  async function submitGoal() {
    setError(null);
    const target = splitTargetKey(targetValue);
    const option = targetOptions.find((candidate) => candidate.key === targetValue);
    const dependencies = dependencyDraft
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((label) => ({ kind: "manual" as const, label, status: "pending" as const }));
    try {
      const goal = await createGoal.mutateAsync({
        title: title.trim(),
        priority: Number(priority),
        target_kind: target.kind,
        target_id: target.id,
        target_label: option?.label,
        readiness_kind: dependencies.length > 0 ? "dependent" : readiness,
        dependencies,
      });
      setSelectedGoalId(goal.id);
      setView(goal.activation_state === "ready" ? "active" : "waiting");
      setTitle("");
      setDependencyDraft("");
      setReadiness("immediate");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create goal.");
    }
  }

  async function addGoalDependency(goal: GoalRecord) {
    const label = newDependency.trim();
    if (!label) return;
    setError(null);
    try {
      await addDependency.mutateAsync({
        goalId: goal.id,
        payload: { kind: "manual", label, status: "pending" },
      });
      setNewDependency("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to add dependency.");
    }
  }

  const busy =
    createGoal.isPending ||
    updateGoal.isPending ||
    activateGoal.isPending ||
    addDependency.isPending ||
    updateDependency.isPending;
  const canCreate = title.trim().length > 0 && !busy;

  return (
    <div className="fixed inset-3 z-50 flex min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-background shadow-2xl">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted">
            <TargetIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold">Goals</h1>
            <p className="truncate text-xs text-muted-foreground">
              {selectedScope?.label ?? "All scopes"}
            </p>
          </div>
        </div>
        <Button variant="ghost" size="icon" asChild aria-label="Close goals">
          <Link to="/">
            <XIcon />
          </Link>
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[18rem_minmax(0,1fr)_24rem]">
        <aside className="min-h-0 border-b border-border lg:border-r lg:border-b-0">
          <div className="flex h-full min-h-0 flex-col">
            <div className="grid grid-cols-4 gap-2 border-b border-border p-3">
              <Metric value={stats.total} label="Total" />
              <Metric value={stats.ready} label="Ready" />
              <Metric value={stats.waiting} label="Waiting" />
              <Metric value={stats.blocked} label="Blocked" />
            </div>
            <div className="shrink-0 border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
              Scope
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-2">
              {scopeOptions.map((scope) => (
                <button
                  key={scope.key}
                  type="button"
                  onClick={() => setSelectedScopeKey(scope.key)}
                  className={cn(
                    "mb-1 flex min-h-12 w-full cursor-pointer items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
                    scope.key === selectedScopeKey
                      ? "border-border bg-muted text-foreground"
                      : "border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                  )}
                  aria-pressed={scope.key === selectedScopeKey}
                >
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background">
                    {iconForScope(scope.kind)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{scope.label}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {scope.subtitle}
                    </span>
                  </span>
                  <Badge variant="secondary">{scope.count}</Badge>
                </button>
              ))}
            </div>
          </div>
        </aside>

        <main className="min-h-0 overflow-hidden border-b border-border lg:border-r lg:border-b-0">
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
              <div className="flex items-center gap-1">
                <ViewButton active={view === "active"} onClick={() => setView("active")}>
                  Active
                </ViewButton>
                <ViewButton active={view === "waiting"} onClick={() => setView("waiting")}>
                  Waiting
                </ViewButton>
                <ViewButton active={view === "done"} onClick={() => setView("done")}>
                  Done
                </ViewButton>
              </div>
              <div className="text-xs text-muted-foreground">
                {goals.isFetching ? "Refreshing" : `${filteredGoals.length} shown`}
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto p-3">
              {filteredGoals.map((goal) => (
                <GoalRow
                  key={goal.id}
                  goal={goal}
                  selected={goal.id === selectedGoalId}
                  onSelect={() => setSelectedGoalId(goal.id)}
                />
              ))}
              {filteredGoals.length === 0 && (
                <div className="flex min-h-52 items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
                  No goals match this view.
                </div>
              )}
            </div>
          </div>
        </main>

        <section className="min-h-0 overflow-auto">
          <div className="space-y-4 p-3">
            <div className="rounded-md border border-border">
              <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                <PlusIcon className="size-4 text-muted-foreground" />
                <h2 className="text-sm font-semibold">New Goal</h2>
              </div>
              <div className="space-y-3 p-3">
                <Field label="Title">
                  <Input value={title} onChange={(event) => setTitle(event.target.value)} />
                </Field>
                <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2">
                  <Field label="Priority">
                    <Select value={priority} onValueChange={setPriority}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {PRIORITY_OPTIONS.map((option) => (
                          <SelectItem key={option} value={String(option)}>
                            P{option}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </Field>
                  <Field label="Readiness">
                    <Select
                      value={readiness}
                      onValueChange={(value) => setReadiness(value as GoalReadinessKind)}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="immediate">Immediate</SelectItem>
                        <SelectItem value="dependent">Dependent</SelectItem>
                        <SelectItem value="deferred">Deferred</SelectItem>
                      </SelectContent>
                    </Select>
                  </Field>
                </div>
                <Field label="Target">
                  <Select value={targetValue} onValueChange={setTargetValue}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {targetOptions.map((option) => (
                        <SelectItem key={option.key} value={option.key}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Field label="Dependencies">
                  <Textarea
                    value={dependencyDraft}
                    onChange={(event) => setDependencyDraft(event.target.value)}
                    className="min-h-20 resize-none"
                  />
                </Field>
                {error && (
                  <div
                    role="alert"
                    className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                  >
                    {error}
                  </div>
                )}
                <Button className="w-full" disabled={!canCreate} onClick={() => void submitGoal()}>
                  <PlusIcon /> Create goal
                </Button>
              </div>
            </div>

            {selectedGoal ? (
              <GoalDetail
                goal={selectedGoal}
                busy={busy}
                newDependency={newDependency}
                setNewDependency={setNewDependency}
                onActivate={() => void activateGoal.mutateAsync(selectedGoal.id)}
                onStatus={(status) =>
                  void updateGoal.mutateAsync({ goalId: selectedGoal.id, payload: { status } })
                }
                onAddDependency={() => void addGoalDependency(selectedGoal)}
                onDependencyStatus={(dependencyId, status) =>
                  void updateDependency.mutateAsync({
                    goalId: selectedGoal.id,
                    dependencyId,
                    payload: { status },
                  })
                }
              />
            ) : (
              <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">
                Select a goal to inspect status, dependencies, and activation.
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
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

function ViewButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: string;
  onClick: () => void;
}) {
  return (
    <Button variant={active ? "secondary" : "ghost"} size="sm" onClick={onClick}>
      {children}
    </Button>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function GoalRow({
  goal,
  selected,
  onSelect,
}: {
  goal: GoalRecord;
  selected: boolean;
  onSelect: () => void;
}) {
  const pending = pendingDependencyCount(goal);
  return (
    <button
      type="button"
      className={cn(
        "mb-2 grid min-h-24 w-full cursor-pointer grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-md border p-3 text-left transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50",
        selected ? "border-border bg-muted" : "border-border/70 bg-background hover:bg-muted/50",
      )}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="min-w-0">
        <span className="mb-2 flex min-w-0 items-center gap-2">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40">
            {iconForScope(goal.target_kind)}
          </span>
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold">{goal.title}</span>
            <span className="block truncate text-xs text-muted-foreground">
              {displayTarget(goal)}
            </span>
          </span>
        </span>
        <span className="flex flex-wrap gap-1.5">
          <Badge variant="outline">
            <FlagIcon /> P{goal.priority}
          </Badge>
          <Badge variant={goal.activation_state === "ready" ? "secondary" : "outline"}>
            {activationIcon(goal)}
            {activationLabel(goal.activation_state)}
          </Badge>
          <Badge variant={goal.status === "blocked" ? "destructive" : "outline"}>
            {statusLabel(goal.status)}
          </Badge>
          {pending > 0 && <Badge variant="outline">{pending} pending</Badge>}
        </span>
      </span>
      <span className="text-right text-xs text-muted-foreground">
        <span className="block">{readinessLabel(goal.readiness_kind)}</span>
        <span className="block tabular-nums">{formattedTime(goal.updated_at)}</span>
      </span>
    </button>
  );
}

function GoalDetail({
  goal,
  busy,
  newDependency,
  setNewDependency,
  onActivate,
  onStatus,
  onAddDependency,
  onDependencyStatus,
}: {
  goal: GoalRecord;
  busy: boolean;
  newDependency: string;
  setNewDependency: (value: string) => void;
  onActivate: () => void;
  onStatus: (status: GoalStatus) => void;
  onAddDependency: () => void;
  onDependencyStatus: (dependencyId: string, status: "satisfied" | "waived") => void;
}) {
  return (
    <div className="rounded-md border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">{goal.title}</h2>
            <p className="truncate text-xs text-muted-foreground">{displayTarget(goal)}</p>
          </div>
          <Badge variant={goal.activation_state === "ready" ? "secondary" : "outline"}>
            {activationLabel(goal.activation_state)}
          </Badge>
        </div>
      </div>
      <div className="space-y-3 p-3">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <InfoCell label="Status" value={statusLabel(goal.status)} />
          <InfoCell label="Readiness" value={readinessLabel(goal.readiness_kind)} />
          <InfoCell label="Priority" value={`P${goal.priority}`} />
          <InfoCell label="Updated" value={formattedTime(goal.updated_at)} />
        </div>

        <Field label="Lifecycle">
          <Select
            value={goal.status}
            onValueChange={(value) => onStatus(value as GoalStatus)}
            disabled={busy || goal.status === "done"}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((status) => (
                <SelectItem key={status} value={status}>
                  {statusLabel(status)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        {goal.activation_state !== "ready" && (
          <Button variant="outline" className="w-full" disabled={busy} onClick={onActivate}>
            <CheckCircle2Icon /> Activate now
          </Button>
        )}

        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <ListChecksIcon className="size-3.5" />
            Dependencies
          </div>
          {goal.dependencies.length === 0 && (
            <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
              No dependencies.
            </div>
          )}
          {goal.dependencies.map((dependency) => (
            <div key={dependency.id} className="rounded-md border border-border p-2">
              <div className="mb-2 flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="break-words text-sm">{dependency.label}</p>
                  <p className="text-xs text-muted-foreground">{dependency.kind}</p>
                </div>
                <Badge variant={dependency.status === "pending" ? "outline" : "secondary"}>
                  {dependency.status}
                </Badge>
              </div>
              {dependency.status === "pending" && (
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => onDependencyStatus(dependency.id, "satisfied")}
                  >
                    <CheckIcon /> Satisfy
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => onDependencyStatus(dependency.id, "waived")}
                  >
                    Waive
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="flex gap-2">
          <Input
            value={newDependency}
            onChange={(event) => setNewDependency(event.target.value)}
            aria-label="New dependency"
          />
          <Button
            variant="outline"
            size="icon"
            aria-label="Add dependency"
            disabled={busy || newDependency.trim().length === 0}
            onClick={onAddDependency}
          >
            <PlusIcon />
          </Button>
        </div>
      </div>
    </div>
  );
}

function InfoCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-muted/30 px-2 py-1.5">
      <div className="truncate text-muted-foreground">{label}</div>
      <div className="truncate font-medium">{value}</div>
    </div>
  );
}
