import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import type { Automation } from "#/types/automation";
import { ConfigurationSection } from "#/components/automations/detail/configuration-section";

const mockAutomation: Automation = {
  id: "1",
  name: "PR Triage Digest",
  description: "Summarize new pull requests.",
  trigger: {
    type: "cron",
    schedule: "0 9 * * 1-5",
    schedule_human: "Weekdays at 09:00",
  },
  enabled: true,
  repository: "acme/frontend-app",
  model: "Claude Opus",
  created_at: "2026-01-10T00:00:00Z",
  updated_at: "2026-03-23T09:00:00Z",
  branch: "main",
  timezone: "America/Los_Angeles",
  notification: "Slack digest to #eng-reviews",
};

describe("ConfigurationSection", () => {
  it("renders all configuration fields", () => {
    render(<ConfigurationSection automation={mockAutomation} />);

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$CONFIGURATION"),
    ).toBeInTheDocument();
    expect(screen.getByText("acme/frontend-app")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("Schedule")).toBeInTheDocument();
    expect(
      screen.getByText("Weekdays at 09:00 (America/Los_Angeles)"),
    ).toBeInTheDocument();
    expect(screen.getByText("Claude Opus")).toBeInTheDocument();
    expect(
      screen.getByText("Slack digest to #eng-reviews"),
    ).toBeInTheDocument();
  });

  it("hides repository and model fields when values are empty", () => {
    const automation = {
      ...mockAutomation,
      repository: "",
      model: "",
    };

    render(<ConfigurationSection automation={automation} />);

    expect(
      screen.queryByText("AUTOMATIONS$DETAIL$REPOSITORIES"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("AUTOMATIONS$DETAIL$MODEL"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("AUTOMATIONS$DETAIL$CONFIGURATION"),
    ).toBeInTheDocument();
  });

  it("does not render notification field when not provided", () => {
    const automationWithoutNotification = {
      ...mockAutomation,
      notification: undefined,
    };

    render(<ConfigurationSection automation={automationWithoutNotification} />);

    expect(
      screen.queryByText("AUTOMATIONS$DETAIL$NOTIFICATION"),
    ).not.toBeInTheDocument();
  });
});
