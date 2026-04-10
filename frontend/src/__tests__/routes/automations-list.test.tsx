import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { vi, describe, it, expect, beforeEach } from "vitest";
import AutomationService from "#/api/automation-service";
import type { AutomationsResponse } from "#/types/automation";
import AutomationsList from "#/routes/automations-list";

const mockNavigate = vi.fn();
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => mockNavigate,
}));

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function renderPage() {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AutomationsList />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockAutomations: AutomationsResponse = {
  automations: [
    {
      id: "1",
      name: "PR Triage Digest",
      description: "Summarize new pull requests and flag risky changes.",
      trigger: { type: "schedule", schedule_human: "Weekdays at 09:00" },
      enabled: true,
      repository: "acme/frontend-app",
      model: "Claude Opus",
      created_at: "2026-01-10T00:00:00Z",
      updated_at: "2026-03-23T09:00:00Z",
    },
    {
      id: "2",
      name: "Nightly Security Pass",
      description: "Run a repository scan and create a remediation summary.",
      trigger: { type: "schedule", schedule_human: "Daily at 01:30" },
      enabled: true,
      repository: "acme/backend-api",
      model: "GPT-5",
      created_at: "2026-02-01T00:00:00Z",
      updated_at: "2026-03-22T01:30:00Z",
    },
    {
      id: "3",
      name: "Release Readiness Review",
      description:
        "Compile release blockers, open incidents, and pending approvals.",
      trigger: { type: "schedule", schedule_human: "Fridays at 11:00" },
      enabled: false,
      repository: "acme/realtime-service",
      model: "Gemini 2.5 Pro",
      created_at: "2026-01-20T00:00:00Z",
      updated_at: "2026-03-21T11:00:00Z",
    },
  ],
  total: 3,
};

describe("AutomationsList", () => {
  let getAutomationsSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    getAutomationsSpy = vi.spyOn(AutomationService, "getAutomations");
  });

  it("shows loading skeletons while data is being fetched", () => {
    getAutomationsSpy.mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(screen.getAllByTestId("automation-card-skeleton")).toHaveLength(3);
  });

  it("renders automations grouped by active and inactive status", async () => {
    getAutomationsSpy.mockResolvedValue(mockAutomations);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    // Active section with 2 active automations
    expect(screen.getByText("AUTOMATIONS$ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("Nightly Security Pass")).toBeInTheDocument();

    // Inactive section with 1 inactive automation
    expect(screen.getByText("AUTOMATIONS$INACTIVE")).toBeInTheDocument();
    expect(screen.getByText("Release Readiness Review")).toBeInTheDocument();
  });

  it("filters automations by search query", async () => {
    const user = userEvent.setup();
    getAutomationsSpy.mockResolvedValue(mockAutomations);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText(
      "AUTOMATIONS$SEARCH_PLACEHOLDER",
    );
    await user.type(searchInput, "Security");

    expect(screen.getByText("Nightly Security Pass")).toBeInTheDocument();
    expect(screen.queryByText("PR Triage Digest")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Release Readiness Review"),
    ).not.toBeInTheDocument();
  });

  it("shows empty state when no automations exist", async () => {
    getAutomationsSpy.mockResolvedValue({ automations: [], total: 0 });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("AUTOMATIONS$EMPTY")).toBeInTheDocument();
    });
  });

  it("shows error state and allows retry", async () => {
    getAutomationsSpy.mockRejectedValue(new Error("Network error"));
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("AUTOMATIONS$ERROR_TITLE")).toBeInTheDocument();
    });

    expect(screen.getByText("AUTOMATIONS$ERROR_RETRY")).toBeInTheDocument();

    getAutomationsSpy.mockResolvedValue(mockAutomations);
    const user = userEvent.setup();
    await user.click(screen.getByText("AUTOMATIONS$ERROR_RETRY"));

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });
  });

  it("navigates to automation detail when card is clicked", async () => {
    getAutomationsSpy.mockResolvedValue(mockAutomations);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    const card = screen.getByText("PR Triage Digest").closest("[role='link']");
    expect(card).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(card!);

    expect(mockNavigate).toHaveBeenCalledWith("/1");
  });
});
