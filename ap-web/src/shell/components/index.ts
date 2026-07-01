// AppShell
export { AppShellLayout } from "./app-shell/AppShellLayout";
export { AppShellAgentInfoDialog } from "./app-shell/AppShellAgentInfoDialog";
export { useAppShellState } from "./app-shell/useAppShellState";
export { initialSidebarOpen } from "./app-shell/appShellUtils";

// Sidebar
export { SidebarConversationList } from "./SidebarConversationList";
export { SidebarConversationSection } from "./SidebarConversationSection";
export { SidebarConversationRow } from "./sidebar-conversation-row/SidebarConversationRow";
export { SidebarConversationEditRow } from "./SidebarConversationEditRow";
export { SidebarDeletingRow } from "./SidebarDeletingRow";
export { SidebarArchivingRow } from "./SidebarArchivingRow";
export { useActiveNavItem } from "./useActiveNavItem";
export { isMobileViewport } from "./sidebarViewport";
export {
  readPinnedConversationIds,
  writePinnedConversationIds,
  readCollapsedSidebarSections,
  writeCollapsedSidebarSections,
} from "./sidebarStorage";
export {
  TIME_MARKER_SLOT_CLASS,
  isOwnedByViewer,
  sameStringArray,
} from "./sidebarConversationConstants";

// New chat landing
export * from "./new-chat-landing";

// Comments panel
export { CommentCard } from "./CommentCard";
export * from "./commentsPanelUtils";

// Workspace picker
export * from "./workspacePickerUtils";

// Files panel
export { HiddenFilesToggle } from "./HiddenFilesToggle";
export { FilesPanelSortSelector } from "./FilesPanelSortSelector";
export { FileScopeSwitch } from "./FileScopeSwitch";
export { SearchFilterInput } from "./SearchFilterInput";
export { WorkingDirLabel } from "./WorkingDirLabel";

// Subagents panel
export { SubagentMainRow } from "./SubagentMainRow";
export { SubagentRow } from "./SubagentRow";
export { SubagentStatusIndicator } from "./SubagentStatusIndicator";
export * from "./subagentsPanelConstants";
export * from "./subagentsPanelUtils";

// Folder tree
export { FolderTreeIndentGuides } from "./FolderTreeIndentGuides";
export { FolderTreeFileRowItem } from "./FolderTreeFileRowItem";
export {
  FolderTreeSearchResultRow,
  FolderTreeFileRow,
  FolderTreeNodeRow,
} from "./FolderTreeRows";
export * from "./folderTreeConstants";
export * from "./folderTreeUtils";

// Fork session
export { ForkSessionForm } from "./fork-session/ForkSessionForm";
export { ForkSessionHostLabel } from "./fork-session/ForkSessionHostLabel";
export * from "./fork-session/forkSessionConstants";
export * from "./fork-session/forkSessionUtils";

// Table bubble menu
export { TableHandles } from "./table-handles/TableHandles";
export { TableHandleMenu, type MenuItemDef } from "./table-handles/TableHandleMenu";
export * from "./table-handles/tableBubbleMenuUtils";

// File viewer
export * from "./file-viewer";

// Code viewer
export { CodeViewer, type CodeViewerProps } from "./code-viewer/CodeViewer";

// Chat header
export { ChatHeader, type MobileSessionMenuProps } from "./chat-header/ChatHeader";

// Workspace panel
export { WorkspacePanel } from "./workspace-panel/WorkspacePanel";

// Markdown toolbar
export {
  ToolbarPlugin,
  ToolbarBtn,
  Divider,
} from "./markdown-toolbar/MarkdownEditorToolbar";

// Toolbar overflow (shared)
export { useToolbarOverflow } from "./useToolbarOverflow";