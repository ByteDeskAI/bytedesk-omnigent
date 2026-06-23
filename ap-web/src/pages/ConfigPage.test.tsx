// Tests for the read-only Configuration admin page (ADR-0150, BDP-2416).
//
// Browser e2e is admin/accounts-gated, so the surface is pinned here by
// mocking getMe (admin gate), useNavigate (unauth bounce), and the read-side
// config hooks (useConfigDescriptors / useConfigValue) so no QueryClient or
// network is needed. isSecretValue is kept real so the secret-render branch
// is exercised end-to-end.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConfigPage } from "./ConfigPage";
import * as accountsApi from "@/lib/accountsApi";
import * as configHooks from "@/hooks/useConfigDescriptors";
import type { ConfigDescriptor } from "@/hooks/useConfigDescriptors";

const navigateMock = vi.fn();

vi.mock("@/lib/routing", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/routing")>()),
  useNavigate: () => navigateMock,
}));
vi.mock("@/lib/accountsApi", () => ({ getMe: vi.fn() }));
vi.mock("@/hooks/useConfigDescriptors", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/useConfigDescriptors")>()),
  useConfigDescriptors: vi.fn(),
  useConfigValue: vi.fn(),
}));

function descriptor(overrides: Partial<ConfigDescriptor> = {}): ConfigDescriptor {
  return {
    key: "system.log_level",
    scope: "system",
    what: "Process log level.",
    json_schema: { type: "string" },
    tier: 2,
    sensitivity: "public",
    effect_timing: "live",
    storage_source: "env",
    floor: null,
    change_event: null,
    writable: true,
    read_only_reason: null,
    ...overrides,
  };
}

function setDescriptors(list: ConfigDescriptor[]) {
  vi.mocked(configHooks.useConfigDescriptors).mockReturnValue({
    data: list,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  } as never);
}

/** Map each key to its value-query result. */
function setValues(byKey: Record<string, unknown>) {
  vi.mocked(configHooks.useConfigValue).mockImplementation(
    (key: string) =>
      ({
        data: { key, value: byKey[key], etag: "1", source: "env", writable: true },
        isLoading: false,
        isError: false,
        error: null,
      }) as never,
  );
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ConfigPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(accountsApi.getMe).mockResolvedValue({
    id: "admin",
    is_admin: true,
    created_at: null,
    last_login_at: null,
  });
  setDescriptors([]);
  setValues({});
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ConfigPage gating", () => {
  it("shows a loading state until the identity probe resolves", () => {
    vi.mocked(accountsApi.getMe).mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("blocks non-admins with a permission message", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue({
      id: "alice",
      is_admin: false,
      created_at: null,
      last_login_at: null,
    });
    renderPage();
    expect(
      await screen.findByText("You don't have permission to view system configuration."),
    ).toBeInTheDocument();
  });

  it("bounces an unauthenticated visitor to /login", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue(null);
    renderPage();
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/login", { replace: true }));
  });
});

describe("ConfigPage render", () => {
  it("groups descriptors by scope and renders each key", async () => {
    setDescriptors([
      descriptor({ key: "system.log_level", scope: "system" }),
      descriptor({ key: "policies.cost_hard_stop.default_ceiling_usd", scope: "policy" }),
    ]);
    setValues({ "system.log_level": "INFO" });
    renderPage();

    expect(await screen.findByText("system.log_level")).toBeInTheDocument();
    expect(
      screen.getByText("policies.cost_hard_stop.default_ceiling_usd"),
    ).toBeInTheDocument();
    // scope section headers (with counts)
    expect(screen.getByText("system · 1")).toBeInTheDocument();
    expect(screen.getByText("policy · 1")).toBeInTheDocument();
    // a plain value renders
    expect(screen.getByText("INFO")).toBeInTheDocument();
  });

  it("shows a Tier-0 locked key with its read-only reason", async () => {
    setDescriptors([
      descriptor({
        key: "system.nats.url",
        scope: "system",
        tier: 0,
        writable: false,
        read_only_reason: "Deploy-only: changing the bus URL mid-flight orphans state.",
      }),
    ]);
    setValues({ "system.nats.url": "nats://localhost:4222" });
    renderPage();

    expect(await screen.findByText("system.nats.url")).toBeInTheDocument();
    expect(screen.getByText("read-only")).toBeInTheDocument();
    expect(screen.getByText("Tier 0 · Locked")).toBeInTheDocument();
    expect(
      screen.getByText("Deploy-only: changing the bus URL mid-flight orphans state."),
    ).toBeInTheDocument();
  });

  it("shows a secret as name+presence only, never the value", async () => {
    setDescriptors([
      descriptor({
        key: "system.database.uri",
        scope: "system",
        tier: 0,
        sensitivity: "secret",
        writable: false,
        read_only_reason: "Secret, deploy-only.",
      }),
    ]);
    setValues({
      "system.database.uri": { name: "system.database.uri", present: true, source: "env" },
    });
    renderPage();

    expect(await screen.findByText("system.database.uri")).toBeInTheDocument();
    expect(screen.getByText("secret")).toBeInTheDocument(); // the badge
    expect(screen.getByText(/secret · present/)).toBeInTheDocument(); // the value cell
    // The actual connection string must never reach the DOM.
    expect(screen.queryByText(/postgres:\/\//)).not.toBeInTheDocument();
  });
});
