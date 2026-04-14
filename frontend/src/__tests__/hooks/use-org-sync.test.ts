import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useOrgSync } from "#/hooks/use-org-sync";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    queryClient,
    Wrapper: function Wrapper({ children }: { children: React.ReactNode }) {
      return React.createElement(
        QueryClientProvider,
        { client: queryClient },
        children,
      );
    },
  };
}

describe("useOrgSync", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("invalidates all queries on storage event with different org", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderHook(() => useOrgSync("o1"), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.SELECTED_ORG,
          newValue: "o2",
        }),
      );
    });

    expect(invalidateSpy).toHaveBeenCalled();
  });

  it("does not invalidate when storage event has same org as current", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderHook(() => useOrgSync("o1"), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.SELECTED_ORG,
          newValue: "o1",
        }),
      );
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("does not invalidate for unrelated storage keys", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderHook(() => useOrgSync("o1"), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "some_other_key",
          newValue: "o2",
        }),
      );
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("does not invalidate when newValue is null", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderHook(() => useOrgSync("o1"), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.SELECTED_ORG,
          newValue: null,
        }),
      );
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("invalidates on window focus when stored org differs from current", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    localStorage.setItem(LOCAL_STORAGE_KEYS.SELECTED_ORG, "o2");

    renderHook(() => useOrgSync("o1"), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(invalidateSpy).toHaveBeenCalled();
  });

  it("does not invalidate on window focus when stored org matches current", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    localStorage.setItem(LOCAL_STORAGE_KEYS.SELECTED_ORG, "o1");

    renderHook(() => useOrgSync("o1"), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
