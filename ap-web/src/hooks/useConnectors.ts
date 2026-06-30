import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  checkConnectorHealth,
  createConnectorConnection,
  fetchConnectorCatalog,
  grantConnectorToAgent,
  setConnectorServiceEnabled,
  startConnectorOAuth,
} from "@/lib/connectorsApi";

const QUERY_KEY = ["connectors-catalog"];

export function useConnectorsCatalog() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: fetchConnectorCatalog,
    staleTime: 10_000,
  });
}

export function useStartConnectorOAuth() {
  return useMutation({
    mutationFn: startConnectorOAuth,
    onSuccess: (url) => {
      window.location.href = url;
    },
  });
}

export function useCreateConnectorConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      provider,
      displayName,
      metadata,
      secretPayload,
      secretRef,
      enabledServices,
    }: {
      provider: string;
      displayName: string;
      metadata: Record<string, unknown>;
      secretPayload?: Record<string, unknown>;
      secretRef?: string;
      enabledServices: string[];
    }) =>
      createConnectorConnection(provider, {
        displayName,
        metadata,
        secretPayload,
        secretRef,
        enabledServices,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useSetConnectorServiceEnabled() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      connectionId,
      serviceKey,
      enabled,
    }: {
      connectionId: string;
      serviceKey: string;
      enabled: boolean;
    }) => setConnectorServiceEnabled(connectionId, serviceKey, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useCheckConnectorHealth() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ connectionId, live }: { connectionId: string; live?: boolean }) =>
      checkConnectorHealth(connectionId, { live }),
    onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useGrantConnectorToAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      connectionId,
      agentId,
      tools,
    }: {
      connectionId: string;
      agentId: string;
      tools: string[];
    }) => grantConnectorToAgent(connectionId, agentId, tools),
    onSuccess: () => qc.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}
