import { describe, it, expect, vi, beforeEach } from "vitest";
import { AxiosError, AxiosHeaders } from "axios";
import OpenHandsService from "#/api/openhands-service";
import { openhandsApi } from "#/api/axios-clients";

describe("OpenHandsService", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  describe("authenticate", () => {
    it("returns true when session is valid", async () => {
      vi.spyOn(openhandsApi, "post").mockResolvedValue({
        data: { status: "ok" },
      });

      const result = await OpenHandsService.authenticate();

      expect(result).toBe(true);
      expect(openhandsApi.post).toHaveBeenCalledWith("/api/authenticate");
    });

    it("rejects when session is invalid", async () => {
      const error = new AxiosError(
        "Unauthorized",
        "ERR_BAD_REQUEST",
        undefined,
        undefined,
        {
          status: 401,
          data: { error: "User is not authenticated" },
          statusText: "Unauthorized",
          headers: {},
          config: { headers: new AxiosHeaders() },
        },
      );
      vi.spyOn(openhandsApi, "post").mockRejectedValue(error);

      await expect(OpenHandsService.authenticate()).rejects.toThrow();
    });
  });

  describe("getMe", () => {
    it("returns user data", async () => {
      const mockUser = {
        user_id: "u1",
        email: "test@example.com",
        org_id: "o1",
        org_name: "Test Org",
        role: "member",
        permissions: ["view_billing"],
      };
      vi.spyOn(openhandsApi, "get").mockResolvedValue({ data: mockUser });

      const result = await OpenHandsService.getMe();

      expect(result).toEqual(mockUser);
      expect(openhandsApi.get).toHaveBeenCalledWith("/api/v1/users/me");
    });
  });
});
