import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { AxiosError, AxiosHeaders } from "axios";
import { useAutoLogin } from "#/hooks/use-auto-login";
import OpenHandsService from "#/api/openhands-service";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(
      QueryClientProvider,
      { client: queryClient },
      children,
    );
  };
}

const error401 = new AxiosError(
  "Unauthorized",
  "ERR_BAD_REQUEST",
  undefined,
  undefined,
  {
    status: 401,
    data: { error: "User is not authenticated" },
    statusText: "Unauthorized",
    headers: {},
    config: { headers: new AxiosHeaders() },
  },
);

describe("useAutoLogin", () => {
  let hrefSetter: ReturnType<typeof vi.fn<(url: string) => void>>;

  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    hrefSetter = vi.fn<(url: string) => void>();
    Object.defineProperty(window, "location", {
      value: {
        hostname: "app.all-hands.dev",
        host: "app.all-hands.dev",
        protocol: "https:",
        pathname: "/automations",
        search: "",
        get href() {
          return "https://app.all-hands.dev/automations";
        },
        set href(url: string) {
          hrefSetter(url);
        },
      },
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("redirects to OAuth provider when unauthenticated with stored login method", async () => {
    localStorage.setItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD, "github");
    vi.spyOn(OpenHandsService, "authenticate").mockRejectedValue(error401);

    renderHook(() => useAutoLogin(), { wrapper: createWrapper() });

    await waitFor(() => {
      expect(hrefSetter).toHaveBeenCalled();
    });

    const redirectUrl = hrefSetter.mock.calls[0][0] as string;
    expect(redirectUrl).toContain("kc_idp_hint=github");
    expect(redirectUrl).toContain("login_method=github");
  });

  it("does not redirect when authenticated", async () => {
    localStorage.setItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD, "github");
    vi.spyOn(OpenHandsService, "authenticate").mockResolvedValue(true);

    renderHook(() => useAutoLogin(), { wrapper: createWrapper() });

    await waitFor(() => {
      expect(hrefSetter).not.toHaveBeenCalled();
    });
  });

  it("does not redirect when no login method is stored", async () => {
    vi.spyOn(OpenHandsService, "authenticate").mockRejectedValue(error401);

    renderHook(() => useAutoLogin(), { wrapper: createWrapper() });

    await waitFor(() => {
      expect(hrefSetter).not.toHaveBeenCalled();
    });
  });
});
