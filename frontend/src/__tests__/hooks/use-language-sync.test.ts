import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { useLanguageSync } from "#/hooks/use-language-sync";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";
import type { User } from "#/types/user";

const changeLanguageMock = vi.fn();

vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: {
      language: "en",
      changeLanguage: changeLanguageMock,
      exists: () => false,
      options: {
        supportedLngs: ["en", "ja", "zh-CN", "fr", "de"],
      },
    },
  }),
}));

const mockUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: [],
  language: "ja",
};

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

describe("useLanguageSync", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    changeLanguageMock.mockReset();
  });

  it("calls i18n.changeLanguage with user language when user data arrives", async () => {
    const { Wrapper } = createWrapper();

    renderHook(() => useLanguageSync(mockUser), { wrapper: Wrapper });

    await waitFor(() => {
      expect(changeLanguageMock).toHaveBeenCalledWith("ja");
    });
  });

  it("does not call i18n.changeLanguage when user has no language field", () => {
    const { Wrapper } = createWrapper();
    const userWithoutLanguage: User = { ...mockUser, language: undefined };

    renderHook(() => useLanguageSync(userWithoutLanguage), {
      wrapper: Wrapper,
    });

    expect(changeLanguageMock).not.toHaveBeenCalled();
  });

  it("does not call i18n.changeLanguage when user language matches current language", () => {
    const { Wrapper } = createWrapper();
    const sameLanguageUser: User = { ...mockUser, language: "en" };

    renderHook(() => useLanguageSync(sameLanguageUser), { wrapper: Wrapper });

    expect(changeLanguageMock).not.toHaveBeenCalled();
  });

  it("applies language from storage event and invalidates the me query", () => {
    const { Wrapper, queryClient } = createWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderHook(() => useLanguageSync(undefined), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.I18N_LANGUAGE,
          newValue: "fr",
        }),
      );
    });

    expect(changeLanguageMock).toHaveBeenCalledWith("fr");
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: ["user", "me"] }),
    );
  });

  it("ignores storage events for unrelated keys", () => {
    const { Wrapper } = createWrapper();

    renderHook(() => useLanguageSync(undefined), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "some_other_key",
          newValue: "de",
        }),
      );
    });

    expect(changeLanguageMock).not.toHaveBeenCalled();
  });

  it("ignores storage events with unsupported language codes", () => {
    const { Wrapper } = createWrapper();

    renderHook(() => useLanguageSync(undefined), { wrapper: Wrapper });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: LOCAL_STORAGE_KEYS.I18N_LANGUAGE,
          newValue: "invalid-lang-xyz",
        }),
      );
    });

    expect(changeLanguageMock).not.toHaveBeenCalled();
  });
});
