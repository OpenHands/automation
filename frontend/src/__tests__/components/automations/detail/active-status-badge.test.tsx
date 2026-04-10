import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ActiveStatusBadge } from "#/components/automations/detail/active-status-badge";

describe("ActiveStatusBadge", () => {
  it("renders active label when active is true", () => {
    render(<ActiveStatusBadge active />);
    expect(screen.getByText("AUTOMATIONS$DETAIL$ACTIVE")).toBeInTheDocument();
  });

  it("renders inactive label when active is false", () => {
    render(<ActiveStatusBadge active={false} />);
    expect(screen.getByText("AUTOMATIONS$DETAIL$INACTIVE")).toBeInTheDocument();
  });
});
