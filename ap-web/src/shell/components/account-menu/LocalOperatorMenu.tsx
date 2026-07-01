import { Link } from "@/lib/routing";
import {
  BotIcon,
  CalendarClockIcon,
  GaugeIcon,
  PlugIcon,
  PuzzleIcon,
  SquareTerminalIcon,
  TargetIcon,
  UserCogIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export function LocalOperatorMenu({ terminalEnabled }: { terminalEnabled: boolean }) {
  return (
    <div className="shrink-0">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" className="h-auto w-full justify-start gap-2 px-3 py-2">
            <span className="flex size-6 shrink-0 items-center justify-center rounded-md border border-border">
              <UserCogIcon className="size-3.5" />
            </span>
            <span className="min-w-0 flex-1 truncate text-left">Omnigent</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" side="top" className="mx-2 w-auto min-w-48">
          <DropdownMenuItem asChild>
            <Link to="/skills" className="flex items-center gap-2">
              <PuzzleIcon /> Skills
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/work-force" className="flex items-center gap-2">
              <BotIcon /> Work Force
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/connectors" className="flex items-center gap-2">
              <PlugIcon /> Connectors
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/goals" className="flex items-center gap-2">
              <TargetIcon /> Goals
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/command-center" className="flex items-center gap-2">
              <GaugeIcon /> Command Center
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/schedules" className="flex items-center gap-2">
              <CalendarClockIcon /> Schedules
            </Link>
          </DropdownMenuItem>
          {terminalEnabled && (
            <DropdownMenuItem asChild>
              <Link to="/terminal" className="flex items-center gap-2">
                <SquareTerminalIcon /> Terminal
              </Link>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}