import { describe, it, expect, beforeEach } from "vitest";
import {
  getLoginMethod,
  setLoginMethod,
  clearLoginData,
  LoginMethod,
  LOCAL_STORAGE_KEYS,
  getSelectedOrg,
  getTheme,
} from "#/utils/local-storage";

describe("local-storage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("returns null when no login method is stored", () => {
    expect(getLoginMethod()).toBeNull();
  });

  it("stores and retrieves login method", () => {
    setLoginMethod(LoginMethod.GITHUB);

    expect(getLoginMethod()).toBe(LoginMethod.GITHUB);
    expect(localStorage.getItem(LOCAL_STORAGE_KEYS.LOGIN_METHOD)).toBe(
      "github",
    );
  });

  it("clears login data", () => {
    setLoginMethod(LoginMethod.GITLAB);
    clearLoginData();

    expect(getLoginMethod()).toBeNull();
  });

  describe("getSelectedOrg", () => {
    it("returns null when no org is stored", () => {
      expect(getSelectedOrg()).toBeNull();
    });

    it("returns stored org id", () => {
      localStorage.setItem(LOCAL_STORAGE_KEYS.SELECTED_ORG, "org-123");

      expect(getSelectedOrg()).toBe("org-123");
    });
  });

  describe("getTheme", () => {
    it("returns null when no theme is stored", () => {
      expect(getTheme()).toBeNull();
    });

    it("returns stored theme value", () => {
      localStorage.setItem(LOCAL_STORAGE_KEYS.THEME, "light");

      expect(getTheme()).toBe("light");
    });
  });
});
