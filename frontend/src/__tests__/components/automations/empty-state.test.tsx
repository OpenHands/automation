import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { EmptyState } from "#/components/automations/empty-state";

describe("EmptyState", () => {
  it("renders empty state message", () => {
    render(<EmptyState />);

    expect(screen.getByText("AUTOMATIONS$EMPTY")).toBeInTheDocument();
  });
});
