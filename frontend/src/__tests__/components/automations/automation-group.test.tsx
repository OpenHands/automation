import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect } from "vitest";
import { MemoryRouter } from "react-router";
import { AutomationGroup } from "#/components/automations/automation-group";
import type { Automation } from "#/types/automation";

const mockAutomation: Automation = {
  id: "1",
  name: "Test Automation",
  prompt: "A test automation",
  trigger: { type: "cron", schedule_human: "Daily" },
  enabled: true,
  repository: "acme/test",
  model: "claude",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("AutomationGroup", () => {
  it("renders nothing when automations array is empty", () => {
    const { container } = render(
      <MemoryRouter>
        <AutomationGroup
          title="Active"
          count={0}
          automations={[]}
          onToggle={vi.fn()}
          onDelete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders title with count and automation cards", () => {
    render(
      <MemoryRouter>
        <AutomationGroup
          title="Active"
          count={1}
          automations={[mockAutomation]}
          onToggle={vi.fn()}
          onDelete={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("Test Automation")).toBeInTheDocument();
  });
});
