import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ConfigField } from "#/components/automations/detail/config-field";

describe("ConfigField", () => {
  it("renders label, icon, and children", () => {
    render(
      <ConfigField icon={<span data-testid="field-icon" />} label="Repository">
        <span>{String("acme/frontend")}</span>
      </ConfigField>,
    );

    expect(screen.getByText("Repository")).toBeInTheDocument();
    expect(screen.getByText("acme/frontend")).toBeInTheDocument();
    expect(screen.getByTestId("field-icon")).toBeInTheDocument();
  });

  it("renders complex children", () => {
    render(
      <ConfigField icon={<span />} label="Branch">
        <span>{String("main")}</span>
        <span>{String("develop")}</span>
      </ConfigField>,
    );

    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("develop")).toBeInTheDocument();
  });
});
