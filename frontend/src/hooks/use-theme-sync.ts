import React from "react";
import { useCrossTabState } from "./use-cross-tab-state";
import { LOCAL_STORAGE_KEYS, getTheme } from "#/utils/local-storage";
import type { Theme } from "#/utils/local-storage";

/**
 * Synchronises the dark/light theme across browser tabs by reading the
 * `openhands_theme` localStorage key and toggling the `dark` CSS class
 * on `<html>`. Defaults to dark when no preference is stored.
 */
export function useThemeSync() {
  const applyTheme = React.useCallback((theme: Theme | null) => {
    if (theme === "light") {
      document.documentElement.classList.remove("dark");
    } else {
      document.documentElement.classList.add("dark");
    }
  }, []);

  React.useEffect(() => {
    applyTheme(getTheme());
  }, [applyTheme]);

  const handleStorageChange = React.useCallback(
    (event: StorageEvent) => {
      if (event.key === LOCAL_STORAGE_KEYS.THEME) {
        applyTheme(event.newValue as Theme | null);
      }
    },
    [applyTheme],
  );

  const handleWindowFocus = React.useCallback(() => {
    applyTheme(getTheme());
  }, [applyTheme]);

  useCrossTabState(handleStorageChange, handleWindowFocus);
}
