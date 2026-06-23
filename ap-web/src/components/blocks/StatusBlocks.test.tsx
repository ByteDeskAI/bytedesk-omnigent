import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  CompactionMarker,
  ErrorBanner,
  PolicyDeniedBanner,
  RetryIndicator,
} from "./StatusBlocks";

afterEach(cleanup);

describe("ErrorBanner", () => {
  it("renders message with source and code when all fields are present", () => {
    render(<ErrorBanner message="boom" source="runner" code="E42" />);
    expect(screen.getByText(/Error · runner · E42/)).toBeDefined();
    expect(screen.getByText("boom")).toBeDefined();
  });

  it("falls back to code when message is empty", () => {
    render(<ErrorBanner message="" source="api" code="TIMEOUT" />);
    expect(screen.getByText("TIMEOUT")).toBeDefined();
    expect(screen.getByText(/Error · api/)).toBeDefined();
    expect(screen.queryByText(/TIMEOUT/)).toBeDefined();
  });

  it("falls back to Unknown error when message and code are empty", () => {
    render(<ErrorBanner message="" source="" code="" />);
    expect(screen.getByText("Unknown error")).toBeDefined();
    expect(screen.getByText("Error")).toBeDefined();
  });
});

describe("PolicyDeniedBanner", () => {
  it("renders reason with phase label", () => {
    render(<PolicyDeniedBanner reason="not allowed" phase="tool_call" />);
    expect(screen.getByText(/Blocked by policy · tool_call/)).toBeDefined();
    expect(screen.getByText("not allowed")).toBeDefined();
  });

  it("omits phase suffix when phase is empty", () => {
    render(<PolicyDeniedBanner reason="denied" phase="" />);
    expect(screen.getByText("Blocked by policy")).toBeDefined();
    expect(screen.getByText("denied")).toBeDefined();
  });
});

describe("RetryIndicator", () => {
  it("shows delay when delaySeconds is positive", () => {
    render(
      <RetryIndicator source="llm" attempt={2} maxAttempts={5} delaySeconds={1.5} />,
    );
    expect(screen.getByText(/Retrying llm · attempt 2\/5 · waiting 1\.5s/)).toBeDefined();
  });

  it("omits delay suffix when delaySeconds is zero", () => {
    render(
      <RetryIndicator source="mcp" attempt={1} maxAttempts={3} delaySeconds={0} />,
    );
    expect(screen.getByText(/Retrying mcp · attempt 1\/3/)).toBeDefined();
    expect(screen.queryByText(/waiting/)).toBeNull();
  });
});

describe("CompactionMarker", () => {
  it("renders the compacted conversation marker", () => {
    render(<CompactionMarker />);
    expect(screen.getByText("Conversation compacted")).toBeDefined();
  });
});