/**
 * Backoff schedule for automatic re-attach after a transport-level
 * close ({@link isUnexpectedTerminalClose}). One entry per attempt;
 * when the schedule is exhausted the closed overlay stays up and the
 * user falls back to a manual refresh / resume.
 *
 * Exported for direct unit testing (fake timers advance through it).
 */
export const RECONNECT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000] as const;

/**
 * A connection that stayed open at least this long before dropping is
 * treated as a fresh outage: the retry budget resets. Without this, a
 * terminal that reconnects fine but drops again hours later (another
 * background-tab freeze) would eventually exhaust the budget; with a
 * plain reset-on-connect, a connect→drop hot loop would retry forever.
 */
export const RECONNECT_STABLE_MS = 30_000;