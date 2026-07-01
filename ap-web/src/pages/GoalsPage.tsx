import { GoalsPageShell } from "./organisms/GoalsPageShell";
import { useGoalsPage } from "./useGoalsPage";

export function GoalsPage() {
  return <GoalsPageShell {...useGoalsPage()} />;
}