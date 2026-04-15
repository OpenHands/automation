import { renderHook } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { useUserStore } from "#/stores/user-store";
import type { User } from "#/types/user";

const mockUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: ["manage_secrets"],
};

describe("useHasPermission", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useUserStore.setState({ user: null, isInitialized: false });
  });

  describe("when permission is not enforced (baseline)", () => {
    beforeEach(() => {
      vi.resetModules();
      vi.doMock("#/utils/permissions", () => ({
        ENFORCED_PERMISSIONS: new Set(),
      }));
    });

    it("returns false when user is not authenticated", async () => {
      const { useHasPermission } = await import("#/hooks/use-has-permission");
      const { result } = renderHook(() =>
        useHasPermission("manage_automations"),
      );
      expect(result.current).toBe(false);
    });

    it("returns true when user is authenticated", async () => {
      const { useUserStore: freshStore } = await import("#/stores/user-store");
      freshStore.setState({ user: mockUser, isInitialized: true });
      const { useHasPermission } = await import("#/hooks/use-has-permission");
      const { result } = renderHook(() =>
        useHasPermission("manage_automations"),
      );
      expect(result.current).toBe(true);
    });
  });

  describe("when permission is enforced", () => {
    beforeEach(() => {
      vi.resetModules();
      vi.doMock("#/utils/permissions", () => ({
        ENFORCED_PERMISSIONS: new Set(["manage_automations"]),
      }));
    });

    it("returns true when user has the permission", async () => {
      const userWithPermission: User = {
        ...mockUser,
        permissions: ["manage_automations"],
      };

      const { useUserStore: freshStore } = await import("#/stores/user-store");
      freshStore.setState({ user: userWithPermission, isInitialized: true });

      const { useHasPermission } = await import("#/hooks/use-has-permission");
      const { result } = renderHook(() =>
        useHasPermission("manage_automations"),
      );
      expect(result.current).toBe(true);
    });

    it("returns false when user lacks the permission", async () => {
      const { useUserStore: freshStore } = await import("#/stores/user-store");
      freshStore.setState({ user: mockUser, isInitialized: true });

      const { useHasPermission } = await import("#/hooks/use-has-permission");
      const { result } = renderHook(() =>
        useHasPermission("manage_automations"),
      );
      expect(result.current).toBe(false);
    });
  });
});
