import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setOmnigentHostConfig } from "@/lib/host";
import { setAppBadgeCount } from "@/lib/pwa/badging";
import {
  captureInstallPrompt,
  clearDeferredInstallPrompt,
  getDeferredInstallPrompt,
} from "@/lib/pwa/installPrompt";
import { manifestShortcuts, shareTarget } from "@/lib/pwa/manifestConfig";
import { readNavigatorOnline, shouldDisableNetworkActions } from "@/lib/pwa/offline";
import {
  buildPushNotificationPayload,
  parsePushEventData,
  shouldSuppressPushForFocusedClient,
} from "@/lib/pwa/pushPayload";
import { isEmbedMode, shouldRegisterPwa } from "@/lib/pwa/runtime";
import {
  buildComposerPrefillFromShare,
  hasShareTargetContent,
  parseShareTargetSearch,
} from "@/lib/pwa/shareTarget";
import { shareMessageContent } from "@/lib/pwa/shareMessage";

describe("pwa offline", () => {
  it("disables network actions when offline", () => {
    expect(shouldDisableNetworkActions(false)).toBe(true);
    expect(shouldDisableNetworkActions(true)).toBe(false);
  });

  it("defaults navigator online to true in jsdom", () => {
    expect(readNavigatorOnline()).toBe(true);
  });
});

describe("share target parser", () => {
  it("parses inbound share params and builds composer prefill", () => {
    const params = parseShareTargetSearch("?title=Hello&text=World&url=https://x.test/a");
    expect(hasShareTargetContent(params)).toBe(true);
    expect(buildComposerPrefillFromShare(params)).toContain("Hello");
    expect(buildComposerPrefillFromShare(params)).toContain("https://x.test/a");
  });
});

describe("push payload", () => {
  it("builds notification routes and parses push data", () => {
    const payload = buildPushNotificationPayload({
      sessionId: "conv_abc",
      title: "Test",
      body: "Ready",
      kind: "turn_end",
    });
    expect(payload.url).toBe("/c/conv_abc");
    expect(parsePushEventData(payload)?.sessionId).toBe("conv_abc");
  });

  it("suppresses push when focused client is on the session", () => {
    const suppressed = shouldSuppressPushForFocusedClient(
      [
        {
          focused: true,
          visibilityState: "visible",
          url: "https://app.test/c/conv_abc",
        },
      ],
      "conv_abc",
      "https://app.test",
    );
    expect(suppressed).toBe(true);
  });
});

describe("manifest config", () => {
  it("includes shortcuts and share target action", () => {
    expect(manifestShortcuts.length).toBeGreaterThanOrEqual(3);
    expect(shareTarget.action).toBe("/share");
  });
});

describe("install prompt capture", () => {
  it("stores deferred beforeinstallprompt event", () => {
    clearDeferredInstallPrompt();
    const fake = { preventDefault: () => undefined } as unknown as Event;
    captureInstallPrompt(fake);
    expect(getDeferredInstallPrompt()).toBe(fake);
  });
});

describe("runtime gating", () => {
  beforeEach(() => {
    setOmnigentHostConfig({});
  });

  afterEach(() => {
    setOmnigentHostConfig({});
  });

  it("registers PWA in standalone mode without host fetcher", () => {
    expect(isEmbedMode()).toBe(false);
    expect(shouldRegisterPwa()).toBe(true);
  });

  it("does not register PWA when host fetcher is installed", () => {
    setOmnigentHostConfig({
      fetcher: async () => new Response(null, { status: 200 }),
    });
    expect(isEmbedMode()).toBe(true);
    expect(shouldRegisterPwa()).toBe(false);
  });
});

describe("badging fallback", () => {
  it("does not throw when Badging API is absent", async () => {
    await expect(setAppBadgeCount(2)).resolves.toBeUndefined();
    await expect(setAppBadgeCount(0)).resolves.toBeUndefined();
  });
});

describe("shareMessageContent", () => {
  it("falls back to clipboard when navigator.share is missing", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const result = await shareMessageContent({
      title: "T",
      text: "body",
      url: "https://example.test/c/1",
    });
    expect(result).toBe("copied");
    expect(writeText).toHaveBeenCalledWith("https://example.test/c/1");
  });
});