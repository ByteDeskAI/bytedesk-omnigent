/**
 * Re-exports for the home-page landing composer and its shared utilities.
 * Implementation lives in `./components/new-chat-landing/`.
 */
export { NewChatLandingScreen } from "./components/new-chat-landing/NewChatLandingScreen";
export { ConnectHostInstructions } from "./components/new-chat-landing/ConnectHostInstructions";
export {
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
  sessionsSharingDirectory,
} from "./components/new-chat-landing/newChatLandingUtils";