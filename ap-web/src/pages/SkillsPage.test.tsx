import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillsPage } from "./SkillsPage";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import * as skillsHooks from "@/hooks/useSkills";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useSkills", () => ({
  useSkillSources: vi.fn(),
  useInstalledSkills: vi.fn(),
  useSearchSkills: vi.fn(),
  useCreateSkillPreview: vi.fn(),
  useApplySkillPreview: vi.fn(),
}));

const searchMutate = vi.fn();
const previewMutate = vi.fn();
const applyMutate = vi.fn();
const refetchInstalled = vi.fn();

const PREVIEW = {
  id: "skprev_1",
  operation: "install",
  install_mode: "replace",
  created_at: 1,
  expires_at: 2,
  skills: [
    {
      name: "image-tools",
      description: "Work with images.",
      total_bytes: 42,
      files: [
        {
          path: "skills/image-tools/SKILL.md",
          size: 40,
          sha256: "abc",
          binary: false,
        },
      ],
    },
  ],
  target_actions: [
    {
      agent_id: "ag_1",
      agent_name: "demo",
      agent_version: 1,
      skill_name: "image-tools",
      action: "install",
      reason: null,
    },
  ],
  command: null,
  skill_names: ["image-tools"],
};

function renderPage() {
  return render(<SkillsPage />);
}

beforeEach(() => {
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      {
        id: "ag_1",
        name: "demo",
        display_name: "Demo",
        description: null,
        harness: "codex",
        skills: [],
      },
      {
        id: "ag_2",
        name: "helper",
        display_name: "Helper",
        description: null,
        harness: "claude-sdk",
        skills: [],
      },
    ],
  } as never);
  vi.mocked(skillsHooks.useSkillSources).mockReturnValue({
    data: [
      {
        id: "skills",
        label: "Agent Skills CLI",
        kind: "named_adapter",
        supports_search: true,
        supports_preview: true,
        high_risk: false,
      },
      {
        id: "freeform",
        label: "Free-form Command",
        kind: "freeform_command",
        supports_search: false,
        supports_preview: true,
        high_risk: true,
      },
    ],
  } as never);
  vi.mocked(skillsHooks.useInstalledSkills).mockReturnValue({
    data: [
      {
        name: "deep-search",
        description: "Search stuff.",
        agents: [{ id: "ag_1", name: "demo", version: 1 }],
      },
    ],
    isLoading: false,
    refetch: refetchInstalled,
  } as never);
  vi.mocked(skillsHooks.useSearchSkills).mockReturnValue({
    mutate: searchMutate,
    isPending: false,
    data: {
      data: [
        {
          source: "skills",
          name: "image-tools",
          description: "Work with images.",
          source_ref: "github:org/repo",
          version: null,
          url: null,
        },
      ],
      errors: [],
    },
  } as never);
  previewMutate.mockImplementation(
    (_payload, opts?: { onSuccess?: (preview: typeof PREVIEW) => void }) =>
      opts?.onSuccess?.(PREVIEW),
  );
  vi.mocked(skillsHooks.useCreateSkillPreview).mockReturnValue({
    mutate: previewMutate,
    isPending: false,
    error: null,
  } as never);
  applyMutate.mockImplementation((_payload, opts?: { onSuccess?: () => void }) =>
    opts?.onSuccess?.(),
  );
  vi.mocked(skillsHooks.useApplySkillPreview).mockReturnValue({
    mutate: applyMutate,
    isPending: false,
    error: null,
    data: { data: [] },
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SkillsPage", () => {
  it("renders installed skills and selected template agents", async () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "Skills" })).toBeInTheDocument();
    expect(screen.getByText("deep-search")).toBeInTheDocument();
    expect(await screen.findByLabelText(/Demo/)).toBeChecked();
    expect(screen.getByLabelText(/Helper/)).toBeChecked();
  });

  it("searches, previews, and applies a selected result", async () => {
    renderPage();

    fireEvent.change(screen.getByPlaceholderText("Search skills"), {
      target: { value: "image" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Search/ }));

    expect(searchMutate).toHaveBeenCalledWith({
      query: "image",
      sources: ["skills"],
      limit: 20,
      command: undefined,
    });

    fireEvent.click(screen.getByRole("button", { name: /image-tools/ }));
    fireEvent.click(screen.getByRole("button", { name: /Preview install/ }));

    await waitFor(() => expect(screen.getByText("Agent actions")).toBeInTheDocument());
    expect(previewMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        operation: "install",
        target_agent_ids: ["ag_1", "ag_2"],
        source: "skills",
        source_ref: "github:org/repo",
      }),
      expect.any(Object),
    );

    fireEvent.click(screen.getByRole("button", { name: "Apply preview" }));
    expect(applyMutate).toHaveBeenCalledWith({ previewId: "skprev_1" }, expect.any(Object));
    expect(refetchInstalled).toHaveBeenCalled();
  });

  it("stages a removal preview from the installed list", () => {
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: /Remove deep-search/ }));

    expect(previewMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        operation: "remove",
        target_agent_ids: ["ag_1", "ag_2"],
        skill_names: ["deep-search"],
      }),
      expect.any(Object),
    );
  });
});
