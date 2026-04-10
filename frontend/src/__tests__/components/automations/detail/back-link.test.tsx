import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, it, expect } from "vitest";
import { BackLink } from "#/components/automations/detail/back-link";

describe("BackLink", () => {
  it("renders translated back-to-list label", () => {
    render(
      <MemoryRouter>
        <BackLink />
      </MemoryRouter>,
    );

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$BACK_TO_LIST"),
    ).toBeInTheDocument();
  });

  it("links to the root automations list", () => {
    render(
      <MemoryRouter>
        <BackLink />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/");
  });
});
