import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { vi, describe, it, expect } from "vitest";
import type { Automation } from "#/types/automation";
import { DetailHeader } from "#/components/automations/detail/detail-header";

const mockAutomation: Automation = {
  id: "1",
  name: "PR Triage Digest",
  description: "Summarize new pull requests.",
  trigger: { type: "cron", schedule_human: "Weekdays at 09:00" },
  enabled: true,
  repository: "acme/frontend-app",
  model: "Claude Opus",
  created_at: "2026-01-10T00:00:00Z",
  updated_at: "2026-03-23T09:00:00Z",
};

describe("DetailHeader", () => {
  it("renders automation name and description", () => {
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
    expect(
      screen.getByText("Summarize new pull requests."),
    ).toBeInTheDocument();
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
});
