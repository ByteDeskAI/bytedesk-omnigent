import { describe, expect, it } from "vitest";
import type { BlueprintGraph, BlueprintRun } from "@/hooks/useBlueprints";
import { buildBlueprintFlowElements } from "./BlueprintPanel";

const graph: BlueprintGraph = {
  object: "blueprint",
  agent_id: "ag_blueprint",
  agent_name: "blueprint-playground",
  name: "Playground",
  description: null,
  version: 1,
  nodes: [
    {
      id: "collect",
      kind: "blueprint",
      depends_on: [],
      target: "demo-team-idea-collection",
      metadata: {},
      loop: null,
    },
    {
      id: "draft",
      kind: "blueprint",
      depends_on: ["collect"],
      target: "demo-motto-drafting",
      metadata: {},
      loop: null,
    },
    {
      id: "review",
      kind: "loop",
      depends_on: ["draft"],
      metadata: {},
      loop: {
        max_iterations: 2,
        until: { path: "$.nodes.approve.output.approved", equals: true },
        on_exhausted: "fail",
        reuse_session: true,
        nodes: [],
        edges: [],
      },
    },
  ],
  edges: [
    { id: "collect->draft", source: "collect", target: "draft" },
    { id: "draft->review", source: "draft", target: "review" },
  ],
  outputs: {},
};

const run: BlueprintRun = {
  object: "blueprint_run",
  blueprint_run_id: "bpr_123",
  status: "running",
  nodes: [
    {
      id: "collect",
      kind: "blueprint",
      status: "completed",
      child_session_id: "conv_child",
      payload: {},
      loop_iteration: null,
      updated_at: 100,
    },
    {
      id: "draft",
      kind: "blueprint",
      status: "running",
      child_session_id: null,
      payload: {},
      loop_iteration: null,
      updated_at: 101,
    },
  ],
  loop_iterations: [],
  events: [],
};

describe("buildBlueprintFlowElements", () => {
  it("lays out dependency depth and overlays live node metadata", () => {
    const { nodes, edges } = buildBlueprintFlowElements(graph, run);

    const collect = nodes.find((node) => node.id === "collect");
    const draft = nodes.find((node) => node.id === "draft");
    const review = nodes.find((node) => node.id === "review");
    expect(collect?.position.x).toBe(0);
    expect(draft?.position.x).toBeGreaterThan(collect?.position.x ?? -1);
    expect(review?.position.x).toBeGreaterThan(draft?.position.x ?? -1);
    expect(collect?.data.status).toBe("completed");
    expect(collect?.data.childSessionId).toBe("conv_child");
    expect(review?.data.loopIterations).toBe(2);
    expect(edges.find((edge) => edge.id === "collect->draft")?.animated).toBe(true);
  });
});
