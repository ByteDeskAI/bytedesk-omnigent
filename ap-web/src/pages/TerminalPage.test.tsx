import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TerminalPage } from "./TerminalPage";
import type { CurrentAccount } from "@/lib/accountsApi";
import type { ServerInfo } from "@/lib/capabilities";
import * as accountsApi from "@/lib/accountsApi";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import * as terminalApi from "@/lib/omniCliTerminalApi";

vi.mock("@/lib/accountsApi", () => ({ getMe: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));
vi.mock("@/lib/omniCliTerminalApi", () => ({ getOmniCliTerminalStatus: vi.fn() }));
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({ attachPath }: { attachPath?: string }) => (
    <div data-testid="terminal-view">{attachPath}</div>
  ),
}));

function account(overrides: Partial<CurrentAccount> = {}): CurrentAccount {
  return { id: "alice", is_admin: true, created_at: null, last_login_at: null, ...overrides };
}

const ACCOUNTS_ON: ServerInfo = {
  accounts_enabled: true,
  login_url: "/login",
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  omni_cli_terminal_enabled: true,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <TerminalPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(useServerInfo).mockReturnValue(ACCOUNTS_ON);
  vi.mocked(accountsApi.getMe).mockResolvedValue(account());
  vi.mocked(terminalApi.getOmniCliTerminalStatus).mockResolvedValue({
    enabled: true,
    namespace: "bytedesk",
    pod_name: "omnigent-cli-0",
    container: "cli",
    phase: "Running",
    server_url: "http://omnigent-server.bytedesk.svc.cluster.local",
    attach_path: "/v1/admin/omni-cli/terminal/attach",
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TerminalPage", () => {
  it("renders the admin terminal with the server-provided attach path", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Terminal" })).toBeInTheDocument();
    expect(await screen.findByTestId("terminal-view")).toHaveTextContent(
      "/v1/admin/omni-cli/terminal/attach",
    );
  });

  it("does not mount the terminal for a non-admin", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue(account({ is_admin: false }));

    renderPage();

    expect(await screen.findByText(/don't have permission/i)).toBeInTheDocument();
    await waitFor(() => expect(terminalApi.getOmniCliTerminalStatus).not.toHaveBeenCalled());
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("does not call /auth/me when accounts mode is off", async () => {
    vi.mocked(useServerInfo).mockReturnValue({
      ...ACCOUNTS_ON,
      accounts_enabled: false,
      login_url: null,
    });

    renderPage();

    expect(await screen.findByRole("heading", { name: "Terminal" })).toBeInTheDocument();
    expect(accountsApi.getMe).not.toHaveBeenCalled();
    expect(await screen.findByTestId("terminal-view")).toHaveTextContent(
      "/v1/admin/omni-cli/terminal/attach",
    );
  });
});
