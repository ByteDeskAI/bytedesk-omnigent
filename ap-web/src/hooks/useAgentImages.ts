import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchAgentImage,
  fetchAgentImageFile,
  fetchAgentImageTree,
  updateAgentImage,
  type AgentImageUpdate,
} from "@/lib/agentImagesApi";

const IMAGE_KEY = ["agent-image"];
const TREE_KEY = ["agent-image-tree"];

export function useAgentImage(agentId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: [...IMAGE_KEY, agentId],
    queryFn: () => fetchAgentImage(agentId as string),
    enabled: enabled && Boolean(agentId),
    staleTime: 5_000,
    retry: false,
  });
}

export function useAgentImageTree(
  agentId: string | null | undefined,
  path: string,
  enabled = true,
) {
  return useQuery({
    queryKey: [...TREE_KEY, agentId, path],
    queryFn: () => fetchAgentImageTree(agentId as string, path),
    enabled: enabled && Boolean(agentId),
    staleTime: 5_000,
    retry: false,
  });
}

export function useReadAgentImageFile() {
  return useMutation({
    mutationFn: ({ agentId, path }: { agentId: string; path: string }) =>
      fetchAgentImageFile(agentId, path),
  });
}

export function useUpdateAgentImage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      body,
      etag,
    }: {
      agentId: string;
      body: AgentImageUpdate;
      etag?: string | null;
    }) => updateAgentImage(agentId, body, etag),
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: [...IMAGE_KEY, variables.agentId] });
      void queryClient.invalidateQueries({ queryKey: [...TREE_KEY, variables.agentId] });
      void queryClient.invalidateQueries({ queryKey: ["available-agents"] });
      void queryClient.invalidateQueries({ queryKey: ["installed-skills"] });
      void queryClient.invalidateQueries({ queryKey: ["connectors-catalog"] });
    },
  });
}
