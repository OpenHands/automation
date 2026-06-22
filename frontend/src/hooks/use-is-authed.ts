import { useQuery } from "@tanstack/react-query";
import axios from "axios";
import OpenHandsService from "#/api/openhands-service";

export const AUTH_QUERY_KEY = ["user", "authenticated"] as const;

export function useIsAuthed() {
  return useQuery({
    queryKey: AUTH_QUERY_KEY,
    queryFn: async () => {
      try {
        await OpenHandsService.authenticate();
        return true;
      } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === 401) {
          return false;
        }
        throw error;
      }
    },
    staleTime: 5 * 60 * 1000,
    retry: false,
    meta: { disableToast: true },
  });
}
