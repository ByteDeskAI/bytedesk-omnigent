import { SchedulesPageShell } from "./organisms/SchedulesPageShell";
import { useSchedulesPageState } from "./organisms/useSchedulesPageState";

export function SchedulesPage() {
  const shellProps = useSchedulesPageState();
  return <SchedulesPageShell {...shellProps} />;
}