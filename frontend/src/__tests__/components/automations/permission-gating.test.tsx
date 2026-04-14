import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router";
import { useUserStore } from "#/stores/user-store";
import type { Automation } from "#/types/automation";
import type { User } from "#/types/user";

vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => vi.fn(),
}));

vi.mock("#/utils/permissions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("#/utils/permissions")>()),
  ENFORCED_PERMISSIONS: new Set(["manage_automations"]),
}));

const mockAutomation: Automation = {
  id: "1",
  name: "PR Triage Digest",
  description: "Summarize new pull requests.",
  trigger: { type: "cron", schedule_human: "Weekdays at 09:00" },
  enabled: true,
  repository: "acme/frontend-app",
  model: "Claude Opus",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-03-01T00:00:00Z",
};

const baseUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "member",
  permissions: [],
};

describe("Permission gating when manage_automations is enforced", () => {
  afterEach(() => {
    useUserStore.setState({ user: null, isInitialized: false });
  });

  describe("AutomationCard", () => {
    let AutomationCard: typeof import("#/components/automations/automation-card").AutomationCard;

    beforeEach(async () => {
      ({ AutomationCard } =
        await import("#/components/automations/automation-card"));
    });

    it("hides toggle and kebab when user lacks manage_automations", () => {
      useUserStore.setState({ user: baseUser, isInitialized: true });

      render(
        <MemoryRouter>
          <AutomationCard
            automation={mockAutomation}
            onToggle={vi.fn()}
            onDelete={vi.fn()}
          />
        </MemoryRouter>,
      );

      expect(screen.queryByRole("switch")).not.toBeInTheDocument();
      expect(
        screen.queryByLabelText("Automation actions"),
      ).not.toBeInTheDocument();
    });

    it("shows toggle and kebab when user has manage_automations", () => {
      const userWithPerm: User = {
        ...baseUser,
        permissions: ["manage_automations"],
      };
      useUserStore.setState({ user: userWithPerm, isInitialized: true });

      render(
        <MemoryRouter>
          <AutomationCard
            automation={mockAutomation}
            onToggle={vi.fn()}
            onDelete={vi.fn()}
          />
        </MemoryRouter>,
      );

      expect(screen.getByRole("switch")).toBeInTheDocument();
      expect(screen.getByLabelText("Automation actions")).toBeInTheDocument();
    });
  });

  describe("DetailHeader", () => {
    let DetailHeader: typeof import("#/components/automations/detail/detail-header").DetailHeader;

    beforeEach(async () => {
      ({ DetailHeader } =
        await import("#/components/automations/detail/detail-header"));
    });

    it("hides toggle and kebab when user lacks manage_automations", () => {
      useUserStore.setState({ user: baseUser, isInitialized: true });

      render(
        <MemoryRouter>
          <DetailHeader
            automation={mockAutomation}
            onToggle={vi.fn()}
            onDelete={vi.fn()}
          />
        </MemoryRouter>,
      );

      expect(screen.queryByRole("switch")).not.toBeInTheDocument();
      expect(
        screen.queryByLabelText("Automation actions"),
      ).not.toBeInTheDocument();
    });

    it("shows toggle and kebab when user has manage_automations", () => {
      const userWithPerm: User = {
        ...baseUser,
        permissions: ["manage_automations"],
      };
      useUserStore.setState({ user: userWithPerm, isInitialized: true });

      render(
        <MemoryRouter>
          <DetailHeader
            automation={mockAutomation}
            onToggle={vi.fn()}
            onDelete={vi.fn()}
          />
        </MemoryRouter>,
      );

      expect(screen.getByRole("switch")).toBeInTheDocument();
      expect(screen.getByLabelText("Automation actions")).toBeInTheDocument();
    });
  });
});
