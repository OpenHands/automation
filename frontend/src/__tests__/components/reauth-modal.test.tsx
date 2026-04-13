import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ReauthModal } from "#/components/reauth-modal";

describe("ReauthModal", () => {
  it("renders the logging back in message within a dialog", () => {
    render(<ReauthModal />);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("AUTH$LOGGING_BACK_IN")).toBeInTheDocument();
  });
});
