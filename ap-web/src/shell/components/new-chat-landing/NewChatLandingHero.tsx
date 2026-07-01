import { AgentMascotEyes } from "@/components/AgentMascotEyes";

export function NewChatLandingHero() {
  return (
    <div className="flex flex-col items-center gap-3.5 sm:flex-row">
      <AgentMascotEyes className="h-18 w-auto shrink-0" />
      <h1 className="text-center text-3xl font-medium text-foreground sm:text-left">
        What should we do?
      </h1>
    </div>
  );
}