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

  it("renders pending label for pending status", () => {
    render(<RunStatusBadge status={AutomationRunStatus.PENDING} />);
    expect(screen.getByText("AUTOMATIONS$DETAIL$PENDING")).toBeInTheDocument();
  });

  it("renders running label for running status", () => {
    render(<RunStatusBadge status={AutomationRunStatus.RUNNING} />);
    expect(screen.getByText("AUTOMATIONS$DETAIL$RUNNING")).toBeInTheDocument();
  });
});
