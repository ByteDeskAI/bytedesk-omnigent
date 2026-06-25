import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { OfflineBanner } from "./OfflineBanner";

vi.mock("@/hooks/useOffline", () => ({
  useOffline: vi.fn(),
}));

import { useOffline } from "@/hooks/useOffline";

describe("OfflineBanner", () => {
  it("renders when offline", () => {
    vi.mocked(useOffline).mockReturnValue(true);
    render(<OfflineBanner />);
    expect(screen.getByTestId("offline-banner")).toBeInTheDocument();
  });

  it("hides when online", () => {
    vi.mocked(useOffline).mockReturnValue(false);
    render(<OfflineBanner />);
    expect(screen.queryByTestId("offline-banner")).toBeNull();
  });
});