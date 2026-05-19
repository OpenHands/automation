import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import type { Automation } from "#/types/automation";
import type { User } from "#/types/user";
import { useUserStore } from "#/stores/user-store";
import { DetailHeader } from "#/components/automations/detail/detail-header";

const mockAutomation: Automation = {
  id: "1",
  name: "PR Triage Digest",
  prompt: "Summarize new pull requests.",
  trigger: { type: "cron", schedule_human: "Weekdays at 09:00" },
  enabled: true,
  repository: "acme/frontend-app",
  llm_profile: "claude-opus",
  created_at: "2026-01-10T00:00:00Z",
  updated_at: "2026-03-23T09:00:00Z",
};

const mockUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: ["manage_secrets", "manage_automations"],
};

describe("DetailHeader", () => {
  beforeEach(() => {
    useUserStore.setState({ user: mockUser, isInitialized: true });
  });

  afterEach(() => {
    useUserStore.setState({ user: null, isInitialized: false });
  });
  it("renders automation name and active badge", () => {
    render(
      <MemoryRouter>
        <DetailHeader
          automation={mockAutomation}
          onToggle={vi.fn()}
          onDelete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    expect(screen.getByText("AUTOMATIONS$DETAIL$ACTIVE")).toBeInTheDocument();
  });

  it("renders active badge when automation is enabled", () => {
    render(
      <MemoryRouter>
        <DetailHeader
          automation={mockAutomation}
          onToggle={vi.fn()}
          onDelete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("AUTOMATIONS$DETAIL$ACTIVE")).toBeInTheDocument();
  });

  it("calls onToggle when toggle switch is clicked", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();

    render(
      <MemoryRouter>
        <DetailHeader
          automation={mockAutomation}
          onToggle={onToggle}
          onDelete={vi.fn()}
        />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("switch"));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("calls onDelete when delete menu item is clicked", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();

    render(
      <MemoryRouter>
        <DetailHeader
          automation={mockAutomation}
          onToggle={vi.fn()}
          onDelete={onDelete}
        />
      </MemoryRouter>,
    );

    await user.click(screen.getByLabelText("Automation actions"));
    await user.click(screen.getByText("AUTOMATIONS$DELETE"));
    expect(onDelete).toHaveBeenCalledTimes(1);
  });

  it("shows toggle and kebab with baseline permissions", () => {
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
