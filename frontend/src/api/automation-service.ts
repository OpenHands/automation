import { automationApi } from "./axios-clients";
import type { Automation, AutomationsResponse } from "#/types/automation";

class AutomationService {
  static async getAutomations(
    limit = 50,
    offset = 0,
  ): Promise<AutomationsResponse> {
    const { data } = await automationApi.get<AutomationsResponse>("/v1", {
      params: { limit, offset },
    });
    return data;
  }

  static async toggleAutomation(
    id: string,
    enabled: boolean,
  ): Promise<Automation> {
    const { data } = await automationApi.patch<Automation>(`/v1/${id}`, {
      enabled,
    });
    return data;
  }

  static async deleteAutomation(id: string): Promise<void> {
    await automationApi.delete(`/v1/${id}`);
  }
}

export default AutomationService;
