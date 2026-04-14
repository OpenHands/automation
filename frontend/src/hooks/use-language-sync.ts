import React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useCrossTabState } from "./use-cross-tab-state";
import { ME_QUERY_KEY } from "./use-me";
import { LOCAL_STORAGE_KEYS } from "#/utils/local-storage";
import type { User } from "#/types/user";

/**
 * Synchronises the active i18n language with:
 *  1. The language from the user profile (GET /api/v1/users/me).
 *  2. Changes to the `i18nextLng` localStorage key made in other tabs.
 *
 * Priority: API response > localStorage (cross-tab) > browser detection
 * (browser detection is handled by i18next-browser-languagedetector at init).
 */
export function useLanguageSync(user: User | undefined) {
  const { i18n } = useTranslation();
  const queryClient = useQueryClient();

  React.useEffect(() => {
    if (user?.language && user.language !== i18n.language) {
      i18n.changeLanguage(user.language);
    }
  }, [user?.language, i18n]);

  const handleStorageChange = React.useCallback(
    (event: StorageEvent) => {
      if (
        event.key === LOCAL_STORAGE_KEYS.I18N_LANGUAGE &&
        event.newValue &&
        event.newValue !== i18n.language
      ) {
        i18n.changeLanguage(event.newValue);
        queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
      }
    },
    [i18n, queryClient],
  );

  const handleWindowFocus = React.useCallback(() => {}, []);

  useCrossTabState(handleStorageChange, handleWindowFocus);
}
