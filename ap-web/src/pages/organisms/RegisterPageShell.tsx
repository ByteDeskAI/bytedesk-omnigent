import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { FormEvent } from "react";

const MIN_PASSWORD_LENGTH = 8;

export interface RegisterPageShellProps {
  missingInvite: boolean;
  username: string;
  password: string;
  confirm: string;
  submitting: boolean;
  error: string | null;
  onUsernameChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onConfirmChange: (value: string) => void;
  onSubmit: (e: FormEvent<HTMLFormElement>) => void;
}

export function RegisterPageShell({
  missingInvite,
  username,
  password,
  confirm,
  submitting,
  error,
  onUsernameChange,
  onPasswordChange,
  onConfirmChange,
  onSubmit,
}: RegisterPageShellProps) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Create your account</h1>
          <p className="text-sm text-muted-foreground">
            You were invited to join this Omnigent server.
          </p>
        </div>

        {missingInvite ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            This page needs an invite token in the URL — make sure you opened the link your admin
            sent you.
          </div>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="register-username" className="text-sm font-medium leading-none">
                Username
              </label>
              <Input
                id="register-username"
                type="text"
                autoComplete="username"
                value={username}
                onChange={(e) => onUsernameChange(e.target.value.toLowerCase())}
                disabled={submitting}
                required
                pattern="[a-z0-9][a-z0-9._\-]{0,63}(@[a-z0-9.\-]+\.[a-z]{2,})?"
                title="Lowercase letters, digits, dots, hyphens, underscores (or a lowercase email)"
              />
              <p className="text-xs text-muted-foreground">
                Lowercase letters, digits, dots, hyphens, underscores — or a lowercase email.
              </p>
            </div>

            <div className="space-y-1.5">
              <label htmlFor="register-password" className="text-sm font-medium leading-none">
                Password
              </label>
              <Input
                id="register-password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => onPasswordChange(e.target.value)}
                disabled={submitting}
                required
                minLength={MIN_PASSWORD_LENGTH}
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="register-confirm" className="text-sm font-medium leading-none">
                Confirm password
              </label>
              <Input
                id="register-confirm"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => onConfirmChange(e.target.value)}
                disabled={submitting}
                required
                minLength={MIN_PASSWORD_LENGTH}
              />
            </div>

            {error !== null && (
              <div
                role="alert"
                className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                {error}
              </div>
            )}

            <Button
              type="submit"
              className="w-full"
              disabled={submitting || password.length < MIN_PASSWORD_LENGTH || username.length === 0}
            >
              {submitting ? "Creating…" : "Create account"}
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}