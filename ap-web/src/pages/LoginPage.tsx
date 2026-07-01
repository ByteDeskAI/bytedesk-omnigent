/**
 * The sign-in page for the ``accounts`` auth provider.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "@/lib/routing";
import {
  magicErrorMessage,
  readLastUsername,
  rememberUsername,
  sanitizeReturnTo,
} from "@/components/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getMe, login as loginRequest } from "@/lib/accountsApi";

export function LoginPage() {
  const [params] = useSearchParams();
  const returnTo = sanitizeReturnTo(params.get("return_to"));
  const magicError = params.get("magic");

  const [username, setUsername] = useState(readLastUsername);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(magicErrorMessage(magicError));

  useEffect(() => {
    void (async () => {
      const account = await getMe();
      if (account !== null) {
        window.location.href = returnTo;
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const targetId = username ? "login-password" : "login-username";
    const el = document.getElementById(targetId);
    if (el instanceof HTMLInputElement) {
      el.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);

    const result = await loginRequest({ username, password });
    if (result.ok) {
      rememberUsername(username);
      window.location.href = returnTo;
      return;
    }
    setSubmitting(false);
    setError(result.error);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="text-sm text-muted-foreground">Welcome to Omnigent.</p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="login-username" className="text-sm font-medium leading-none">
              Username
            </label>
            <Input
              id="login-username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              required
            />
            <p className="text-xs text-muted-foreground">
              On a fresh install your username is your machine login (the output of{" "}
              <code className="font-mono">whoami</code>), unless an admin set a different one.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="login-password" className="text-sm font-medium leading-none">
              Password
            </label>
            <Input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
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

          <Button type="submit" className="w-full" disabled={submitting || password.length === 0}>
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="text-center text-xs text-muted-foreground">
          On a fresh install the initial admin password was printed to the server's stderr and saved
          to{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono">
            ~/.omnigent/admin-credentials
          </code>
          .
        </p>
      </div>
    </div>
  );
}