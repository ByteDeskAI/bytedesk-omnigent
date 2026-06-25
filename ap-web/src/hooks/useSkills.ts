import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface SkillSource {
  id: string;
  label: string;
  kind: string;
  supports_search: boolean;
  supports_preview: boolean;
  high_risk: boolean;
  available?: boolean;
  unavailable_reason?: string | null;
}

export interface InstalledSkillAgent {
  id: string;
  name: string;
  version: number;
}

export interface InstalledSkill {
  name: string;
  description: string;
  agents: InstalledSkillAgent[];
}

export interface SkillSearchResult {
  source: string;
  name: string;
  description: string | null;
  source_ref: string | null;
  version: string | null;
  url: string | null;
}

export interface SkillFileManifest {
  path: string;
  size: number;
  sha256: string;
  binary: boolean;
}

export interface StagedSkill {
  name: string;
  description: string;
  total_bytes: number;
  files: SkillFileManifest[];
}

export interface SkillTargetAction {
  agent_id: string;
  agent_name: string;
  agent_version: number;
  skill_name: string;
  action: string;
  reason: string | null;
}

export interface SkillCommandBody {
  argv?: string[];
  shell?: string;
  timeout_seconds?: number;
}

export interface SkillPreview {
  id: string;
  operation: "install" | "remove";
  install_mode: "replace" | "skip_existing" | "fail_on_existing";
  created_at: number;
  expires_at: number;
  skills: StagedSkill[];
  target_actions: SkillTargetAction[];
  command: {
    command: string[] | string;
    shell: boolean;
    exit_code: number;
    duration_ms: number;
    stdout: string;
    stderr: string;
  } | null;
  skill_names: string[];
}

export interface SkillApplyResult {
  agent_id: string;
  status: "applied" | "skipped" | "failed";
  action_count: number;
  version: number | null;
  error: string | null;
}

export interface SearchSkillsPayload {
  query: string;
  sources?: string[];
  limit?: number;
  command?: SkillCommandBody;
}

export interface SkillMarketplace {
  id: string;
  label: string;
  source_id: string;
  kind: string;
  description: string | null;
  default: boolean;
  repo: string | null;
}

export interface SkillRecommendation {
  name: string;
  source: string;
  source_ref: string;
  reason: string;
}

export interface StartSkillsConciergeSessionPayload {
  target_kind: "organization" | "department" | "employee";
  target_id: string;
  target_label?: string | null;
  target_agent_ids?: string[];
}

export interface SkillsConciergeSession {
  session_id: string;
  agent_id: string;
  agent_name: string;
  title: string;
  prompt: string;
  web_path: string;
}

export interface CreateSkillPreviewPayload {
  operation?: "install" | "remove";
  target_agent_ids: string[];
  install_mode?: "replace" | "skip_existing" | "fail_on_existing";
  source?: string;
  source_ref?: string | null;
  command?: SkillCommandBody | null;
  selected_skill_names?: string[] | null;
  skill_names?: string[] | null;
}

const SOURCES_KEY = ["skill-sources"];
const INSTALLED_KEY = ["installed-skills"];
const MARKETPLACES_KEY = ["skill-marketplaces"];
const RECOMMENDATIONS_KEY = ["skill-recommendations"];

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    return body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

export function useSkillSources() {
  return useQuery({
    queryKey: SOURCES_KEY,
    queryFn: async () => {
      const res = await authenticatedFetch("/v1/skills/sources");
      if (!res.ok) throw new Error(await readError(res));
      const body = (await res.json()) as { data: SkillSource[] };
      return body.data;
    },
    staleTime: 60_000,
  });
}

export function useInstalledSkills() {
  return useQuery({
    queryKey: INSTALLED_KEY,
    queryFn: async () => {
      const res = await authenticatedFetch("/v1/skills/installed");
      if (!res.ok) throw new Error(await readError(res));
      const body = (await res.json()) as { data: InstalledSkill[] };
      return body.data;
    },
    staleTime: 10_000,
  });
}

export function useSkillMarketplaces() {
  return useQuery({
    queryKey: MARKETPLACES_KEY,
    queryFn: async () => {
      const res = await authenticatedFetch("/v1/skills/marketplaces");
      if (!res.ok) throw new Error(await readError(res));
      const body = (await res.json()) as { data: SkillMarketplace[] };
      return body.data;
    },
    staleTime: 60_000,
  });
}

export function useSkillRecommendations(department?: string | null, title?: string | null) {
  return useQuery({
    queryKey: [...RECOMMENDATIONS_KEY, department ?? "", title ?? ""],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (department) params.set("department", department);
      if (title) params.set("title", title);
      const res = await authenticatedFetch(`/v1/skills/recommendations?${params.toString()}`);
      if (!res.ok) throw new Error(await readError(res));
      const body = (await res.json()) as { data: SkillRecommendation[] };
      return body.data;
    },
    staleTime: 30_000,
  });
}

export function useStartSkillsConciergeSession() {
  return useMutation({
    mutationFn: async (payload: StartSkillsConciergeSessionPayload) => {
      const res = await authenticatedFetch("/v1/skills/concierge/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await readError(res));
      return (await res.json()) as SkillsConciergeSession;
    },
  });
}

export function useSearchSkills() {
  return useMutation({
    mutationFn: async (payload: SearchSkillsPayload) => {
      const res = await authenticatedFetch("/v1/skills/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await readError(res));
      return (await res.json()) as { data: SkillSearchResult[]; errors: string[] };
    },
  });
}

export function useCreateSkillPreview() {
  return useMutation({
    mutationFn: async (payload: CreateSkillPreviewPayload) => {
      const res = await authenticatedFetch("/v1/skills/previews", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await readError(res));
      return (await res.json()) as SkillPreview;
    },
  });
}

export function useApplySkillPreview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      previewId,
      targetAgentIds,
    }: {
      previewId: string;
      targetAgentIds?: string[];
    }) => {
      const res = await authenticatedFetch(
        `/v1/skills/previews/${encodeURIComponent(previewId)}/apply`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_agent_ids: targetAgentIds }),
        },
      );
      if (!res.ok) throw new Error(await readError(res));
      return (await res.json()) as { data: SkillApplyResult[] };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: INSTALLED_KEY });
      void queryClient.invalidateQueries({ queryKey: ["available-agents"] });
    },
  });
}
