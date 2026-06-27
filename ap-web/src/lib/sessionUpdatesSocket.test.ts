import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hostFetch } from "@/lib/host";
import { HEARTBEAT_WATCHDOG_MS, sessionUpdatesSocket } from "./sessionUpdatesSocket";

// The socket now fetches a short-TTL ws-ticket over HTTP before constructing the
// WebSocket (BDP-2513) so the cross-site iframe can authenticate the handshake.
// Mock that fetch so construction is deterministic; resolveWebSocketUrl is
// stubbed to a fixed origin. Default per-test: empty ticket → bare URL.
vi.mock("@/lib/host", () => ({
  resolveWebSocketUrl: (path: string) => `ws://localhost${path}`,
  hostFetch: vi.fn(),
}));

const mockHostFetch = vi.mocked(hostFetch);

/** Flush the awaited ws-ticket fetch chain so openSocket constructs the socket. */
async function flushConnect(): Promise<void> {
  for (let i = 0; i < 4; i++) await Promise.resolve();
}

// Minimal stand-in for the browser WebSocket: records sends/closes and lets
// the test drive the lifecycle (open, message) by hand. A real socket can't be
// opened in jsdom, and we need deterministic control over when frames arrive
// relative to the watchdog deadline — so this is the transport-level mock the
// testing guide allows.
class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readyState = 0; // CONNECTING
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closeCount = 0;
  readonly url: string;

  constructor(url: string) {
    // Plain field assignment, not a TS parameter property — the latter is
    // forbidden by `erasableSyntaxOnly` in tsconfig.app.json.
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(): void {
    // The watch-set send is irrelevant to the watchdog; ignore it.
  }

  close(): void {
    this.closeCount += 1;
    this.readyState = 3; // CLOSED
    this.onclose?.();
  }

  /** Test helper: complete the handshake (fires the socket's onopen). */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  /** Test helper: deliver one server text frame. */
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

/** The socket constructed most recently by start()/reconnect. */
function latestWs(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1);
  if (!ws) throw new Error("no WebSocket was constructed");
  return ws;
}

describe("sessionUpdatesSocket heartbeat watchdog", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
    // Default: no ticket (header / local-style) → connect to the bare URL.
    mockHostFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ ticket: "" }),
    } as unknown as Response);
  });

  afterEach(() => {
    // Tear down the shared singleton's connection + timers so cases don't leak
    // into each other, then restore real timers/globals.
    sessionUpdatesSocket.stop();
    vi.clearAllTimers();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("forces a reconnect after the watchdog window of total silence", async () => {
    sessionUpdatesSocket.start();
    await flushConnect();
    const ws = latestWs();
    ws.open();
    expect(sessionUpdatesSocket.isConnected()).toBe(true);

    // One tick short of the deadline: still alive, not closed.
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1);
    expect(ws.closeCount).toBe(0);
    expect(sessionUpdatesSocket.isConnected()).toBe(true);

    // Crossing the deadline with zero frames trips the watchdog, which closes
    // the dead socket; onclose flips us to disconnected so consumers resume
    // their HTTP fallback poll.
    vi.advanceTimersByTime(1);
    expect(ws.closeCount).toBe(1);
    expect(sessionUpdatesSocket.isConnected()).toBe(false);

    // The close scheduled a reconnect; after the (jittered, ≤5 s) backoff a
    // fresh socket is constructed — the stream tries to come back, it doesn't
    // just give up.
    const before = FakeWebSocket.instances.length;
    // Async advance: firing the reconnect timer kicks off openSocket's awaited
    // ticket fetch, so the fresh socket is constructed on the flushed microtasks.
    await vi.advanceTimersByTimeAsync(RECONNECT_CEILING_MS);
    expect(FakeWebSocket.instances.length).toBe(before + 1);
  });

  it("keeps the connection alive when a heartbeat arrives before the deadline", async () => {
    sessionUpdatesSocket.start();
    await flushConnect();
    const ws = latestWs();
    ws.open();

    // A heartbeat just before the deadline must reset the watchdog...
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1);
    ws.emit({ type: "heartbeat" });

    // ...so advancing nearly another full window still doesn't close it. If the
    // watchdog hadn't reset, this second advance would have tripped it.
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1);
    expect(ws.closeCount).toBe(0);
    expect(sessionUpdatesSocket.isConnected()).toBe(true);

    // It still fires on a genuine stall after the last frame.
    vi.advanceTimersByTime(1);
    expect(ws.closeCount).toBe(1);
    expect(sessionUpdatesSocket.isConnected()).toBe(false);
  });

  it("attaches the ws-ticket to the handshake URL when one is issued", async () => {
    mockHostFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ ticket: "tkt-123" }),
    } as unknown as Response);

    sessionUpdatesSocket.start();
    await flushConnect();

    expect(mockHostFetch).toHaveBeenCalledWith("/v1/auth/ws-ticket");
    expect(latestWs().url).toContain("ticket=tkt-123");
  });

  it("connects without a ticket when the ws-ticket fetch fails", async () => {
    // Best-effort: a failed ticket fetch must never block the socket (the
    // cookie / proxy identity path still applies where it works).
    mockHostFetch.mockRejectedValueOnce(new Error("network"));

    sessionUpdatesSocket.start();
    await flushConnect();

    expect(latestWs().url).not.toContain("ticket=");
  });
});

// Reconnect backoff is capped at 5 s + jitter; advancing past 5 s guarantees
// the scheduled reconnect timer has fired regardless of the random jitter.
const RECONNECT_CEILING_MS = 5_001;
