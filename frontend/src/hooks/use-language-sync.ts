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

  // Priority 1: Apply language from API response whenever user data arrives.
  // If a cross-tab storage event fires before user data loads, the refetch
  // triggered by invalidateQueries will return the updated language from the
  // API, so this effect self-corrects any transient mismatch.
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
        const { supportedLngs } = i18n.options;
        if (
          Array.isArray(supportedLngs) &&
          !supportedLngs.includes(event.newValue)
        ) {
          return;
        }

        i18n.changeLanguage(event.newValue);
        queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
      }
    },
    [i18n, queryClient],
  );

  useCrossTabState(handleStorageChange);
}
