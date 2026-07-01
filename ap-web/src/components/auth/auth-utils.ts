export const DEFAULT_RETURN_TO = "/";
export const LAST_USERNAME_KEY = "omnigent.lastLoginUsername";

export function readLastUsername(): string {
  try {
    return window.localStorage.getItem(LAST_USERNAME_KEY) ?? "";
  } catch {
    // localStorage can throw in sandboxed iframes / blocked-cookies mode.
    return "";
  }
}

export function rememberUsername(value: string): void {
  try {
    window.localStorage.setItem(LAST_USERNAME_KEY, value);
  } catch {
    // Best-effort — see readLastUsername.
  }
}

/**
 * Reject anything that isn't a relative path on the same origin.
 *
 * Defense against an open-redirect via crafted ``?return_to=`` —
 * an attacker who can get a victim to click a link to
 * ``/login?return_to=https://evil.com`` would otherwise have us
 * land them on the attacker's page after auth.
 */
export function sanitizeReturnTo(raw: string | null): string {
  if (raw === null || raw === "") return DEFAULT_RETURN_TO;
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) {
    return DEFAULT_RETURN_TO;
  }
  try {
    const resolved = new URL(raw, window.location.origin);
    if (resolved.origin !== window.location.origin) return DEFAULT_RETURN_TO;
    return resolved.pathname + resolved.search + resolved.hash;
  } catch {
    return DEFAULT_RETURN_TO;
  }
}

export function magicErrorMessage(magicError: string | null): string | null {
  if (magicError === "expired") {
    return "That sign-in link has expired. Enter your password to sign in.";
  }
  if (magicError === "missing") {
    return "That sign-in link is no longer valid. Enter your password to sign in.";
  }
  return null;
}