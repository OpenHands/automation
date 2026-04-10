import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { BranchBadge } from "#/components/automations/detail/branch-badge";

describe("BranchBadge", () => {
  it("renders the branch name", () => {
    render(<BranchBadge branch="main" />);
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("renders a different branch name", () => {
    render(<BranchBadge branch="feature/login" />);
    expect(screen.getByText("feature/login")).toBeInTheDocument();
  });
});
