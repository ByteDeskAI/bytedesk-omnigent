import {
  BotIcon,
  FileTextIcon,
  PlugIcon,
  PuzzleIcon,
  SlidersHorizontalIcon,
  UsersIcon,
} from "lucide-react";
import {
  AccessGate,
  ConfigTab,
  ConnectorsTab,
  DetailHeader,
  FilesTab,
  OverviewTab,
  PermissionsTab,
  RosterPanel,
  SkillsTab,
  WorkForceShell,
  editConfirmationButtonLabel,
  editConfirmationDescription,
  editConfirmationTitle,
  type PendingSave,
  type WorkForceTab,
} from "@/components/workforce";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { AgentTier } from "@/lib/agentTiers";
import type { AgentImageSnapshot, AgentImageUpdate } from "@/lib/agentImagesApi";

export interface WorkForcePageShellProps {
  allowed: boolean | null;
  filteredAgents: AvailableAgent[];
  selectedAgentId: string | null;
  setSelectedAgentId: (id: string | null) => void;
  query: string;
  setQuery: (query: string) => void;
  selectedAgent: AvailableAgent | null;
  selectedTier: AgentTier;
  editable: boolean;
  workforceEditable: boolean;
  tab: WorkForceTab;
  setTab: (tab: WorkForceTab) => void;
  isConfigDirty: boolean;
  imageSnapshot: AgentImageSnapshot | undefined;
  imageIsError: boolean;
  imageError: unknown;
  configText: string;
  setConfigText: (text: string) => void;
  instructionsText: string;
  setInstructionsText: (text: string) => void;
  onSaveConfig: () => void;
  updateImagePending: boolean;
  saveError: string | null;
  saveNotice: string | null;
  onRefetch: () => void;
  commitSave: (body: AgentImageUpdate, label: string) => void;
  pendingSave: PendingSave | null;
  setPendingSave: (save: PendingSave | null) => void;
  onConfirmPendingSave: () => void;
}

export function WorkForcePageShell({
  allowed,
  filteredAgents,
  selectedAgentId,
  setSelectedAgentId,
  query,
  setQuery,
  selectedAgent,
  selectedTier,
  editable,
  workforceEditable,
  tab,
  setTab,
  isConfigDirty,
  imageSnapshot,
  imageIsError,
  imageError,
  configText,
  setConfigText,
  instructionsText,
  setInstructionsText,
  onSaveConfig,
  updateImagePending,
  saveError,
  saveNotice,
  onRefetch,
  commitSave,
  pendingSave,
  setPendingSave,
  onConfirmPendingSave,
}: WorkForcePageShellProps) {
  return (
    <AccessGate allowed={allowed}>
      <WorkForceShell>
        <RosterPanel
          agents={filteredAgents}
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          query={query}
          setQuery={setQuery}
        />
        <main className="flex min-h-0 flex-col overflow-hidden bg-bg-base">
          {selectedAgent ? (
            <>
              <DetailHeader
                agent={selectedAgent}
                tier={selectedTier}
                editable={editable}
                refetch={onRefetch}
              />
              {selectedTier !== "workflow" && imageIsError && (
                <div className="border-b border-accent-red/30 bg-accent-red/10 px-5 py-2 text-sm text-accent-red">
                  {imageError instanceof Error ? imageError.message : "Agent image unavailable"}
                </div>
              )}
              <Tabs
                value={tab}
                onValueChange={(value) => setTab(value as WorkForceTab)}
                className="min-h-0 flex-1 overflow-hidden"
              >
                <TabsList variant="line" className="mx-5 mt-3 flex-wrap">
                  <TabsTrigger value="overview">
                    <BotIcon /> Overview
                  </TabsTrigger>
                  <TabsTrigger value="config" disabled={!editable}>
                    <SlidersHorizontalIcon /> Config
                    {isConfigDirty && editable && (
                      <span className="size-1.5 rounded-full bg-accent-amber" aria-hidden="true" />
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="permissions" disabled={!workforceEditable}>
                    <UsersIcon /> Permissions
                  </TabsTrigger>
                  <TabsTrigger value="skills" disabled={!editable}>
                    <PuzzleIcon /> Skills
                  </TabsTrigger>
                  <TabsTrigger value="connectors" disabled={!editable}>
                    <PlugIcon /> Connectors
                  </TabsTrigger>
                  <TabsTrigger value="files" disabled={!editable}>
                    <FileTextIcon /> Files
                  </TabsTrigger>
                </TabsList>
                <div className="min-h-0 flex-1 overflow-y-auto">
                  <TabsContent value="overview">
                    <OverviewTab
                      agent={selectedAgent}
                      tier={selectedTier}
                      imageVersion={imageSnapshot?.image.version ?? null}
                      sotTier={imageSnapshot?.image.sot_tier ?? null}
                      imageLoaded={Boolean(imageSnapshot)}
                    />
                  </TabsContent>
                  <TabsContent value="config">
                    <ConfigTab
                      editable={editable}
                      configText={configText}
                      setConfigText={setConfigText}
                      instructionsText={instructionsText}
                      setInstructionsText={setInstructionsText}
                      onSave={onSaveConfig}
                      busy={updateImagePending}
                      error={saveError}
                      notice={saveNotice}
                      dirty={isConfigDirty}
                    />
                  </TabsContent>
                  <TabsContent value="permissions">
                    {selectedAgent && (
                      <PermissionsTab agent={selectedAgent} editable={workforceEditable} />
                    )}
                  </TabsContent>
                  <TabsContent value="skills">
                    {selectedAgentId && <SkillsTab agentId={selectedAgentId} editable={editable} />}
                  </TabsContent>
                  <TabsContent value="connectors">
                    {selectedAgentId && (
                      <ConnectorsTab agentId={selectedAgentId} editable={editable} />
                    )}
                  </TabsContent>
                  <TabsContent value="files">
                    {selectedAgentId && (
                      <FilesTab
                        agentId={selectedAgentId}
                        editable={editable}
                        etag={imageSnapshot?.etag}
                        commitSave={commitSave}
                        busy={updateImagePending}
                      />
                    )}
                  </TabsContent>
                </div>
              </Tabs>
            </>
          ) : (
            <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
              No agents match the current filter.
            </div>
          )}
        </main>
      </WorkForceShell>
      <Dialog open={pendingSave !== null} onOpenChange={(open) => !open && setPendingSave(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editConfirmationTitle(pendingSave?.tier)}</DialogTitle>
            <DialogDescription>{editConfirmationDescription(pendingSave?.tier)}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingSave(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={updateImagePending || pendingSave === null}
              onClick={onConfirmPendingSave}
            >
              {editConfirmationButtonLabel(pendingSave?.tier)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </AccessGate>
  );
}