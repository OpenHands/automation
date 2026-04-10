import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect } from "vitest";
import { ErrorState } from "#/components/automations/error-state";

describe("ErrorState", () => {
  it("renders error message and retry button", () => {
    render(<ErrorState onRetry={vi.fn()} />);

    expect(screen.getByText("AUTOMATIONS$ERROR_TITLE")).toBeInTheDocument();
    expect(screen.getByText("AUTOMATIONS$ERROR_RETRY")).toBeInTheDocument();
  });

  it("calls onRetry when retry button is clicked", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(<ErrorState onRetry={onRetry} />);

    await user.click(screen.getByText("AUTOMATIONS$ERROR_RETRY"));

    expect(onRetry).toHaveBeenCalledOnce();
  });
});
