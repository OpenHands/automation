import { openhandsApi } from "./axios-clients";
import type { User } from "#/types/user";

class OpenHandsService {
  static async authenticate(): Promise<boolean> {
    await openhandsApi.post("/api/authenticate");
    return true;
  }

  static async getMe(): Promise<User> {
    const { data } = await openhandsApi.get<User>("/api/v1/users/me");
    return data;
  }
}

export default OpenHandsService;
