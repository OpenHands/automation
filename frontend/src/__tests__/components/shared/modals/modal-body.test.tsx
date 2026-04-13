import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ModalBody } from "#/components/shared/modals/modal-body";

describe("ModalBody", () => {
  it("renders children", () => {
    render(
      <ModalBody>
        <div data-testid="child" />
      </ModalBody>,
    );

    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("applies testID as data-testid", () => {
    render(
      <ModalBody testID="my-modal">
        <div data-testid="child" />
      </ModalBody>,
    );

    expect(screen.getByTestId("my-modal")).toBeInTheDocument();
  });

  it("merges custom className with defaults", () => {
    render(
      <ModalBody testID="styled" className="border border-red-500">
        <div data-testid="child" />
      </ModalBody>,
    );

    const el = screen.getByTestId("styled");
    expect(el.className).toContain("border");
    expect(el.className).toContain("rounded-xl");
  });

  it("applies small width by default", () => {
    render(
      <ModalBody testID="default-width">
        <div data-testid="child" />
      </ModalBody>,
    );

    const el = screen.getByTestId("default-width");
    expect(el.className).toContain("w-[384px]");
    expect(el.className).not.toContain("w-[700px]");
  });

  it("applies medium width when specified", () => {
    render(
      <ModalBody testID="medium-width" width="medium">
        <div data-testid="child" />
      </ModalBody>,
    );

    const el = screen.getByTestId("medium-width");
    expect(el.className).toContain("w-[700px]");
    expect(el.className).not.toContain("w-[384px]");
  });
});
