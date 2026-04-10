import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PluginsSection } from "#/components/automations/detail/plugins-section";

describe("PluginsSection", () => {
  it("renders the section title and all plugin chips", () => {
    const plugins = ["GitHub", "Slack", "Linear"];
    render(<PluginsSection plugins={plugins} />);

    expect(screen.getByText("AUTOMATIONS$DETAIL$PLUGINS")).toBeInTheDocument();
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.getByText("Slack")).toBeInTheDocument();
    expect(screen.getByText("Linear")).toBeInTheDocument();
  });

  it("renders a single plugin", () => {
    render(<PluginsSection plugins={["GitHub"]} />);

    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });
});
