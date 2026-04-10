import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { ActivitySection } from "#/components/automations/detail/activity-section";

describe("ActivitySection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the section title and field labels", () => {
    render(
      <ActivitySection
        createdAt="2026-01-15T10:00:00Z"
        lastRunAt="2026-01-20T10:00:00Z"
      />,
    );

    expect(screen.getByText("AUTOMATIONS$DETAIL$ACTIVITY")).toBeInTheDocument();
    expect(screen.getByText("AUTOMATIONS$DETAIL$CREATED")).toBeInTheDocument();
    expect(screen.getByText("AUTOMATIONS$DETAIL$LAST_RUN")).toBeInTheDocument();
  });

  it("renders 'Never' translation when lastRunAt is null", () => {
    render(
      <ActivitySection createdAt="2026-01-15T10:00:00Z" lastRunAt={null} />,
    );

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$TIME_NEVER"),
    ).toBeInTheDocument();
  });

  it("renders 'Never' translation when lastRunAt is undefined", () => {
    render(
      <ActivitySection
        createdAt="2026-01-15T10:00:00Z"
        lastRunAt={undefined}
      />,
    );

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$TIME_NEVER"),
    ).toBeInTheDocument();
  });

  it("renders 'Just now' for a very recent run", () => {
    vi.spyOn(Date, "now").mockReturnValue(
      new Date("2026-03-10T12:00:00Z").getTime(),
    );

    render(
      <ActivitySection
        createdAt="2026-01-15T10:00:00Z"
        lastRunAt="2026-03-10T12:00:00Z"
      />,
    );

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$TIME_JUST_NOW"),
    ).toBeInTheDocument();
  });

  it("renders 'Yesterday' for a run one day ago", () => {
    vi.spyOn(Date, "now").mockReturnValue(
      new Date("2026-03-10T12:00:00Z").getTime(),
    );

    render(
      <ActivitySection
        createdAt="2026-01-15T10:00:00Z"
        lastRunAt="2026-03-09T12:00:00Z"
      />,
    );

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$TIME_YESTERDAY"),
    ).toBeInTheDocument();
  });
});
