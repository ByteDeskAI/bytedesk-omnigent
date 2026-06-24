import { hostFetch } from "@/lib/host";

export interface OmniCliTerminalStatus {
  enabled: boolean;
  namespace: string;
  pod_name: string;
  container: string;
  phase: string | null;
  server_url: string;
  attach_path: string;
}

export async function getOmniCliTerminalStatus(): Promise<OmniCliTerminalStatus | null> {
  let res: Response;
  try {
    res = await hostFetch("/v1/admin/omni-cli/terminal", { cache: "no-store" });
  } catch {
    return null;
  }
  if (!res.ok) return null;
  return (await res.json()) as OmniCliTerminalStatus;
}
