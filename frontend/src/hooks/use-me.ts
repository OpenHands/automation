import { useQuery } from "@tanstack/react-query";
import OpenHandsService from "#/api/openhands-service";
import { useUserStore } from "#/stores/user-store";

export const ME_QUERY_KEY = ["user", "me"] as const;

export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: async () => {
      const user = await OpenHandsService.getMe();
      useUserStore.getState().setUser(user);
      return user;
    },
    enabled,
    staleTime: 5 * 60 * 1000,
  });
}
