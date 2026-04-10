import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { RunStatusBadge } from "#/components/automations/detail/run-status-badge";

describe("RunStatusBadge", () => {
  it("renders successful label for successful status", () => {
    render(<RunStatusBadge status="successful" />);
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$SUCCESSFUL"),
    ).toBeInTheDocument();
  });

  it("renders failed label for failed status", () => {
    render(<RunStatusBadge status="failed" />);
    expect(screen.getByText("AUTOMATIONS$DETAIL$FAILED")).toBeInTheDocument();
  });
});
