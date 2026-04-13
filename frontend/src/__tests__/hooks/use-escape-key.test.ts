import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useEscapeKey } from "#/hooks/use-escape-key";

describe("useEscapeKey", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("calls the callback when the Escape key is pressed", () => {
    const onEscape = vi.fn();
    renderHook(() => useEscapeKey(onEscape));

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });

    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("does not call the callback for other keys", () => {
    const onEscape = vi.fn();
    renderHook(() => useEscapeKey(onEscape));

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    });

    expect(onEscape).not.toHaveBeenCalled();
  });

  it("does not throw when callback is undefined", () => {
    renderHook(() => useEscapeKey(undefined));

    expect(() => {
      act(() => {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      });
    }).not.toThrow();
  });

  it("cleans up the listener on unmount", () => {
    const onEscape = vi.fn();
    const { unmount } = renderHook(() => useEscapeKey(onEscape));
    unmount();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });

    expect(onEscape).not.toHaveBeenCalled();
  });
});
