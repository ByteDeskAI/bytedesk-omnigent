import { describe, expect, it } from "vitest";

import { routeSection } from "./PageTransitionOutlet";

describe("routeSection", () => {
  it("collapses both chat routes to one section so conversation switches don't re-animate", () => {
    expect(routeSection("/")).toBe("chat");
    expect(routeSection("/c/abc-123")).toBe("chat");
    // Same section for both → keyed wrapper does not remount between them.
    expect(routeSection("/")).toBe(routeSection("/c/whatever"));
  });

  it("gives each top-level admin route its own section", () => {
    expect(routeSection("/inbox")).toBe("inbox");
    expect(routeSection("/skills")).toBe("skills");
    expect(routeSection("/goals")).toBe("goals");
    expect(routeSection("/schedules")).toBe("schedules");
    expect(routeSection("/members")).toBe("members");
    expect(routeSection("/policies")).toBe("policies");
    expect(routeSection("/config")).toBe("config");
  });

  it("ignores deeper segments — a section is the first path segment only", () => {
    expect(routeSection("/skills/some/deep/path")).toBe("skills");
    expect(routeSection("/members/")).toBe("members");
  });
});
