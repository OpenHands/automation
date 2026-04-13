import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useCrossTabState } from "#/hooks/use-cross-tab-state";

describe("useCrossTabState", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("calls onStorageChange when a storage event fires", () => {
    const onStorage = vi.fn();
    const onFocus = vi.fn();

    renderHook(() => useCrossTabState(onStorage, onFocus));

    const event = new StorageEvent("storage", { key: "test_key" });
    act(() => {
      window.dispatchEvent(event);
    });

    expect(onStorage).toHaveBeenCalledWith(event);
    expect(onFocus).not.toHaveBeenCalled();
  });

  it("calls onWindowFocus when the window gains focus", () => {
    const onStorage = vi.fn();
    const onFocus = vi.fn();

    renderHook(() => useCrossTabState(onStorage, onFocus));

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(onFocus).toHaveBeenCalledTimes(1);
    expect(onStorage).not.toHaveBeenCalled();
  });

  it("cleans up listeners on unmount", () => {
    const onStorage = vi.fn();
    const onFocus = vi.fn();

    const { unmount } = renderHook(() => useCrossTabState(onStorage, onFocus));
    unmount();

    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "k" }));
      window.dispatchEvent(new Event("focus"));
    });

    expect(onStorage).not.toHaveBeenCalled();
    expect(onFocus).not.toHaveBeenCalled();
  });
});
