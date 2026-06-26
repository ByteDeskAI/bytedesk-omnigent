import type { Edge, Node, NodeProps } from "@xyflow/react";
import { MarkerType, Position } from "@xyflow/react";
import { GitBranchIcon, WorkflowIcon } from "lucide-react";
import { memo, useMemo } from "react";
import { Canvas } from "@/components/ai-elements/canvas";
import { Controls } from "@/components/ai-elements/controls";
import { Badge } from "@/components/ui/badge";
import {
  type BlueprintGraph,
  type BlueprintGraphNode,
  type BlueprintRun,
  useAgentBlueprint,
  useSessionBlueprintRun,
} from "@/hooks/useBlueprints";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";

const NODE_WIDTH = 224;
const NODE_X_GAP = 280;
const NODE_Y_GAP = 132;

type BlueprintNodeStatus =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "skipped";

interface BlueprintFlowNodeData extends Record<string, unknown> {
  id: string;
  kind: string;
  target: string | null;
  status: BlueprintNodeStatus;
  childSessionId: string | null;
  loopIterations: number;
}

type BlueprintFlowNode = Node<BlueprintFlowNodeData, "blueprint">;
type BlueprintFlowEdge = Edge<Record<string, never>, "default">;

const STATUS_CLASS: Record<BlueprintNodeStatus, string> = {
  pending: "border-border bg-card text-muted-foreground",
  running: "border-primary/50 bg-primary/5 text-primary",
  waiting: "border-warning/50 bg-warning/10 text-warning",
  completed: "border-success/45 bg-success/10 text-success",
  failed: "border-destructive/50 bg-destructive/10 text-destructive",
  skipped: "border-border bg-muted/60 text-muted-foreground",
};

const STATUS_DOT_CLASS: Record<BlueprintNodeStatus, string> = {
  pending: "bg-muted-foreground/45",
  running: "bg-primary",
  waiting: "bg-warning",
  completed: "bg-success",
  failed: "bg-destructive",
  skipped: "bg-muted-foreground/35",
};

const nodeTypes = {
  blueprint: memo(BlueprintGraphNodeCard),
};

interface BlueprintPanelProps {
  conversationId: string;
  agentId: string | null;
}

export function BlueprintPanel({ conversationId, agentId }: BlueprintPanelProps) {
  const graphQuery = useAgentBlueprint(agentId);
  const graph = graphQuery.data ?? null;
  const runQuery = useSessionBlueprintRun(graph ? conversationId : null, 2_500);
  const run = runQuery.data ?? null;
  const { nodes, edges } = useMemo(() => buildBlueprintFlowElements(graph, run), [graph, run]);

  if (graphQuery.isLoading) {
    return (
      <div className="flex h-full flex-1 items-center justify-center bg-card px-4 py-8 text-center text-muted-foreground text-xs">
        Loading…
      </div>
    );
  }

  if (graphQuery.error) {
    return (
      <div className="flex h-full flex-1 items-center justify-center bg-card px-4 py-8 text-center text-muted-foreground text-xs">
        Failed to load blueprint.
      </div>
    );
  }

  if (!graph) {
    return (
      <div className="flex h-full flex-1 items-center justify-center bg-card px-4 py-8 text-center text-muted-foreground text-xs">
        No blueprint graph.
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card" data-testid="blueprint-panel">
      <div className="flex shrink-0 items-center justify-between gap-2 border-border border-b px-3 py-2">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <WorkflowIcon className="size-4 shrink-0 text-muted-foreground" />
            <h2 className="truncate font-medium text-sm">{graph.name ?? graph.agent_name ?? "Blueprint"}</h2>
          </div>
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
            {graph.nodes.length} nodes · v{graph.version}
          </div>
        </div>
        <Badge variant="secondary" className={cn("capitalize", runStatusClass(run?.status))}>
          {run?.status ?? "pending"}
        </Badge>
      </div>
      <div className="min-h-0 flex-1">
        <Canvas
          className="h-full w-full bg-card"
          edges={edges}
          fitViewOptions={{ padding: 0.22 }}
          maxZoom={1.25}
          minZoom={0.25}
          nodeTypes={nodeTypes}
          nodes={nodes}
          nodesDraggable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
        >
          <Controls showInteractive={false} />
        </Canvas>
      </div>
      {run?.loop_iterations.length ? (
        <div className="max-h-28 shrink-0 overflow-y-auto border-border border-t px-3 py-2">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
            <GitBranchIcon className="size-3.5" />
            Loop Attempts
          </div>
          <div className="space-y-1">
            {run.loop_iterations.slice(-6).map((item, idx) => (
              <LoopAttemptRow key={`${item.node_id ?? "loop"}-${item.loop_iteration ?? idx}`} item={item} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function buildBlueprintFlowElements(
  graph: BlueprintGraph | null,
  run: BlueprintRun | null,
): { nodes: BlueprintFlowNode[]; edges: BlueprintFlowEdge[] } {
  if (!graph) return { nodes: [], edges: [] };

  const runNodes = new Map(run?.nodes.map((node) => [node.id, node]) ?? []);
  const graphNodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const depthMemo = new Map<string, number>();

  function depthFor(node: BlueprintGraphNode): number {
    const cached = depthMemo.get(node.id);
    if (cached !== undefined) return cached;
    const depDepths = node.depends_on
      .map((dep) => graphNodes.get(dep))
      .filter((dep): dep is BlueprintGraphNode => dep !== undefined)
      .map((dep) => depthFor(dep) + 1);
    const depth = depDepths.length > 0 ? Math.max(...depDepths) : 0;
    depthMemo.set(node.id, depth);
    return depth;
  }

  const lanesByDepth = new Map<number, number>();
  const nodes = graph.nodes.map((node) => {
    const depth = depthFor(node);
    const lane = lanesByDepth.get(depth) ?? 0;
    lanesByDepth.set(depth, lane + 1);
    const live = runNodes.get(node.id);
    const status = normalizeStatus(live?.status);
    return {
      id: node.id,
      type: "blueprint",
      position: { x: depth * NODE_X_GAP, y: lane * NODE_Y_GAP },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      data: {
        id: node.id,
        kind: live?.kind ?? node.kind,
        target: node.target ?? null,
        status,
        childSessionId: live?.child_session_id ?? null,
        loopIterations: node.loop?.max_iterations ?? 0,
      },
      style: { width: NODE_WIDTH },
    } satisfies BlueprintFlowNode;
  });

  const edges = graph.edges.map((edge) => {
    const sourceStatus = normalizeStatus(runNodes.get(edge.source)?.status);
    const targetStatus = normalizeStatus(runNodes.get(edge.target)?.status);
    const active = sourceStatus === "running" || targetStatus === "running" || targetStatus === "waiting";
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      animated: active,
      style: {
        stroke: active ? "var(--primary)" : "var(--border-strong)",
        strokeWidth: active ? 1.5 : 1,
      },
    } satisfies BlueprintFlowEdge;
  });

  return { nodes, edges };
}

function BlueprintGraphNodeCard({ data }: NodeProps<BlueprintFlowNode>) {
  return (
    <div
      className={cn(
        "rounded-md border px-3 py-2 shadow-sm backdrop-blur-sm",
        "min-h-[88px] overflow-hidden",
        STATUS_CLASS[data.status],
      )}
      data-testid={`blueprint-node-${data.id}`}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn("size-2 shrink-0 rounded-full", STATUS_DOT_CLASS[data.status])} />
        <span className="min-w-0 truncate font-medium text-[13px] text-foreground">{data.id}</span>
      </div>
      <div className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px]">
        <span className="rounded-sm bg-muted px-1.5 py-0.5 text-muted-foreground">{data.kind}</span>
        {data.loopIterations > 0 && (
          <span className="rounded-sm bg-muted px-1.5 py-0.5 text-muted-foreground">
            {data.loopIterations}x
          </span>
        )}
      </div>
      {data.target && (
        <div className="mt-1 truncate text-[11px] text-muted-foreground" title={data.target}>
          {data.target}
        </div>
      )}
      {data.childSessionId && (
        <Link
          className="nodrag mt-2 inline-flex max-w-full truncate rounded-sm bg-background px-1.5 py-0.5 text-[11px] text-primary hover:underline"
          to={`/c/${data.childSessionId}`}
        >
          {data.childSessionId}
        </Link>
      )}
    </div>
  );
}

function LoopAttemptRow({ item }: { item: Record<string, unknown> }) {
  const nodeId = typeof item.node_id === "string" ? item.node_id : "loop";
  const iteration =
    typeof item.loop_iteration === "number" || typeof item.loop_iteration === "string"
      ? item.loop_iteration
      : "?";
  const status = typeof item.status === "string" ? item.status : "running";
  return (
    <div className="flex min-w-0 items-center justify-between gap-2 text-[11px]">
      <span className="min-w-0 truncate text-muted-foreground">
        {nodeId} #{iteration}
      </span>
      <span className={cn("shrink-0 capitalize", runStatusClass(status))}>{status}</span>
    </div>
  );
}

function normalizeStatus(status: string | null | undefined): BlueprintNodeStatus {
  if (
    status === "running" ||
    status === "waiting" ||
    status === "completed" ||
    status === "failed" ||
    status === "skipped"
  ) {
    return status;
  }
  return "pending";
}

function runStatusClass(status: string | null | undefined): string {
  if (status === "completed") return "text-success";
  if (status === "failed") return "text-destructive";
  if (status === "running") return "text-primary";
  if (status === "waiting") return "text-warning";
  return "text-muted-foreground";
}
