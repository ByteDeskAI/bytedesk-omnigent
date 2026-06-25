import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AgentMascotIcon } from "./AgentMascotIcon";

afterEach(cleanup);

describe("AgentMascotIcon", () => {
  it("renders the generated mascot image with two blinkable eyes", () => {
    const { container } = render(<AgentMascotIcon />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("viewBox", "0 0 1023 1070");
    expect(container.querySelector("svg > image")).toBeTruthy();

    const eyes = container.querySelectorAll("svg > g.otto-eye");
    expect(eyes).toHaveLength(2);
    for (const eye of eyes) {
      expect(eye.querySelector("ellipse")).toBeTruthy();
    }
  });

  it("wraps each live pupil in an otto-pupil group for cursor tracking", () => {
    const { container } = render(<AgentMascotIcon />);
    const pupils = container.querySelectorAll("svg g.otto-eye > g.otto-pupil");
    expect(pupils).toHaveLength(2);
    for (const pupil of pupils) {
      expect(pupil.querySelectorAll("circle")).toHaveLength(2);
      expect(pupil.querySelectorAll("path")).toHaveLength(1);
    }
  });

  it("spreads props onto the root svg and stays hidden from screen readers by default", () => {
    const { container } = render(<AgentMascotIcon className="otto-working h-4" />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveClass("otto-working");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("lets callers override image semantics", () => {
    const { container } = render(
      <AgentMascotIcon role="img" aria-label="Omnigent agent" aria-hidden={false} />,
    );
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label", "Omnigent agent");
    expect(svg).toHaveAttribute("aria-hidden", "false");
  });
});
