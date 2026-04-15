import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { RunStatusBadge } from "#/components/automations/detail/run-status-badge";
import { AutomationRunStatus } from "#/types/automation";

describe("RunStatusBadge", () => {
  it("renders successful label for completed status", () => {
    render(<RunStatusBadge status={AutomationRunStatus.COMPLETED} />);
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$SUCCESSFUL"),
    ).toBeInTheDocument();
  });

  it("renders failed label for failed status", () => {
    render(<RunStatusBadge status={AutomationRunStatus.FAILED} />);
    expect(screen.getByText("AUTOMATIONS$DETAIL$FAILED")).toBeInTheDocument();
  });
});
