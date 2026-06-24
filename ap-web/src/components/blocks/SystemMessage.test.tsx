import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SystemMessageView } from "./SystemMessage";

afterEach(cleanup);

describe("SystemMessageView", () => {
  it("hides sub-agent wake notices instead of rendering a centered System row", () => {
    const { container } = render(
      <SystemMessageView
        message={{
          kind: "subagent_wake",
          label: "Sub-agent result ready",
          body: "",
        }}
      />,
    );

    expect(screen.queryByTestId("system-message")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("renders a static row when the body is empty", () => {
    render(
      <SystemMessageView
        message={{ kind: "task_completed", label: "Task done", body: "   " }}
      />,
    );
    const row = screen.getByTestId("system-message");
    expect(row.textContent).toContain("System:");
    expect(row.textContent).toContain("Task done");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("expands collapsible body on click for messages with content", () => {
    render(
      <SystemMessageView
        message={{
          kind: "task_failed",
          label: "Task failed",
          body: "traceback line 1",
        }}
      />,
    );
    const toggle = screen.getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("traceback line 1")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("traceback line 1")).toBeDefined();
  });
});
