import { SkillsPageShell } from "./organisms/SkillsPageShell";
import { useSkillsPage } from "./useSkillsPage";

export function SkillsPage() {
  return <SkillsPageShell {...useSkillsPage()} />;
}