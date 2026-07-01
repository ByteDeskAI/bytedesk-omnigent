import { Link } from "@/lib/routing";
import {
  BotIcon,
  CalendarClockIcon,
  GaugeIcon,
  KeyRoundIcon,
  LogOutIcon,
  PlugIcon,
  PuzzleIcon,
  SettingsIcon,
  ShieldCheckIcon,
  SquareTerminalIcon,
  TargetIcon,
  UserCogIcon,
  UsersIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { CurrentAccount } from "@/lib/accountsApi";

export interface AccountsMenuDropdownProps {
  me: CurrentAccount;
  terminalEnabled: boolean;
  onOpenChangePassword: () => void;
  onSignOut: () => void;
}

export function AccountsMenuDropdown({
  me,
  terminalEnabled,
  onOpenChangePassword,
  onSignOut,
}: AccountsMenuDropdownProps) {
  return (
    <div className="shrink-0">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-auto w-full justify-start gap-2 px-3 py-2"
          >
            <span className="flex size-6 shrink-0 items-center justify-center rounded-md border border-border">
              <UserCogIcon className="size-3.5" />
            </span>
            <span className="min-w-0 flex-1 truncate text-left">{me.id}</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" side="top" className="mx-2 w-auto min-w-48">
          <DropdownMenuLabel>
            {me.id}
            {me.is_admin && (
              <span className="ml-1 text-xs font-normal text-muted-foreground">(admin)</span>
            )}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <Link to="/skills" className="flex items-center gap-2">
              <PuzzleIcon /> Skills
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
          {me.is_admin && (
            <>
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
                <Link to="/members" className="flex items-center gap-2">
                  <UsersIcon /> Members
                </Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link to="/policies" className="flex items-center gap-2">
                  <ShieldCheckIcon /> Policies
                </Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link to="/config" className="flex items-center gap-2">
                  <SettingsIcon /> Configuration
                </Link>
              </DropdownMenuItem>
              {terminalEnabled && (
                <DropdownMenuItem asChild>
                  <Link to="/terminal" className="flex items-center gap-2">
                    <SquareTerminalIcon /> Terminal
                  </Link>
                </DropdownMenuItem>
              )}
            </>
          )}
          <DropdownMenuItem onClick={onOpenChangePassword} className="flex items-center gap-2">
            <KeyRoundIcon /> Change password
          </DropdownMenuItem>
          <DropdownMenuItem onClick={onSignOut} className="flex items-center gap-2">
            <LogOutIcon /> Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}