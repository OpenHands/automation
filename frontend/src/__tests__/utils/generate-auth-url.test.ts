import { describe, it, expect } from "vitest";
import { generateAuthUrl } from "#/utils/generate-auth-url";

describe("generateAuthUrl", () => {
  it("generates a URL with the correct identity provider hint", () => {
    const url = generateAuthUrl(
      "github",
      new URL("https://app.all-hands.dev/automations"),
    );

    expect(url).toContain("kc_idp_hint=github");
  });

  it("uses HTTPS redirect URI for non-localhost", () => {
    const url = generateAuthUrl(
      "github",
      new URL("https://app.all-hands.dev/automations"),
    );

    expect(url).toContain(
      encodeURIComponent("https://app.all-hands.dev/oauth/keycloak/callback"),
    );
  });

  it("uses the request protocol for localhost", () => {
    const url = generateAuthUrl(
      "github",
      new URL("http://localhost:3002/automations"),
    );

    expect(url).toContain(
      encodeURIComponent("http://localhost:3002/oauth/keycloak/callback"),
    );
  });

  it("derives auth URL from app.all-hands.dev", () => {
    const url = generateAuthUrl(
      "github",
      new URL("https://app.all-hands.dev/automations"),
    );

    expect(url).toMatch(
      /^https:\/\/auth\.app\.all-hands\.dev\/realms\/allhands\/protocol\/openid-connect\/auth/,
    );
  });

  it("derives auth URL from staging.all-hands.dev", () => {
    const url = generateAuthUrl(
      "github",
      new URL("https://staging.all-hands.dev/automations"),
    );

    expect(url).toMatch(/^https:\/\/auth\.staging\.all-hands\.dev\//);
  });

  it("uses custom authUrl when provided", () => {
    const url = generateAuthUrl(
      "github",
      new URL("https://app.all-hands.dev/automations"),
      "custom-auth.example.com",
    );

    expect(url).toMatch(/^https:\/\/custom-auth\.example\.com\//);
  });

  it("strips protocol from custom authUrl", () => {
    const url = generateAuthUrl(
      "github",
      new URL("https://app.all-hands.dev/automations"),
      "https://custom-auth.example.com",
    );

    expect(url).toMatch(/^https:\/\/custom-auth\.example\.com\//);
    expect(url).not.toContain("https://https://");
  });

  it("includes login_method in state parameter", () => {
    const url = generateAuthUrl(
      "gitlab",
      new URL("https://app.all-hands.dev/automations"),
    );

    const stateMatch = url.match(/state=([^&]+)/);
    expect(stateMatch).not.toBeNull();
    const state = decodeURIComponent(stateMatch![1]);
    expect(state).toContain("login_method=gitlab");
  });
});
