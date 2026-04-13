import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useMe } from "#/hooks/use-me";
import OpenHandsService from "#/api/openhands-service";
import { useUserStore } from "#/stores/user-store";
import type { User } from "#/types/user";

const mockUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: ["manage_secrets"],
};

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

describe("useMe", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useUserStore.setState({ user: null, isInitialized: false });
  });

  it("fetches user and populates store when enabled", async () => {
    vi.spyOn(OpenHandsService, "getMe").mockResolvedValue(mockUser);

    const { result } = renderHook(() => useMe(true), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toEqual(mockUser);
    expect(useUserStore.getState().user).toEqual(mockUser);
    expect(useUserStore.getState().isInitialized).toBe(true);
  });

  it("does not fetch when disabled", () => {
    const spy = vi.spyOn(OpenHandsService, "getMe");

    renderHook(() => useMe(false), {
      wrapper: createWrapper(),
    });

    expect(spy).not.toHaveBeenCalled();
  });
});
