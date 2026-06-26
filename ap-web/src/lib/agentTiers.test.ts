import { describe, expect, it } from "vitest";
import { groupAgentsByTier, tierForAgent, TIER_LABELS } from "./agentTiers";

describe("tierForAgent", () => {
  it("uses an explicit valid category over the workflow flag", () => {
    // category wins even when the legacy flag would say otherwise.
    expect(tierForAgent({ category: "system", workflow: true })).toBe("system");
    expect(tierForAgent({ category: "workflow", workflow: false })).toBe("workflow");
    expect(tierForAgent({ category: "employee", workflow: true })).toBe("employee");
  });

  it("falls back to the workflow flag when category is absent or invalid", () => {
    expect(tierForAgent({ workflow: true })).toBe("workflow");
    expect(tierForAgent({ workflow: false })).toBe("employee");
    // An unrecognized category string is ignored — heuristic decides.
    expect(tierForAgent({ category: "bogus", workflow: true })).toBe("workflow");
  });

  it("defaults to employee with no signal", () => {
    expect(tierForAgent({})).toBe("employee");
  });

  it("never invents system from the heuristic", () => {
    // System can only come from the explicit category; the flag can't.
    expect(tierForAgent({ workflow: false })).not.toBe("system");
    expect(tierForAgent({ workflow: true })).not.toBe("system");
  });
});

describe("groupAgentsByTier", () => {
  it("buckets by tier and preserves input order within each bucket", () => {
    const agents = [
      { id: "e1", workflow: false },
      { id: "s1", category: "system" },
      { id: "w1", workflow: true },
      { id: "e2", category: "employee" },
      { id: "s2", category: "system" },
      { id: "w2", category: "workflow" },
    ];
    const groups = groupAgentsByTier(agents);
    expect(groups.system.map((a) => a.id)).toEqual(["s1", "s2"]);
    expect(groups.employee.map((a) => a.id)).toEqual(["e1", "e2"]);
    expect(groups.workflow.map((a) => a.id)).toEqual(["w1", "w2"]);
  });

  it("returns empty buckets for an empty input", () => {
    expect(groupAgentsByTier([])).toEqual({ system: [], employee: [], workflow: [] });
  });
});

describe("TIER_LABELS", () => {
  it("labels each tier", () => {
    expect(TIER_LABELS).toEqual({ system: "System", employee: "Employees", workflow: "Workflows" });
  });
});
