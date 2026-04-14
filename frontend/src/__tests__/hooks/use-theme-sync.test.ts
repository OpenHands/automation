import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useThemeSync } from "#/hooks/use-theme-sync";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";

describe("useThemeSync", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    document.documentElement.className = "";
  });

  it("adds dark class on mount when no theme key in localStorage", () => {
    renderHook(() => useThemeSync());

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("adds dark class on mount when localStorage value is dark", () => {
    localStorage.setItem(LOCAL_STORAGE_KEYS.THEME, "dark");

    renderHook(() => useThemeSync());

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("removes dark class on mount when localStorage value is light", () => {
    document.documentElement.classList.add("dark");
    localStorage.setItem(LOCAL_STORAGE_KEYS.THEME, "light");

    renderHook(() => useThemeSync());

    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("removes dark class on storage event with light value", () => {
    document.documentElement.classList.add("dark");

    renderHook(() => useThemeSync());

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.THEME,
          newValue: "light",
        }),
      );
    });

    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("adds dark class on storage event with dark value", () => {
    renderHook(() => useThemeSync());

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.THEME,
          newValue: "dark",
        }),
      );
    });

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("defaults to dark on storage event with null value", () => {
    document.documentElement.className = "";

    renderHook(() => useThemeSync());

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.THEME,
          newValue: null,
        }),
      );
    });

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("ignores storage events for unrelated keys", () => {
    document.documentElement.classList.add("dark");

    renderHook(() => useThemeSync());

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "some_other_key",
          newValue: "light",
        }),
      );
    });

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("re-reads localStorage on window focus", () => {
    document.documentElement.classList.add("dark");
    localStorage.setItem(LOCAL_STORAGE_KEYS.THEME, "light");

    renderHook(() => useThemeSync());

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
