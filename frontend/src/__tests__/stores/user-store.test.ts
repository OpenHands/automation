import { describe, it, expect, beforeEach } from "vitest";
import { useUserStore } from "#/stores/user-store";
import type { User } from "#/types/user";

const mockUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: ["manage_secrets", "view_billing"],
};

describe("useUserStore", () => {
  beforeEach(() => {
    useUserStore.setState({ user: null, isInitialized: false });
  });

  it("has null user and isInitialized false by default", () => {
    const state = useUserStore.getState();

    expect(state.user).toBeNull();
    expect(state.isInitialized).toBe(false);
  });

  it("sets user and marks as initialized", () => {
    useUserStore.getState().setUser(mockUser);
    const state = useUserStore.getState();

    expect(state.user).toEqual(mockUser);
    expect(state.isInitialized).toBe(true);
  });

  it("clears user and resets initialized flag", () => {
    useUserStore.getState().setUser(mockUser);
    useUserStore.getState().clearUser();
    const state = useUserStore.getState();

    expect(state.user).toBeNull();
    expect(state.isInitialized).toBe(false);
  });
});
