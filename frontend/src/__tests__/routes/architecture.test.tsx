import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import Architecture from "#/routes/architecture";

function renderPage() {
  return render(
    <MemoryRouter>
      <Architecture />
    </MemoryRouter>,
  );
}

describe("Architecture", () => {
  it("documents the automations architecture and lifecycle", () => {
    renderPage();

    expect(
      screen.getByRole("heading", {
        name: "How automations become sandbox runs",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("System map")).toBeInTheDocument();
    expect(screen.getByText("FastAPI service")).toBeInTheDocument();
    expect(screen.getByText("Run state machine")).toBeInTheDocument();
    expect(
      screen.getByText("Environment injected into user code"),
    ).toBeInTheDocument();
    expect(screen.getByText("Execution modes")).toBeInTheDocument();
  });

  it("links back to the automations list", () => {
    renderPage();

    expect(screen.getByRole("link", { name: "← Automations" })).toHaveAttribute(
      "href",
      "/",
    );
  });
});
