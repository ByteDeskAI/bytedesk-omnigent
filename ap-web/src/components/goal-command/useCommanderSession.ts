import { useEffect, useMemo, useRef, useState } from "react";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useConversations } from "@/hooks/useConversations";
import { useHostFilesystem, type HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { useHosts } from "@/hooks/useHosts";
import { bindOnlyOnlineRunner, createSession, launchRunner } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";

// The goal-commander is provisioned backend-side (apply_goal_commander.py)
// and seeded agents carry a generated `ag_…` id — so the stable handle is
// the `name` slug, with id + loose display-name fallbacks (mirrors the
// skills-concierge resolution in SkillsPage).
export const COMMANDER_AGENT_NAME = "goal-commander";

function findCommander(agents: AvailableAgent[]): AvailableAgent | null {
  return (
    agents.find((agent) => agent.name === COMMANDER_AGENT_NAME) ??
    agents.find((agent) => agent.id === COMMANDER_AGENT_NAME) ??
    agents.find((agent) => /goal.?commander/i.test(agent.display_name ?? "")) ??
    null
  );
}

function homeWorkspaceFromEntries(entries: HostFilesystemEntry[]): string | null {
  const first = entries[0];
  if (!first) return null;
  const slash = first.path.lastIndexOf("/");
  if (slash < 0) return null;
  return slash === 0 ? "/" : first.path.slice(0, slash);
}

export interface CommanderSession {
  /** Durable agent id for sends/templated controls; null until resolved. */
  agentId: string | null;
  /** The bound commander conversation id; null until started/resumed. */
  sessionId: string | null;
  /** True while resolving/creating the persistent session. */
  starting: boolean;
  /** Non-null when the commander agent isn't provisioned or start failed. */
  error: string | null;
}

/**
 * Resolve the goal-commander agent and bind a single PERSISTENT chat
 * session to the shared chatStore (via `switchTo`). Prefers resuming an
 * existing commander conversation over spinning a new one, so the
 * founder's command-center thread survives reloads. Best-effort runner
 * bind mirrors SkillsPage; messages still relaunch a stopped runner.
 */
export function useCommanderSession(): CommanderSession {
  const agentsQuery = useAvailableAgents();
  const agents = useMemo(() => agentsQuery.data ?? [], [agentsQuery.data]);
  const commander = useMemo(() => findCommander(agents), [agents]);

  const conversationsQuery = useConversations();
  const existingSessionId = useMemo(() => {
    if (!commander) return null;
    const all = conversationsQuery.data?.pages.flatMap((page) => page.data) ?? [];
    const match = all.find(
      (conversation) =>
        conversation.agent_id === commander.id ||
        conversation.agent_name === COMMANDER_AGENT_NAME,
    );
    return match?.id ?? null;
  }, [commander, conversationsQuery.data]);

  const hosts = useHosts();
  const onlineHost = useMemo(
    () => (hosts.data ?? []).find((host) => host.status === "online") ?? null,
    [hosts.data],
  );
  const homeListing = useHostFilesystem(onlineHost?.host_id ?? null, onlineHost ? "" : null);
  const launchWorkspace = useMemo(
    () => homeWorkspaceFromEntries(homeListing.data?.entries ?? []),
    [homeListing.data],
  );

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One-shot guard: only ever start/resume the commander session once per
  // mount, even as the conversations/hosts queries settle around it.
  const startedRef = useRef(false);

  // Agent resolved but not provisioned → surface a clear error, no spin.
  useEffect(() => {
    if (agentsQuery.isLoading) return;
    if (!commander && !startedRef.current) {
      setError("The goal-commander agent isn't provisioned yet.");
    }
  }, [agentsQuery.isLoading, commander]);

  useEffect(() => {
    if (!commander || startedRef.current) return;
    // Wait for the conversations list before deciding resume-vs-create so
    // we don't create a duplicate thread on a slow first load.
    if (conversationsQuery.isLoading) return;
    startedRef.current = true;
    let cancelled = false;
    setStarting(true);
    setError(null);
    void (async () => {
      try {
        const id =
          existingSessionId ??
          (await createSession(commander.id, [], { title: "Goals Command Center" })).id;
        if (cancelled) return;
        setSessionId(id);
        await useChatStore.getState().switchTo(id);
        // Reuse a live runner when one exists; otherwise launch on the
        // online host's home workspace so the commander is host-bound and
        // can use the normal message-time relaunch path.
        const bound = await bindOnlyOnlineRunner(id);
        if (!bound && onlineHost && launchWorkspace) {
          await launchRunner(onlineHost.host_id, id, launchWorkspace);
        }
      } catch (err) {
        if (!cancelled) {
          startedRef.current = false;
          setError(err instanceof Error ? err.message : "Unable to start the commander session.");
        }
      } finally {
        if (!cancelled) setStarting(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    commander,
    conversationsQuery.isLoading,
    existingSessionId,
    onlineHost,
    launchWorkspace,
  ]);

  return {
    agentId: commander?.id ?? null,
    sessionId,
    starting,
    error,
  };
}
