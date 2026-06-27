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
  // The start effect must run EXACTLY once (gated by `startedRef`) and must not
  // be re-triggered — and thus cancelled mid-flight — when the hosts /
  // filesystem / conversations queries settle after first paint. Were these in
  // the effect deps, a settle would fire the cleanup (`cancelled = true`) on the
  // in-flight start while `startedRef` blocks a restart, so the `finally`'s
  // ``if (!cancelled) setStarting(false)`` is skipped and the chat is stranded
  // on "Connecting…" forever. Read their latest values through refs instead.
  const existingSessionIdRef = useRef<string | null>(null);
  existingSessionIdRef.current = existingSessionId;
  const onlineHostRef = useRef<typeof onlineHost>(null);
  onlineHostRef.current = onlineHost;
  const launchWorkspaceRef = useRef<string | null>(null);
  launchWorkspaceRef.current = launchWorkspace;

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
          existingSessionIdRef.current ??
          (await createSession(commander.id, [], { title: "Goals Command Center" })).id;
        if (cancelled) return;
        setSessionId(id);
        await useChatStore.getState().switchTo(id);
        // Reuse a live runner when EXACTLY one exists; otherwise launch a
        // dedicated one on the online host's home workspace so the commander is
        // host-bound and can use the normal message-time relaunch path. The
        // reuse is a best-effort optimization, NOT a hard requirement:
        // bindOnlyOnlineRunner throws when the choice is ambiguous (multiple
        // online runners), which must not fail the whole session — the commander
        // simply launches its own runner instead. Any bind/launch failure here
        // is non-fatal because sending a message relaunches a stopped runner.
        let bound: Awaited<ReturnType<typeof bindOnlyOnlineRunner>> = null;
        try {
          bound = await bindOnlyOnlineRunner(id);
        } catch {
          bound = null;
        }
        const host = onlineHostRef.current;
        const workspace = launchWorkspaceRef.current;
        if (!bound && host && workspace) {
          try {
            await launchRunner(host.host_id, id, workspace);
          } catch {
            // best-effort warmup; message-time relaunch covers a miss here
          }
        }
      } catch (err) {
        if (!cancelled) {
          startedRef.current = false;
          setError(err instanceof Error ? err.message : "Unable to start the commander session.");
        }
      } finally {
        // Always clear `starting`: this start runs once (startedRef-gated) and
        // is never superseded, so the spinner must resolve even if the effect
        // was torn down — leaving it true strands the UI on "Connecting…".
        setStarting(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [commander, conversationsQuery.isLoading]);

  return {
    agentId: commander?.id ?? null,
    sessionId,
    starting,
    error,
  };
}
