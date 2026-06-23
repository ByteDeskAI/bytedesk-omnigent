import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  isSecretValue,
  useConfigDescriptors,
  useConfigValue,
} from "./useConfigDescriptors";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrapperWith(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe("useConfigDescriptors", () => {
  it("GETs /v1/config/descriptors and unwraps the data array", async () => {
    const descriptors = [
      { key: "system.log_level", scope: "system", tier: 2 },
      { key: "system.nats.url", scope: "system", tier: 0 },
    ];
    fetchMock.mockResolvedValueOnce(mockResponse({ data: descriptors }));
    const { result } = renderHook(() => useConfigDescriptors(), {
      wrapper: wrapperWith(client()),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/config/descriptors");
    expect(result.current.data).toEqual(descriptors);
  });

  it("surfaces a non-OK response as an error", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    const { result } = renderHook(() => useConfigDescriptors(), {
      wrapper: wrapperWith(client()),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe("useConfigValue", () => {
  it("GETs the url-encoded value endpoint for a key", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ key: "system.log_level", value: "INFO", etag: "1" }),
    );
    const { result } = renderHook(() => useConfigValue("system.log_level"), {
      wrapper: wrapperWith(client()),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/config/values/system.log_level");
    expect(result.current.data?.value).toBe("INFO");
  });

  it("does not fetch when the key is empty", () => {
    renderHook(() => useConfigValue(""), { wrapper: wrapperWith(client()) });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("isSecretValue", () => {
  it("recognizes the name+presence secret shape", () => {
    expect(isSecretValue({ name: "x", present: true, source: "env" })).toBe(true);
  });

  it("rejects scalars and plain objects", () => {
    expect(isSecretValue("INFO")).toBe(false);
    expect(isSecretValue(42)).toBe(false);
    expect(isSecretValue(null)).toBe(false);
    expect(isSecretValue({ other: 1 })).toBe(false);
  });
});
