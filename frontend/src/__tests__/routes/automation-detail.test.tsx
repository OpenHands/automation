import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { AxiosError } from "axios";
import AutomationService from "#/api/automation-service";
import { useUserStore } from "#/stores/user-store";
import { AutomationRunStatus } from "#/types/automation";
import type { Automation, AutomationRunsResponse } from "#/types/automation";
import AutomationDetail from "#/routes/automation-detail";

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

function renderPage(automationId = "1") {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/${automationId}`]}>
        <Routes>
          <Route path="/:automationId" element={<AutomationDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockAutomation: Automation = {
  id: "1",
  name: "PR Triage Digest",
  trigger: {
    type: "cron",
    schedule: "0 9 * * 1-5",
    schedule_human: "Weekdays at 09:00",
  },
  enabled: true,
  repository: "acme/frontend-app",
  model: "Claude Opus",
  created_at: "2026-01-10T00:00:00Z",
  updated_at: "2026-03-23T09:00:00Z",
  prompt: "Review newly opened pull requests in acme/frontend-app.",
  branch: "main",
  plugins: ["GitHub", "Slack"],
  notification: "Slack digest to #eng-reviews",
  timezone: "America/Los_Angeles",
  last_triggered_at: "2026-03-23T09:00:00Z",
};

const mockRuns: AutomationRunsResponse = {
  runs: [
    {
      id: "r1",
      status: AutomationRunStatus.COMPLETED,
      conversation_id: "conv-r1",
      error_detail: null,
      started_at: "2026-03-23T09:00:00Z",
      completed_at: "2026-03-23T09:02:00Z",
    },
  ],
  total: 1,
};

describe("AutomationDetail", () => {
  let getAutomationSpy: ReturnType<typeof vi.spyOn>;
  let getRunsSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    useUserStore.setState({
      user: {
        user_id: "u1",
        email: "test@example.com",
        org_id: "o1",
        org_name: "Test Org",
        role: "owner",
        permissions: ["manage_secrets", "manage_automations"],
      },
      isInitialized: true,
    });
    getAutomationSpy = vi.spyOn(AutomationService, "getAutomation");
    getRunsSpy = vi.spyOn(AutomationService, "getAutomationRuns");
  });

  afterEach(() => {
    useUserStore.setState({ user: null, isInitialized: false });
  });

  it("shows loading skeleton while fetching", () => {
    getAutomationSpy.mockReturnValue(new Promise(() => {}));
    getRunsSpy.mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(screen.getByTestId("detail-skeleton")).toBeInTheDocument();
  });

  it("renders all sections when data loads", async () => {
    getAutomationSpy.mockResolvedValue(mockAutomation);
    getRunsSpy.mockResolvedValue(mockRuns);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    // Header
    expect(screen.getByText("AUTOMATIONS$DETAIL$ACTIVE")).toBeInTheDocument();

    // Prompt section
    expect(screen.getByText("AUTOMATIONS$DETAIL$PROMPT")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Review newly opened pull requests in acme/frontend-app.",
      ),
    ).toBeInTheDocument();

    // Configuration
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$CONFIGURATION"),
    ).toBeInTheDocument();
    expect(screen.getByText("acme/frontend-app")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();

    // Plugins
    expect(screen.getByText("AUTOMATIONS$DETAIL$PLUGINS")).toBeInTheDocument();
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.getByText("Slack")).toBeInTheDocument();

    // Back link
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$BACK_TO_LIST"),
    ).toBeInTheDocument();
  });

  it("renders the last-triggered time (not 'Never') when API returns last_triggered_at", async () => {
    const recentIso = new Date(Date.now() - 5 * 60_000).toISOString();
    getAutomationSpy.mockResolvedValue({
      ...mockAutomation,
      last_triggered_at: recentIso,
    });
    getRunsSpy.mockResolvedValue(mockRuns);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    expect(
      screen.queryByText("AUTOMATIONS$DETAIL$TIME_NEVER"),
    ).not.toBeInTheDocument();
  });

  it("does not render prompt section when prompt is absent", async () => {
    const automationWithoutPrompt = { ...mockAutomation, prompt: undefined };
    getAutomationSpy.mockResolvedValue(automationWithoutPrompt);
    getRunsSpy.mockResolvedValue(mockRuns);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    expect(
      screen.queryByText("AUTOMATIONS$DETAIL$PROMPT"),
    ).not.toBeInTheDocument();
  });

  it("shows not-found state when API returns 404", async () => {
    const error404 = new AxiosError(
      "Not Found",
      "ERR_BAD_REQUEST",
      undefined,
      undefined,
      {
        status: 404,
        data: { detail: "Not found" },
        statusText: "Not Found",
        headers: {},
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        config: {} as any,
      },
    );
    getAutomationSpy.mockRejectedValue(error404);
    getRunsSpy.mockResolvedValue({ runs: [], total: 0 });
    renderPage("nonexistent");

    await waitFor(() => {
      expect(
        screen.getByText("AUTOMATIONS$DETAIL$NOT_FOUND_TITLE"),
      ).toBeInTheDocument();
    });
  });

  it("shows error state and allows retry on network error", async () => {
    getAutomationSpy.mockRejectedValue(new Error("Network error"));
    getRunsSpy.mockResolvedValue({ runs: [], total: 0 });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("AUTOMATIONS$ERROR_TITLE")).toBeInTheDocument();
    });

    getAutomationSpy.mockResolvedValue(mockAutomation);
    const user = userEvent.setup();
    await user.click(screen.getByText("AUTOMATIONS$ERROR_RETRY"));

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });
  });

  it("calls toggle mutation when toggle is clicked", async () => {
    const toggleSpy = vi
      .spyOn(AutomationService, "toggleAutomation")
      .mockResolvedValue({ ...mockAutomation, enabled: false });
    getAutomationSpy.mockResolvedValue(mockAutomation);
    getRunsSpy.mockResolvedValue(mockRuns);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole("switch"));

    expect(toggleSpy).toHaveBeenCalledWith("1", false);
  });

  it("completes delete flow and navigates to list", async () => {
    const deleteSpy = vi
      .spyOn(AutomationService, "deleteAutomation")
      .mockResolvedValue(undefined);
    getAutomationSpy.mockResolvedValue(mockAutomation);
    getRunsSpy.mockResolvedValue(mockRuns);
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PR Triage Digest")).toBeInTheDocument();
    });

    const user = userEvent.setup();

    // Open kebab menu and click delete
    await user.click(screen.getByLabelText("Automation actions"));
    await user.click(screen.getByText("AUTOMATIONS$DELETE"));

    // Confirm deletion in modal
    expect(
      screen.getByText("AUTOMATIONS$DELETE_CONFIRM_TITLE"),
    ).toBeInTheDocument();
    await user.click(
      screen
        .getAllByText("AUTOMATIONS$DELETE")
        .find(
          (el) => el.closest(".mt-6") || el.closest("[class*='justify-end']"),
        ) ?? screen.getAllByText("AUTOMATIONS$DELETE")[1],
    );

    await waitFor(() => {
      expect(deleteSpy).toHaveBeenCalledWith("1");
    });

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/");
    });
  });
});
