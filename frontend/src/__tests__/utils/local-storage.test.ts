import { describe, it, expect, beforeEach } from "vitest";
import {
  getLoginMethod,
  setLoginMethod,
  clearLoginData,
  LoginMethod,
  LOCAL_STORAGE_KEYS,
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
});
