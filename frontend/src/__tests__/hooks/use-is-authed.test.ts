import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { AxiosError, AxiosHeaders } from "axios";
import React from "react";
import { useIsAuthed } from "#/hooks/use-is-authed";
import OpenHandsService from "#/api/openhands-service";

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

describe("useIsAuthed", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns true when authentication succeeds", async () => {
    vi.spyOn(OpenHandsService, "authenticate").mockResolvedValue(true);

    const { result } = renderHook(() => useIsAuthed(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toBe(true);
  });

  it("returns false when authentication returns 401", async () => {
    const error = new AxiosError(
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
    vi.spyOn(OpenHandsService, "authenticate").mockRejectedValue(error);

    const { result } = renderHook(() => useIsAuthed(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toBe(false);
  });

  it("enters error state on non-401 errors", async () => {
    vi.spyOn(OpenHandsService, "authenticate").mockRejectedValue(
      new Error("Network error"),
    );

    const { result } = renderHook(() => useIsAuthed(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
