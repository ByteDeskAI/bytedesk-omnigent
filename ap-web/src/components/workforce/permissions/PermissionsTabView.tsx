import {
  FileTextIcon,
  PlugIcon,
  PuzzleIcon,
  TerminalIcon,
} from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { PermissionsTabState } from "./usePermissionsTab";
import {
  PermissionsConnectorsTabView,
  PermissionsInstructionsTabView,
  PermissionsScopeHeader,
  PermissionsSkillsTabView,
  PermissionsToolsTabView,
} from "./views";

export function PermissionsTabView(s: PermissionsTabState) {
  const { error, notice } = s;

  return (
    <div className="mc-fade-up space-y-4 p-4">
      <PermissionsScopeHeader {...s} />

      <Tabs defaultValue="tools" className="space-y-4">
        <TabsList variant="line" className="flex-wrap" aria-label="Permission groups">
          <TabsTrigger value="tools">
            <TerminalIcon /> Tools
          </TabsTrigger>
          <TabsTrigger value="instructions">
            <FileTextIcon /> Instructions
          </TabsTrigger>
          <TabsTrigger value="skills">
            <PuzzleIcon /> Skills
          </TabsTrigger>
          <TabsTrigger value="connectors">
            <PlugIcon /> Connectors
          </TabsTrigger>
        </TabsList>

        <TabsContent value="tools" className="space-y-4">
          <PermissionsToolsTabView {...s} />
        </TabsContent>

        <TabsContent value="instructions" className="space-y-4">
          <PermissionsInstructionsTabView {...s} />
        </TabsContent>

        <TabsContent value="skills" className="space-y-4">
          <PermissionsSkillsTabView {...s} />
        </TabsContent>

        <TabsContent value="connectors" className="space-y-4">
          <PermissionsConnectorsTabView {...s} />
        </TabsContent>
      </Tabs>

      <div className="min-h-5 text-sm">
        {error && <span className="text-destructive">{error}</span>}
        {!error && notice && <span className="text-muted-foreground">{notice}</span>}
      </div>
    </div>
  );
}