import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PluginChip } from "#/components/automations/detail/plugin-chip";

describe("PluginChip", () => {
  it("renders the plugin name", () => {
    render(<PluginChip name="GitHub" />);
    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });

  it("renders a different plugin name", () => {
    render(<PluginChip name="Slack" />);
    expect(screen.getByText("Slack")).toBeInTheDocument();
  });
});
