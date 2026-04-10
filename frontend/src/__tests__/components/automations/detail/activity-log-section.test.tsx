import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi, describe, it, expect, beforeEach } from "vitest";
import AutomationService from "#/api/automation-service";
import type { AutomationRunsResponse } from "#/types/automation";
import { ActivityLogSection } from "#/components/automations/detail/activity-log-section";

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function renderSection(automationId = "1") {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <ActivityLogSection automationId={automationId} />
    </QueryClientProvider>,
  );
}

const mockRuns: AutomationRunsResponse = {
  runs: [
    {
      id: "r1",
      status: "successful",
      conversation_id: "conv-r1",
      error_detail: null,
      started_at: "2026-03-23T09:00:00Z",
      completed_at: "2026-03-23T09:02:00Z",
    },
    {
      id: "r2",
      status: "failed",
      conversation_id: "conv-r2",
      error_detail: "Process exited with code 1",
      started_at: "2026-03-22T09:00:00Z",
      completed_at: "2026-03-22T09:01:30Z",
    },
  ],
  total: 2,
};

describe("ActivityLogSection", () => {
  let getRunsSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    getRunsSpy = vi.spyOn(AutomationService, "getAutomationRuns");
  });

  it("renders run items after loading", async () => {
    getRunsSpy.mockResolvedValue(mockRuns);
    renderSection();

    await waitFor(() => {
      expect(
        screen.getByText("AUTOMATIONS$DETAIL$SUCCESSFUL"),
      ).toBeInTheDocument();
    });

    expect(screen.getByText("AUTOMATIONS$DETAIL$FAILED")).toBeInTheDocument();
  });

  it("shows empty state when no runs exist", async () => {
    getRunsSpy.mockResolvedValue({ runs: [], total: 0 });
    renderSection();

    await waitFor(() => {
      expect(
        screen.getByText("AUTOMATIONS$DETAIL$NO_RUNS"),
      ).toBeInTheDocument();
    });
  });

  it("shows load more button when there are more runs", async () => {
    const user = userEvent.setup();
    const manyRuns: AutomationRunsResponse = {
      runs: mockRuns.runs,
      total: 25,
    };
    getRunsSpy.mockResolvedValue(manyRuns);
    renderSection();

    await waitFor(() => {
      expect(
        screen.getByText("AUTOMATIONS$DETAIL$LOAD_MORE_RUNS"),
      ).toBeInTheDocument();
    });

    await user.click(screen.getByText("AUTOMATIONS$DETAIL$LOAD_MORE_RUNS"));

    expect(getRunsSpy).toHaveBeenCalledWith("1", 40, 0);
  });
});
