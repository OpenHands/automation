import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect } from "vitest";
import { DeleteConfirmationModal } from "#/components/automations/delete-confirmation-modal";

describe("DeleteConfirmationModal", () => {
  it("renders nothing when isOpen is false", () => {
    const { container } = render(
      <DeleteConfirmationModal
        automationName="Test"
        isOpen={false}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders title and confirmation message when open", () => {
    render(
      <DeleteConfirmationModal
        automationName="PR Triage Digest"
        isOpen
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(
      screen.getByText("AUTOMATIONS$DELETE_CONFIRM_TITLE"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("AUTOMATIONS$DELETE_CONFIRM_MESSAGE"),
    ).toBeInTheDocument();
  });

  it("calls onConfirm when delete button is clicked", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(
      <DeleteConfirmationModal
        automationName="Test"
        isOpen
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );

    await user.click(screen.getByText("AUTOMATIONS$DELETE"));

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("calls onCancel when cancel button is clicked", async () => {
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <DeleteConfirmationModal
        automationName="Test"
        isOpen
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );

    await user.click(screen.getByText("AUTOMATIONS$CANCEL"));

    expect(onCancel).toHaveBeenCalledOnce();
  });
});
