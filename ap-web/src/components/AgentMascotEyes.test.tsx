import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentMascotEyes } from "./AgentMascotEyes";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AgentMascotEyes", () => {
  it("renders the mascot with image semantics by default", () => {
    const { container } = render(<AgentMascotEyes className="h-18" />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "Omnigent agent");
    expect(svg).toHaveAttribute("aria-hidden", "false");
    expect(svg).toHaveClass("h-18");
  });

  it("can render decoratively for working indicators", () => {
    const { container } = render(<AgentMascotEyes decorative className="h-4" />);
    const svg = container.querySelector("svg");
    expect(svg).not.toHaveAttribute("role");
    expect(svg).not.toHaveAttribute("aria-label");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("slides both pupils toward the pointer", async () => {
    const { container } = render(<AgentMascotEyes />);
    const svg = container.querySelector("svg");
    if (!svg) throw new Error("AgentMascotEyes did not render an svg");
    vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 1000, clientY: 54.95 }));
    await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));

    const pupils = Array.from(container.querySelectorAll<SVGGElement>("g.otto-pupil"));
    expect(pupils).toHaveLength(2);
    for (const pupil of pupils) {
      const match = pupil.style.transform.match(/^translate\((-?[\d.]+)px, (-?[\d.]+)px\)$/) ?? [];
      expect(match).toHaveLength(3);
      expect(Number(match[1])).toBeCloseTo(13, 1);
      expect(Math.abs(Number(match[2]))).toBeLessThan(0.1);
    }
  });
});
