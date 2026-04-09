import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import AutomationService from "#/api/automation-service";

export const AUTOMATIONS_QUERY_KEY = ["automations"] as const;

export function useAutomations(limit = 50, offset = 0) {
  return useQuery({
    queryKey: [...AUTOMATIONS_QUERY_KEY, { limit, offset }],
    queryFn: () => AutomationService.getAutomations(limit, offset),
    staleTime: 5 * 60 * 1000,
  });
}

export function useToggleAutomation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      AutomationService.toggleAutomation(id, enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: AUTOMATIONS_QUERY_KEY });
    },
  });
}

export function useDeleteAutomation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => AutomationService.deleteAutomation(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: AUTOMATIONS_QUERY_KEY });
    },
  });
}
