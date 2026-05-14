import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { AutomationRunStatus } from "#/types/automation";
import type { AutomationRun } from "#/types/automation";
import { ActivityLogItem } from "#/components/automations/detail/activity-log-item";

describe("ActivityLogItem", () => {
  beforeEach(() => {
    Object.defineProperty(window, "location", {
      value: { hostname: "app.all-hands.dev" },
      writable: true,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  const createRun = (
    overrides: Partial<AutomationRun> = {},
  ): AutomationRun => ({
    id: "run-1",
    status: AutomationRunStatus.COMPLETED,
    conversation_id: "conv-123",
    error_detail: null,
    started_at: "2026-03-23T09:00:00Z",
    completed_at: "2026-03-23T09:02:00Z",
    ...overrides,
  });

  it("renders run timestamp and status badge", () => {
    const run = createRun();
    render(<ActivityLogItem run={run} />);

    expect(
      screen.getByText("AUTOMATIONS$DETAIL$SUCCESSFUL"),
    ).toBeInTheDocument();
  });

  it("formats the run timestamp in the automation timezone", () => {
    const run = createRun();
    render(<ActivityLogItem run={run} timeZone="America/Los_Angeles" />);

    expect(
      screen.getByText(
        (content) => content.includes("2:00 AM") && content.includes("PDT"),
      ),
    ).toBeInTheDocument();
  });

  it("renders as a link when conversation_id exists", () => {
    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const link = screen.getByRole("link");
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute(
      "href",
      "https://app.all-hands.dev/conversations/conv-abc123",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("renders as a div when conversation_id is null", () => {
    const run = createRun({ conversation_id: null });
    render(<ActivityLogItem run={run} />);

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows 'Conversation not created' message when conversation_id is null", () => {
    const run = createRun({ conversation_id: null });
    render(<ActivityLogItem run={run} />);

    expect(
      screen.getByText("(AUTOMATIONS$DETAIL$NO_CONVERSATION)"),
    ).toBeInTheDocument();
  });

  it("does not show 'Conversation not created' message when conversation_id exists", () => {
    const run = createRun({ conversation_id: "conv-123" });
    render(<ActivityLogItem run={run} />);

    expect(
      screen.queryByText("(AUTOMATIONS$DETAIL$NO_CONVERSATION)"),
    ).not.toBeInTheDocument();
  });

  it("has hover cursor and styles when conversation exists", () => {
    const run = createRun({ conversation_id: "conv-123" });
    render(<ActivityLogItem run={run} />);

    const link = screen.getByRole("link");
    expect(link.className).toContain("cursor-pointer");
    expect(link.className).toContain("hover:bg-surface-elevated");
  });

  it("has default cursor when no conversation exists", () => {
    const run = createRun({ conversation_id: null });
    const { container } = render(<ActivityLogItem run={run} />);

    const div = container.querySelector("div.cursor-default");
    expect(div).toBeInTheDocument();
    expect(div?.className).not.toContain("hover:bg-surface-elevated");
  });

  it("uses staging host when on staging domain", () => {
    Object.defineProperty(window, "location", {
      value: { hostname: "staging.all-hands.dev" },
      writable: true,
    });

    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      "https://staging.all-hands.dev/conversations/conv-abc123",
    );
  });

  it("uses app.all-hands.dev for localhost", () => {
    Object.defineProperty(window, "location", {
      value: { hostname: "localhost" },
      writable: true,
    });

    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      "https://app.all-hands.dev/conversations/conv-abc123",
    );
  });

  it("renders failed status correctly", () => {
    const run = createRun({
      status: AutomationRunStatus.FAILED,
      error_detail: "Process exited with code 1",
    });
    render(<ActivityLogItem run={run} />);

    expect(screen.getByText("AUTOMATIONS$DETAIL$FAILED")).toBeInTheDocument();
  });

  it("has appropriate aria-label for link with conversation", () => {
    const run = createRun({ conversation_id: "conv-123" });
    render(<ActivityLogItem run={run} />);

    const link = screen.getByRole("link");
    expect(link.getAttribute("aria-label")).toContain("View conversation");
  });

  it("does not have aria-label on non-interactive div", () => {
    const run = createRun({ conversation_id: null });
    const { container } = render(<ActivityLogItem run={run} />);

    // Non-interactive divs don't need aria-label since visible text is read naturally
    const div = container.querySelector("div.cursor-default");
    expect(div).toBeInTheDocument();
    expect(div).not.toHaveAttribute("aria-label");
  });
});
