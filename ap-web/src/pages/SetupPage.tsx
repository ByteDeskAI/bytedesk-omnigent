/**
 * First-run "Create admin" page (shown when ``/v1/info`` reports
 * ``needs_setup``).
 *
 * On a fresh accounts deploy no admin exists yet. The first visitor
 * claims it here by choosing a username + password; the server's
 * ``POST /auth/setup`` creates the admin (hard-gated to the
 * zero-admin state), sets the session cookie, and we navigate to
 * ``/`` signed in. This is the remote-deploy path (Docker / Render /
 * Railway) where there's no terminal to read a password from — and
 * locally the server auto-opens the browser straight here.
 *
 * Once an admin exists the server 409s ``/auth/setup`` and
 * ``needs_setup`` flips false, so App routes to LoginPage instead and
 * this page is never reachable.
 *
 * Mounted outside the AppShell (like Login/Register) — the chrome
 * needs an authenticated identity.
 *
 * Username constraints mirror the server regex
 * (``^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$``); the
 * form lowercases on input so a mixed-case value can't be rejected.
 */

import { useEffect, useState, type FormEvent } from "react";
import { setup as setupRequest } from "@/lib/accountsApi";
import { SetupPageShell } from "./organisms/SetupPageShell";

const MIN_PASSWORD_LENGTH = 8;

export function SetupPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = document.getElementById("setup-username");
    if (el instanceof HTMLInputElement) el.focus();
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (submitting) return;
    setError(null);

    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }

    setSubmitting(true);
    const result = await setupRequest({ username, password });
    if (result.ok) {
      window.location.href = "/";
      return;
    }
    setSubmitting(false);
    if (result.status === 409) {
      window.location.href = "/login";
      return;
    }
    setError(result.error);
  }

  return (
    <SetupPageShell
      username={username}
      password={password}
      confirm={confirm}
      submitting={submitting}
      error={error}
      onUsernameChange={setUsername}
      onPasswordChange={setPassword}
      onConfirmChange={setConfirm}
      onSubmit={(e) => void onSubmit(e)}
    />
  );
}