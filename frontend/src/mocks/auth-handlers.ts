import { http, HttpResponse } from "msw";

export const authHandlers = [
  // POST /api/authenticate — Validate session cookie
  http.post("/api/authenticate", () =>
    HttpResponse.json({ status: "ok" }, { status: 200 }),
  ),

  // GET /api/v1/users/me — Get user context
  http.get("/api/v1/users/me", () =>
    HttpResponse.json({
      user_id: "00000000-0000-0000-0000-000000000001",
      email: "dev@example.com",
      org_id: "00000000-0000-0000-0000-000000000001",
      org_name: "Acme Corp",
      role: "owner",
      language: "en",
      permissions: [
        "manage_secrets",
        "manage_mcp",
        "manage_integrations",
        "manage_application_settings",
        "manage_api_keys",
        "view_llm_settings",
        "edit_llm_settings",
        "view_billing",
        "add_credits",
        "invite_user_to_organization",
        "change_user_role:member",
        "change_user_role:admin",
        "change_user_role:owner",
        "view_org_settings",
        "edit_org_settings",
        "change_organization_name",
        "delete_organization",
        "manage_org_claims",
      ],
    }),
  ),
];
