import React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useCrossTabState } from "./use-cross-tab-state";
import { LOCAL_STORAGE_KEYS, getSelectedOrg } from "#/utils/local-storage";

/**
 * Synchronises the active organization across browser tabs by listening to
 * changes on the `openhands_selected_org` localStorage key.
 *
 * When the org changes (via storage event or window focus), all React Query
 * caches are invalidated so automation data re-fetches under the new org.
 */
export function useOrgSync(currentOrgId: string | undefined) {
  const queryClient = useQueryClient();

  const handleStorageChange = React.useCallback(
    (event: StorageEvent) => {
      if (
        event.key === LOCAL_STORAGE_KEYS.SELECTED_ORG &&
        event.newValue !== null &&
        event.newValue !== currentOrgId
      ) {
        queryClient.invalidateQueries();
      }
    },
    [currentOrgId, queryClient],
  );

  const handleWindowFocus = React.useCallback(() => {
    const storedOrg = getSelectedOrg();
    if (storedOrg && storedOrg !== currentOrgId) {
      queryClient.invalidateQueries();
    }
  }, [currentOrgId, queryClient]);

  useCrossTabState(handleStorageChange, handleWindowFocus);
}
