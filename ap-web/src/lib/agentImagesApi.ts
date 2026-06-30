import { authenticatedFetch } from "@/lib/identity";

export interface AgentImage {
  id: string;
  name: string;
  version: number;
  config: Record<string, unknown>;
  instructions: string | null;
  skills: string[];
  mcp_servers: string[];
  python_tools: string[];
  typescript_tools: string[];
  sub_agents: string[];
  sot_tier: string | null;
}

export interface AgentImageSnapshot {
  image: AgentImage;
  etag: string | null;
}

export interface AgentImageUpdate {
  config?: Record<string, unknown>;
  instructions?: string | null;
  files?: Record<string, string>;
  remove?: string[];
}

export interface AgentImageTreeEntry {
  name: string;
  path: string;
  type: "directory" | "file";
  size: number | null;
}

export interface AgentImageTree {
  id: string;
  name: string;
  version: number;
  path: string;
  entries: AgentImageTreeEntry[];
}

export interface AgentImageFile {
  id: string;
  name: string;
  version: number;
  path: string;
  content: string;
  size: number;
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    return body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as T;
}

export async function fetchAgentImage(agentId: string): Promise<AgentImageSnapshot> {
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}/image`);
  const image = await jsonOrThrow<AgentImage>(res);
  return { image, etag: res.headers.get("etag") };
}

export async function updateAgentImage(
  agentId: string,
  body: AgentImageUpdate,
  etag?: string | null,
): Promise<AgentImage> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (etag) headers["If-Match"] = etag;
  const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(agentId)}/image`, {
    method: "PUT",
    headers,
    body: JSON.stringify(body),
  });
  return jsonOrThrow<AgentImage>(res);
}

export async function fetchAgentImageTree(agentId: string, path = ""): Promise<AgentImageTree> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const res = await authenticatedFetch(
    `/v1/agents/${encodeURIComponent(agentId)}/image/tree${suffix}`,
  );
  return jsonOrThrow<AgentImageTree>(res);
}

export async function fetchAgentImageFile(agentId: string, path: string): Promise<AgentImageFile> {
  const params = new URLSearchParams({ path });
  const res = await authenticatedFetch(
    `/v1/agents/${encodeURIComponent(agentId)}/image/file?${params.toString()}`,
  );
  return jsonOrThrow<AgentImageFile>(res);
}
