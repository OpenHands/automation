import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import { ModalBackdrop } from "#/components/shared/modals/modal-backdrop";

describe("ModalBackdrop", () => {
  it("renders children inside a dialog", () => {
    render(
      <ModalBackdrop>
        <div data-testid="child" />
      </ModalBackdrop>,
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("calls onClose when the Escape key is pressed", () => {
    const onClose = vi.fn();
    render(
      <ModalBackdrop onClose={onClose}>
        <div data-testid="child" />
      </ModalBackdrop>,
    );

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the backdrop overlay is clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <ModalBackdrop onClose={onClose}>
        <div data-testid="child" />
      </ModalBackdrop>,
    );

    const backdrop = screen.getByRole("dialog").querySelector(".opacity-60")!;
    await user.click(backdrop);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not call onClose when children are clicked", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <ModalBackdrop onClose={onClose}>
        <button type="button" data-testid="inner-btn">
          {/* no literal string */}
        </button>
      </ModalBackdrop>,
    );

    await user.click(screen.getByTestId("inner-btn"));

    expect(onClose).not.toHaveBeenCalled();
  });

  it("sets aria-label when provided", () => {
    render(
      <ModalBackdrop aria-label="Test dialog">
        <div data-testid="child" />
      </ModalBackdrop>,
    );

    expect(screen.getByRole("dialog")).toHaveAttribute(
      "aria-label",
      "Test dialog",
    );
  });
});
