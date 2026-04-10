import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, it, expect } from "vitest";
import { NotFoundState } from "#/components/automations/detail/not-found-state";

describe("NotFoundState", () => {
  it("renders not found message and back link", () => {
    render(
      <MemoryRouter>
        <NotFoundState />
      </MemoryRouter>,
    );

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$NOT_FOUND_TITLE"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$NOT_FOUND_MESSAGE"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$BACK_TO_LIST"),
    ).toBeInTheDocument();
  });
});
