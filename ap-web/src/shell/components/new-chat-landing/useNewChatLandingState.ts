import { type DragEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { appendPromptHistoryEntry } from "@/hooks/usePromptHistory";
import { getCliServerUrl, getOmnigentHostConfig } from "@/lib/host";
import { readLastAgentId } from "@/lib/agentPreferences";
import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import {
  isNativeCodingAgent,
  nativeAgentHasCapability,
  nativeAgentSortRank,
  nativeWrapperLabelsForAgent,
} from "@/lib/nativeCodingAgents";
import { groupAgentsByTier } from "@/lib/agentTiers";
import { useHosts } from "@/hooks/useHosts";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import {
  AGENT_DISPLAY_ORDER,
  CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  CLAUDE_NATIVE_PERMISSION_MODES,
  CODEX_NATIVE_APPROVAL_MODES,
  CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
  NEW_SESSION_HIDDEN_AGENTS,
  SKILL_PILL_AGENTS,
} from "./newChatLandingConstants";
import {
  composeSandboxWorkspace,
  deriveHomeDir,
  deriveRepoName,
  describeCreateError,
  harnessUnconfiguredOnHost,
  isValidSandboxRepoUrl,
  isValidWorkspace,
  matchSkillInvocation,
  normalizeWorkspacePath,
  sanitizeInitialPrompt,
} from "./newChatLandingUtils";

export type NewChatLandingState = ReturnType<typeof useNewChatLandingState>;

export function useNewChatLandingState() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const serverUrl = getCliServerUrl();
  const { data: agents } = useAvailableAgents();
  const { data: hosts } = useHosts();
  // Sessions the caller can access, to warn when a new session would share a
  // working directory with a live one (see the conflict tooltip below).
  const { data: directorySessions } = useDirectorySessions(true);

  const agentList = useMemo(() => {
    const displayRank = (name: string) => {
      const i = AGENT_DISPLAY_ORDER.indexOf(name);
      return i === -1 ? AGENT_DISPLAY_ORDER.length : i;
    };
    return [...(agents ?? [])]
      .filter((a) => !NEW_SESSION_HIDDEN_AGENTS.has(a.name))
      .sort(
        (a, b) =>
          nativeAgentSortRank(a) - nativeAgentSortRank(b) ||
          displayRank(a.display_name) - displayRank(b.display_name),
      );
  }, [agents]);

  // Group the picker into labelled tier sections with dividers between.
  // Grouping preserves agentList's order, so the native-agent sort above is
  // kept within each tier.
  const agentTiers = useMemo(() => groupAgentsByTier(agentList), [agentList]);

  const [message, setMessage] = useState<string>("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // maxRows 9 = 180px of 20px lines, matching the composer's 200px
  // border-box max (180px content + 16px top / 4px bottom padding).
  useAutoGrowTextarea(textareaRef, message, 9);

  // Attachments for the first message — same affordances as the in-session
  // composer (paperclip + paste); carried to ChatPage via the pending
  // initial prompt and sent with the auto-dispatched first turn.
  const [files, setFiles] = useState<File[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const addFiles = (incoming: File[]) => setFiles((prev) => [...prev, ...incoming]);
  const removeFile = (index: number) => setFiles((prev) => prev.filter((_, i) => i !== index));

  // Drag-and-drop onto the composer — same behavior as the in-session
  // composer (drop files anywhere on the box; an inset ring + overlay
  // signal the drop target).
  const [isDragActive, setIsDragActive] = useState(false);

  const handleDrop = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(false);
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length > 0) addFiles(dropped);
  };

  const handleDragOver = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragEnter = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    // Only clear the active state when the pointer leaves the container
    // itself, not when it moves between child elements inside it.
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsDragActive(false);
  };

  // Gates the sandbox host option: only servers whose sandbox
  // config can actually serve a managed launch advertise it. "loading"
  // fails closed (option hidden) until the boot probe resolves.
  const info = useServerInfo();
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  // Provider-named label for the sandbox option (e.g. "Modal Sandbox"),
  // falling back to the generic "New Sandbox" when the server names no
  // provider.
  const sandboxLabel = sandboxOptionLabel(info !== "loading" ? info.sandbox_provider : null);
  // Embed-only docs seam: when the host passes additional docs and managed
  // sandboxes are unavailable, keep the sandbox row visible but disabled and
  // attach a help tooltip with a clickable link.
  const docsLinks = getOmnigentHostConfig().docsLinks;
  const newSandboxTooltipContent = docsLinks?.newSandbox;
  // Embed-only docs seam for Databricks git auth setup. Standalone leaves this
  // undefined, so no tooltip is rendered.
  const databricksGitCredentialsTooltipContent = docsLinks?.databricksGitCredentials;
  const showDisabledSandboxWithDocs = !managedSandboxesEnabled && !!newSandboxTooltipContent;

  // Seeded from the persisted last pick so a returning user starts on the
  // agent they used last; validated against the live list in
  // effectiveAgentId below (a stale id falls back to the default).
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(() => readLastAgentId());
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);
  // True when the user picked the sandbox option instead of a connected
  // host — the server provisions a sandbox host at create time
  // (host_type: "managed"), so no host_id or workspace is sent.
  const [sandboxSelected, setSandboxSelected] = useState(false);
  // Sandbox repository inputs — composed into the managed create's
  // `workspace` string (`<url>[#<branch>]`); both blank = empty
  // server-created workspace.
  const [sandboxRepoUrl, setSandboxRepoUrl] = useState<string>("");
  const [sandboxRepoBranch, setSandboxRepoBranch] = useState<string>("");
  const [workspace, setWorkspace] = useState<string>("");
  const [branchName, setBranchName] = useState<string>("");
  const [baseBranch, setBaseBranch] = useState<string>("");
  // Permission mode for Claude Code (claude --permission-mode). Only
  // meaningful for the claude-native wrapper; ignored otherwise. Lives in
  // the footer tray's Advanced settings menu.
  const [permissionMode, setPermissionMode] = useState<string>(
    CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE,
  );
  // Approval mode for Codex (codex --approval-mode). Only meaningful for
  // the codex-native wrapper; ignored otherwise. Lives in the footer
  // tray's Advanced settings menu.
  const [approvalMode, setApprovalMode] = useState<string>(CODEX_NATIVE_DEFAULT_APPROVAL_MODE);
  // Per-session brain-harness override for bundle agents (polly / debby).
  // null = the agent spec's declared harness (no override sent); cleared on
  // every agent switch so a pick never leaks across agents.
  const [pickedHarness, setPickedHarness] = useState<string | null>(null);
  // Controls the working-directory popover so picking a directory closes it.
  const [workspacePopoverOpen, setWorkspacePopoverOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // "Connect a host" instructions modal, opened from the host dropdown.
  const [connectOpen, setConnectOpen] = useState(false);

  const { recent, addRecent } = useRecentWorkspaces(selectedHostId);

  const allHosts = hosts ?? [];
  const onlineHosts = allHosts.filter((h) => h.status === "online");
  const offlineHosts = allHosts.filter((h) => h.status === "offline");

  // Auto-select the FIRST AVAILABLE option, mirroring the menu order, so
  // a session can be started without an explicit pick: the sandbox when
  // the server supports it (it's pinned first in the picker), else the
  // first online host. Only fills an empty slot; explicit choices are
  // never overridden.
  useEffect(() => {
    if (sandboxSelected) return;
    if (selectedHostId !== null) return;
    if (managedSandboxesEnabled) {
      setSandboxSelected(true);
      return;
    }
    const firstOnline = (hosts ?? []).find((h) => h.status === "online");
    if (firstOnline) setSelectedHostId(firstOnline.host_id);
  }, [hosts, selectedHostId, sandboxSelected, managedSandboxesEnabled]);

  // Fall back to the host's home directory when it has no recorded recents, so
  // the working-directory field is pre-filled and the user can send in one
  // click. Derived from the same home listing the picker uses (entries carry
  // absolute paths); only fetched when there's no recent to fall back to.
  const needsHomeFallback = selectedHostId !== null && recent.length === 0;
  const { data: homeListing, isPlaceholderData: homeListingIsPlaceholder } = useHostFilesystem(
    selectedHostId,
    needsHomeFallback ? "" : null,
  );
  // The hook serves the PREVIOUS query's data as a placeholder while a new
  // fetch is in flight (an anti-flicker nicety for the picker), so right
  // after a host switch the listing briefly belongs to the old host.
  // Deriving home from it would seed the old host's path and lock the
  // once-per-host guard below — treat placeholder data as not-yet-loaded.
  const derivedHome = useMemo(
    () => (homeListingIsPlaceholder ? null : deriveHomeDir(homeListing?.entries ?? [])),
    [homeListing, homeListingIsPlaceholder],
  );

  // Seed the working directory once per host, into an empty field only, so an
  // explicit pick isn't clobbered. Prefer the most-recent path; else the
  // derived home (which can arrive a render later, hence the dep).
  const seededHostRef = useRef<string | null>(null);
  useEffect(() => {
    if (selectedHostId === null) return;
    if (seededHostRef.current === selectedHostId) return;
    const candidate = recent[0] ?? derivedHome;
    if (!candidate) return;
    seededHostRef.current = selectedHostId;
    setWorkspace((cur) => (cur === "" ? candidate : cur));
  }, [selectedHostId, recent, derivedHome]);

  // A pick only wins while it exists in the list — a persisted id whose
  // agent has since been unregistered (or hidden) falls back to the default.
  const effectiveAgentId =
    (agentList.some((a) => a.id === pickedAgentId) ? pickedAgentId : agentList[0]?.id) ?? null;
  const selectedAgent = agentList.find((a) => a.id === effectiveAgentId);
  const supportsPermissionMode = nativeAgentHasCapability(selectedAgent, "permissionMode");
  const supportsApprovalMode = nativeAgentHasCapability(selectedAgent, "approvalMode");
  // Native-terminal agents interpret slash commands inside their own CLI
  // (the runner injects the text verbatim), so the landing composer must
  // not intercept them — no skills menu, no slash_command routing.
  const isNativeTerminalAgent = isNativeCodingAgent(selectedAgent);
  const selectedHost = allHosts.find((h) => h.host_id === selectedHostId);
  // Warn-only readiness signal for the agent picker: only meaningful when
  // a connected host is selected (a sandbox provisions its own tooling).
  // Selection stays allowed — the host re-checks at launch and the create
  // call surfaces a specific error if the harness really can't run.
  const harnessWarningHost = !sandboxSelected ? selectedHost : undefined;
  const selectedAgentUnconfigured = harnessUnconfiguredOnHost(
    selectedAgent?.harness,
    harnessWarningHost,
  );
  const workspaceTrimmed = workspace.trim();
  const workspaceValid = isValidWorkspace(workspace);
  const isCloudHost =
    sandboxSelected || (selectedHost?.name?.toLowerCase().includes("cloud") ?? false);

  // Sessions on the selected host that have a workspace — candidates for a
  // directory conflict, fed to the runner-health poll so only *connected*
  // agents count (same /health signal as the sidebar dots).
  const conflictCandidates = useMemo(
    () =>
      (directorySessions ?? []).filter((s) => s.host_id === selectedHostId && s.workspace != null),
    [directorySessions, selectedHostId],
  );
  const runnerHealth = useRunnerHealthRegistration(conflictCandidates);
  // Count of live agents per normalized directory on this host. The file
  // browser uses this to warn when you navigate into an occupied directory.
  const occupancyByDir = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of conflictCandidates) {
      if (s.workspace == null || runnerHealth.get(s.id) !== true) continue;
      const dir = normalizeWorkspacePath(s.workspace);
      if (dir === null) continue;
      counts.set(dir, (counts.get(dir) ?? 0) + 1);
    }
    return counts;
  }, [conflictCandidates, runnerHealth]);

  // Sandbox repo inputs are valid when blank (empty workspace), or when
  // the URL passes the shape check; a branch without a URL is dangling.
  const sandboxRepoValid =
    sandboxRepoUrl.trim() === ""
      ? sandboxRepoBranch.trim() === ""
      : isValidSandboxRepoUrl(sandboxRepoUrl);

  // Sandbox creates need no host or path workspace — the server
  // provisions both; only the message, agent, and (optional) repo
  // inputs gate the submit.
  // Slash-command suggestions for the chosen agent's bundled skills.
  // Mirrors the in-session composer's menu mechanics (open while the
  // command name is still being typed: leading "/", no second "/", no
  // space yet), but lists skills only — built-ins like /model need a
  // live session. Hidden for native-terminal agents (their CLI owns
  // slash commands) and for agents without bundled skills.
  const [slashMenuIndex, setSlashMenuIndex] = useState(-1);
  const skillCommands = useMemo(() => {
    if (isNativeTerminalAgent) return {};
    const m: Record<string, string> = {};
    for (const s of selectedAgent?.skills ?? []) m[`/${s.name}`] = s.description;
    return m;
  }, [selectedAgent, isNativeTerminalAgent]);
  const trimmedMessage = message.trimStart();
  const slashMenuOpen =
    trimmedMessage.startsWith("/") &&
    !trimmedMessage.slice(1).includes("/") &&
    !trimmedMessage.includes(" ");
  const slashMenuQuery = slashMenuOpen ? trimmedMessage.slice(1) : "";
  // Kept in sync with what SlashCommandMenu renders so keyboard nav
  // indexes into the same list.
  const slashMenuMatches = slashMenuOpen
    ? Object.keys(skillCommands).filter((name) =>
        name.slice(1).startsWith(slashMenuQuery.toLowerCase()),
      )
    : [];
  // Pre-select the first match whenever the filtered list changes, so
  // Tab/Enter complete the top item without arrowing down first (same
  // reset pattern as the in-session composer).
  const prevSlashMatchesRef = useRef<string[]>([]);
  if (
    slashMenuMatches.length !== prevSlashMatchesRef.current.length ||
    slashMenuMatches.some((m, i) => m !== prevSlashMatchesRef.current[i])
  ) {
    prevSlashMatchesRef.current = slashMenuMatches;
    setSlashMenuIndex(slashMenuMatches.length > 0 ? 0 : -1);
  }

  // Selecting a skill fills "/name " and leaves the caret ready for the
  // argument — skills never auto-execute from the menu.
  function applySlashSelection(cmd: string) {
    setSlashMenuIndex(-1);
    setMessage(cmd + " ");
    textareaRef.current?.focus();
  }

  // Always-visible skill pills for the allowlisted orchestrators, fed by
  // the same bundled-skills list as the "/" menu.
  const pillSkills =
    selectedAgent && SKILL_PILL_AGENTS.has(selectedAgent.name) ? selectedAgent.skills : [];

  // Pills only render over an empty draft, so there's never args to preserve.
  function applySkillPill(name: string) {
    setMessage(`/${name} `);
    textareaRef.current?.focus();
  }

  const canSubmit =
    message.trim().length > 0 &&
    selectedAgent != null &&
    (sandboxSelected ? sandboxRepoValid : !!selectedHostId && workspaceValid) &&
    !creating;

  // Why submit is disabled, surfaced as the button's tooltip. Checked in the
  // order a user fills the form — location first, then message — so the
  // tooltip always names the next missing input. Null when nothing is
  // actionable (submitting, or mid-create).
  const submitDisabledReason = canSubmit
    ? null
    : sandboxSelected && !sandboxRepoValid
      ? "Please enter a valid repository URL"
      : !sandboxSelected && (!selectedHostId || !workspaceValid)
        ? "Please choose a host and working directory"
        : message.trim().length === 0
          ? "Enter a message to get started"
          : null;

  // Chip display labels.
  const workspaceLabel = workspaceTrimmed
    ? (workspaceTrimmed.split("/").filter(Boolean).pop() ?? workspaceTrimmed)
    : "Working directory";
  const hostLabel = sandboxSelected
    ? sandboxLabel
    : (selectedHost?.name ?? (onlineHosts.length === 0 ? "No hosts" : "Select host"));
  const worktreeLabel = branchName.trim() || "No worktree";
  // Sandbox repository chip label: repo name (server's clone-dir rule)
  // plus the pinned branch, e.g. "repo#main"; placeholder when unset.
  const sandboxRepoName = deriveRepoName(sandboxRepoUrl);
  const sandboxRepoLabel = sandboxRepoName
    ? sandboxRepoBranch.trim()
      ? `${sandboxRepoName}#${sandboxRepoBranch.trim()}`
      : sandboxRepoName
    : "Repository";
  // Selected permission mode's display label — appended to the agent picker
  // label (non-default picks only) so a changed mode stays visible while the
  // radios live in the footer tray's Advanced settings menu.
  const permissionModeLabel =
    CLAUDE_NATIVE_PERMISSION_MODES.find((m) => m.value === permissionMode)?.label ?? permissionMode;
  const approvalModeLabel =
    CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === approvalMode)?.label ?? approvalMode;
  // Effective brain harness for the selected agent: the user's pick, else
  // the spec's declared harness. null for non-overridable agents (native
  // wrappers, agents whose spec failed to load).
  const selectedAgentDefaultHarness =
    selectedAgent?.harness != null && selectedAgent.harness in BRAIN_HARNESS_LABELS
      ? selectedAgent.harness
      : null;
  // The label suffixes the permission/approval mode / harness only when the
  // user explicitly changed it in the Advanced menu — defaults read as just
  // the agent name. pickedHarness is non-null only for an explicit
  // non-default pick (re-picking the spec default clears it).
  const agentLabel = selectedAgent
    ? supportsPermissionMode && permissionMode !== CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
      ? `${selectedAgent.display_name} (${permissionModeLabel})`
      : supportsApprovalMode && approvalMode !== CODEX_NATIVE_DEFAULT_APPROVAL_MODE
        ? `${selectedAgent.display_name} (${approvalModeLabel})`
        : pickedHarness != null
          ? `${selectedAgent.display_name} (${BRAIN_HARNESS_LABELS[pickedHarness] ?? pickedHarness})`
          : selectedAgent.display_name
    : "Select agent";

  function selectHost(hostId: string) {
    // Re-selecting the current host is a no-op. Clearing the workspace here
    // would empty the field for good: the seeding effect's deps (host id,
    // recents, derived home) are all unchanged on a same-host pick, so it
    // never re-runs to fill the field back in — and a host the user already
    // has selected (e.g. the auto-picked first online host) is exactly the
    // one they're most likely to click in the menu.
    if (hostId === selectedHostId) return;
    setSandboxSelected(false);
    setSelectedHostId(hostId);
    // Workspace is host-specific — clear it and let the seeding effect run for
    // the new host.
    setWorkspace("");
    seededHostRef.current = null;
  }

  function selectSandbox() {
    if (sandboxSelected) return;
    // Mirror selectHost: a managed session's host and workspace are both
    // server-chosen, so clear any prior host pick and its workspace.
    setSandboxSelected(true);
    setSelectedHostId(null);
    setWorkspace("");
    seededHostRef.current = null;
  }

  async function handleCreate() {
    // Mirror the Send button's disabled condition (canSubmit) so the Enter-key
    // and form-submit paths that call this directly can't create a session with
    // a blank message, host, agent, or workspace.
    if (!canSubmit) return;
    setCreating(true);
    setCreateError(null);
    try {
      const trimmedBranch = branchName.trim();
      const agent = agentList.find((a) => a.id === effectiveAgentId);
      const nativeLabels = nativeWrapperLabelsForAgent(agent);
      const agentSupportsPermissionMode = nativeAgentHasCapability(agent, "permissionMode");
      const agentSupportsApprovalMode = nativeAgentHasCapability(agent, "approvalMode");
      const res = await authenticatedFetch("/v1/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: effectiveAgentId,
          // Managed (cloud sandbox) creates let the server provision the
          // host: the schema rejects host_id and path workspaces (and git
          // needs a host_id). The optional repository inputs compose into
          // the URL-form workspace the server clones; undefined (no repo)
          // is dropped by JSON.stringify.
          ...(sandboxSelected
            ? {
                host_type: "managed",
                workspace: composeSandboxWorkspace(sandboxRepoUrl, sandboxRepoBranch),
              }
            : {
                host_id: selectedHostId,
                workspace: workspaceTrimmed,
                git: trimmedBranch
                  ? { branch_name: trimmedBranch, base_branch: baseBranch.trim() || undefined }
                  : undefined,
              }),
          // Native terminal agents open terminal-first: `omnigent.ui:
          // terminal` tells the UI to render the terminal wrapper, and
          // `omnigent.wrapper` selects which CLI bridge the runner launches.
          // The values are the registered wrapper ids the runner keys off —
          // they must match the wrapper registry, not the agent display name.
          labels: nativeLabels,
          // Permission / approval mode → CLI flag pair, persisted as
          // terminal_launch_args. Omitted for the default and non-native agents.
          terminal_launch_args:
            agentSupportsPermissionMode && permissionMode !== CLAUDE_NATIVE_DEFAULT_PERMISSION_MODE
              ? ["--permission-mode", permissionMode]
              : agentSupportsApprovalMode && approvalMode !== CODEX_NATIVE_DEFAULT_APPROVAL_MODE
                ? (CODEX_NATIVE_APPROVAL_MODES.find((m) => m.value === approvalMode)?.args ?? [])
                : undefined,
          // Cost-control UI is currently hidden, so omit the override and let
          // the session defer to the agent spec default.
          cost_control_mode_override: undefined,
          // Brain-harness pick from the agent flyout. Omitted when the user
          // kept the spec default (pickedHarness is null) so the session
          // tracks the agent's declared harness.
          harness_override: pickedHarness ?? undefined,
        }),
      });
      if (!res.ok) {
        setCreateError(await describeCreateError(res));
        return;
      }
      const data = (await res.json()) as { id: string };
      // Sandbox creates have no user-picked workspace to remember.
      if (!sandboxSelected) addRecent(workspaceTrimmed);
      // Fire-and-forget: don't block navigation on the sidebar list refresh.
      // The background refetch (or the WS session_added push) backfills the
      // new session's row within ~1s of landing in the chat; the chat itself
      // loads from the session id and never reads the sidebar cache.
      void queryClient.refetchQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["directory-sessions"] });
      const initialPrompt = sanitizeInitialPrompt(message);
      // A first message matching one of the agent's bundled skills is
      // handed off as a structured invocation so ChatPage auto-sends it
      // as a `slash_command` event (server resolves the skill) instead
      // of plain text the agent would see as a literal "/name". Native
      // terminal agents keep plain text — their CLI owns slash commands.
      setPendingInitialPrompt(data.id, {
        text: initialPrompt,
        skill: isNativeTerminalAgent
          ? null
          : matchSkillInvocation(initialPrompt, agent?.skills ?? []),
        files,
      });
      // Scope the recall entry to the new session id so ArrowUp surfaces it in
      // the freshly-opened chat (whose composer reads the same per-conversation
      // key). Sanitized text so recall reproduces exactly what was sent.
      appendPromptHistoryEntry(initialPrompt, data.id);
      navigate(`/c/${data.id}`);
    } catch {
      setCreateError("Couldn't reach the server. Check your connection and try again.");
    } finally {
      setCreating(false);
    }
  }

  return {
    navigate,
    queryClient,
    serverUrl,
    agents,
    hosts,
    directorySessions,
    agentList,
    agentTiers,
    message,
    setMessage,
    textareaRef,
    files,
    addFiles,
    removeFile,
    fileInputRef,
    isDragActive,
    setIsDragActive,
    handleDrop,
    handleDragOver,
    handleDragEnter,
    handleDragLeave,
    info,
    managedSandboxesEnabled,
    sandboxLabel,
    docsLinks,
    newSandboxTooltipContent,
    databricksGitCredentialsTooltipContent,
    showDisabledSandboxWithDocs,
    pickedAgentId,
    setPickedAgentId,
    selectedHostId,
    setSelectedHostId,
    sandboxSelected,
    setSandboxSelected,
    sandboxRepoUrl,
    setSandboxRepoUrl,
    sandboxRepoBranch,
    setSandboxRepoBranch,
    workspace,
    setWorkspace,
    branchName,
    setBranchName,
    baseBranch,
    setBaseBranch,
    permissionMode,
    setPermissionMode,
    approvalMode,
    setApprovalMode,
    pickedHarness,
    setPickedHarness,
    workspacePopoverOpen,
    setWorkspacePopoverOpen,
    creating,
    setCreating,
    createError,
    setCreateError,
    connectOpen,
    setConnectOpen,
    recent,
    addRecent,
    allHosts,
    onlineHosts,
    offlineHosts,
    needsHomeFallback,
    homeListing,
    homeListingIsPlaceholder,
    derivedHome,
    seededHostRef,
    effectiveAgentId,
    selectedAgent,
    supportsPermissionMode,
    supportsApprovalMode,
    isNativeTerminalAgent,
    selectedHost,
    harnessWarningHost,
    selectedAgentUnconfigured,
    workspaceTrimmed,
    workspaceValid,
    isCloudHost,
    conflictCandidates,
    runnerHealth,
    occupancyByDir,
    sandboxRepoValid,
    slashMenuIndex,
    setSlashMenuIndex,
    skillCommands,
    trimmedMessage,
    slashMenuOpen,
    slashMenuQuery,
    slashMenuMatches,
    prevSlashMatchesRef,
    applySlashSelection,
    pillSkills,
    applySkillPill,
    canSubmit,
    submitDisabledReason,
    workspaceLabel,
    hostLabel,
    worktreeLabel,
    sandboxRepoName,
    sandboxRepoLabel,
    permissionModeLabel,
    approvalModeLabel,
    selectedAgentDefaultHarness,
    agentLabel,
    selectHost,
    selectSandbox,
    handleCreate,
  };
}
