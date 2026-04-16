import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router";
import { AutomationCard } from "#/components/automations/automation-card";
import { useUserStore } from "#/stores/user-store";
import type { Automation } from "#/types/automation";
import type { User } from "#/types/user";

const mockUser: User = {
  user_id: "u1",
  email: "test@example.com",
  org_id: "o1",
  org_name: "Test Org",
  role: "owner",
  permissions: ["manage_secrets", "manage_automations"],
};

const mockNavigate = vi.fn();
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => mockNavigate,
}));

const mockAutomation: Automation = {
  id: "abc-123",
  name: "PR Triage Digest",
  prompt: "Summarize new pull requests.",
  trigger: { type: "cron", schedule_human: "Weekdays at 09:00" },
  enabled: true,
  repository: "acme/frontend-app",
  model: "Claude Opus",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-03-01T00:00:00Z",
};

function renderCard(
  automation = mockAutomation,
  onToggle = vi.fn(),
  onDelete = vi.fn(),
) {
  return render(
    <MemoryRouter>
      <AutomationCard
        automation={automation}
        onToggle={onToggle}
        onDelete={onDelete}
      />
    </MemoryRouter>,
  );
}

describe("AutomationCard", () => {
  beforeEach(() => {
    useUserStore.setState({ user: mockUser, isInitialized: true });
  });

  afterEach(() => {
    useUserStore.setState({ user: null, isInitialized: false });
  });
  it("renders automation name, prompt, and metadata chips", () => {
    renderCard();

    expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    expect(
      screen.getByText("Summarize new pull requests."),
    ).toBeInTheDocument();
    expect(screen.getByText("acme/frontend-app")).toBeInTheDocument();
    expect(screen.getByText("Weekdays at 09:00")).toBeInTheDocument();
    expect(screen.getByText("Claude Opus")).toBeInTheDocument();
  });

  it("navigates to detail page when card is clicked", async () => {
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByText("PR Triage Digest"));

    expect(mockNavigate).toHaveBeenCalledWith("/abc-123");
  });

  it("calls onToggle without navigating when toggle is clicked", async () => {
    const onToggle = vi.fn();
    const user = userEvent.setup();
    mockNavigate.mockClear();
    renderCard(mockAutomation, onToggle);

    await user.click(screen.getByRole("switch"));

    expect(onToggle).toHaveBeenCalledWith("abc-123", true);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("calls onDelete via kebab menu without navigating", async () => {
    const onDelete = vi.fn();
    const user = userEvent.setup();
    mockNavigate.mockClear();
    renderCard(mockAutomation, vi.fn(), onDelete);

    await user.click(screen.getByLabelText("Automation actions"));
    await user.click(screen.getByText("AUTOMATIONS$DELETE"));

    expect(onDelete).toHaveBeenCalledWith("abc-123");
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("hides repository and model chips when values are empty", () => {
    const automation = {
      ...mockAutomation,
      repository: "",
      model: "",
    };
    renderCard(automation);

    expect(screen.queryByText("acme/frontend-app")).not.toBeInTheDocument();
    expect(screen.queryByText("Claude Opus")).not.toBeInTheDocument();
    expect(screen.getByText("Weekdays at 09:00")).toBeInTheDocument();
  });

  it("shows trigger type when schedule_human is not available", () => {
    const automation = {
      ...mockAutomation,
      trigger: { type: "cron" },
    };
    renderCard(automation);

    expect(screen.getByText("cron")).toBeInTheDocument();
  });

  it("shows toggle and kebab with baseline permissions", () => {
    renderCard();

    expect(screen.getByRole("switch")).toBeInTheDocument();
    expect(screen.getByLabelText("Automation actions")).toBeInTheDocument();
  });
});
