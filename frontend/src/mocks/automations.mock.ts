import type { AutomationsResponse } from "#/types/automation";

/**
 * Mock automations data matching the AutomationResponse schema from the backend.
 *
 * Backend schema fields: id, user_id, org_id, name, trigger (JSONB),
 * tarball_path, setup_script_path, entrypoint, timeout, enabled,
 * last_triggered_at, created_at, updated_at.
 *
 * The frontend Automation type only uses a subset of these fields.
 * Additional backend fields are included here for API fidelity.
 */

const now = new Date().toISOString();
const daysAgo = (days: number) =>
  new Date(Date.now() - days * 86_400_000).toISOString();

export const MOCK_AUTOMATIONS_RESPONSE: AutomationsResponse = {
  automations: [
    {
      id: "a1000000-0000-0000-0000-000000000001",
      name: "PR Triage Digest",
      description:
        "Summarize new pull requests and flag risky changes every weekday morning.",
      trigger: {
        type: "cron",
        schedule: "0 9 * * 1-5",
        schedule_human: "Weekdays at 09:00",
      },
      enabled: true,
      repository: "acme/frontend-app",
      model: "Claude Opus",
      created_at: daysAgo(90),
      updated_at: now,
    },
    {
      id: "a1000000-0000-0000-0000-000000000002",
      name: "Nightly Security Pass",
      description:
        "Run a repository scan and create a remediation summary for critical findings.",
      trigger: {
        type: "cron",
        schedule: "30 1 * * *",
        schedule_human: "Daily at 01:30",
      },
      enabled: true,
      repository: "acme/backend-api",
      model: "GPT-5",
      created_at: daysAgo(60),
      updated_at: now,
    },
    {
      id: "a1000000-0000-0000-0000-000000000003",
      name: "Docs Sync on Push",
      description:
        "Watch the docs repository and prepare a changelog-ready summary when pushes land.",
      trigger: {
        type: "cron",
        schedule: "*/5 * * * *",
        schedule_human: "Runs on every push",
      },
      enabled: true,
      repository: "acme/docs",
      model: "GPT-4o",
      created_at: daysAgo(45),
      updated_at: now,
    },
    {
      id: "a1000000-0000-0000-0000-000000000004",
      name: "Release Readiness Review",
      description:
        "Compile release blockers, open incidents, and pending approvals before Friday ship.",
      trigger: {
        type: "cron",
        schedule: "0 11 * * 5",
        schedule_human: "Fridays at 11:00",
      },
      enabled: false,
      repository: "acme/realtime-service",
      model: "Gemini 2.5 Pro",
      created_at: daysAgo(80),
      updated_at: now,
    },
    {
      id: "a1000000-0000-0000-0000-000000000005",
      name: "Incident Webhook Summary",
      description:
        "Summarize incoming incident webhooks and post a digest to the on-call channel.",
      trigger: {
        type: "cron",
        schedule: "0 */2 * * *",
        schedule_human: "On incident webhook",
      },
      enabled: false,
      repository: "acme/incident-service",
      model: "Claude Sonnet",
      created_at: daysAgo(30),
      updated_at: now,
    },
  ],
  total: 5,
};
