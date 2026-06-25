import { describe, expect, it, vi } from "vitest";
import { handleNotificationClick, handlePushPayload } from "@/lib/pwa/swHandlers";

const ORIGIN = "https://app.test";

describe("handlePushPayload", () => {
  it("shows a notification for valid push data", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);
    const raw = {
      sessionId: "conv_abc",
      kind: "turn_end",
      url: "/c/conv_abc",
      title: "Agent ready",
      body: "Turn complete",
    };

    const result = await handlePushPayload(raw, {
      origin: ORIGIN,
      matchClients: async () => [],
      showNotification,
    });

    expect(result).toEqual({
      shown: true,
      title: "Agent ready",
      body: "Turn complete",
    });
    expect(showNotification).toHaveBeenCalledWith("Agent ready", {
      body: "Turn complete",
      tag: "omnigent:conv_abc:turn_end",
      data: {
        sessionId: "conv_abc",
        kind: "turn_end",
        url: "/c/conv_abc",
      },
    });
  });

  it("suppresses notification when a focused client is on the session", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);
    const raw = {
      sessionId: "conv_abc",
      kind: "elicitation",
      url: "/c/conv_abc",
    };

    const result = await handlePushPayload(raw, {
      origin: ORIGIN,
      matchClients: async () => [
        {
          focused: true,
          visibilityState: "visible",
          url: `${ORIGIN}/c/conv_abc`,
        },
      ],
      showNotification,
    });

    expect(result).toEqual({ shown: false });
    expect(showNotification).not.toHaveBeenCalled();
  });

  it("returns shown false for invalid push payloads", async () => {
    const showNotification = vi.fn().mockResolvedValue(undefined);

    const result = await handlePushPayload({ bad: true }, {
      origin: ORIGIN,
      matchClients: async () => [],
      showNotification,
    });

    expect(result).toEqual({ shown: false });
    expect(showNotification).not.toHaveBeenCalled();
  });
});

describe("handleNotificationClick", () => {
  it("focuses and navigates an existing same-origin client", async () => {
    const focus = vi.fn().mockResolvedValue(undefined);
    const navigate = vi.fn().mockResolvedValue(undefined);
    const openWindow = vi.fn();

    const absolute = await handleNotificationClick(
      { sessionId: "conv_xyz", kind: "turn_end", url: "/c/conv_xyz" },
      {
        origin: ORIGIN,
        matchClients: async () => [
          {
            focused: false,
            visibilityState: "hidden",
            url: `${ORIGIN}/settings`,
            focus,
            navigate,
          },
        ],
        openWindow,
      },
    );

    expect(absolute).toBe(`${ORIGIN}/c/conv_xyz`);
    expect(focus).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith(`${ORIGIN}/c/conv_xyz`);
    expect(openWindow).not.toHaveBeenCalled();
  });

  it("opens a new window when no matching client exists", async () => {
    const openWindow = vi.fn().mockResolvedValue(null);

    const absolute = await handleNotificationClick(
      { sessionId: "conv_new", kind: "elicitation", url: "/c/conv_new" },
      {
        origin: ORIGIN,
        matchClients: async () => [],
        openWindow,
      },
    );

    expect(absolute).toBe(`${ORIGIN}/c/conv_new`);
    expect(openWindow).toHaveBeenCalledWith(`${ORIGIN}/c/conv_new`);
  });
});