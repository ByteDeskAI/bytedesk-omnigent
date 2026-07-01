/**
 * The invite-redemption page (``/register?invite=...``).
 *
 * Reached by clicking the copyable URL an admin minted in the
 * Members page. The user chooses their own username + password,
 * the server consumes the invite + creates the account + sets the
 * session cookie, and we navigate to ``/``.
 *
 * Mounted outside the AppShell for the same reason LoginPage is —
 * the chrome loads sidebar / conversations / runner hooks that
 * require an authenticated identity.
 *
 * Username constraints are intentionally restrictive to match the
 * server's validation regex
 * (``^[a-z0-9][a-z0-9._-]{0,63}(@[a-z0-9.-]+\.[a-z]{2,})?$``).
 * The form lowercases on input so the user can't accidentally
 * type a mixed-case value that the server then rejects.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "@/lib/routing";
import { register as registerRequest } from "@/lib/accountsApi";
import { RegisterPageShell } from "./organisms/RegisterPageShell";

const MIN_PASSWORD_LENGTH = 8;

export function RegisterPage() {
  const [params] = useSearchParams();
  const invite = params.get("invite") ?? "";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const missingInvite = invite === "";

  useEffect(() => {
    const el = document.getElementById("register-username");
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
    const result = await registerRequest({ invite, username, password });
    if (result.ok) {
      window.location.href = "/";
      return;
    }
    setSubmitting(false);
    setError(result.error);
  }

  return (
    <RegisterPageShell
      missingInvite={missingInvite}
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