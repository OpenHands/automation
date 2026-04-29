import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { AutomationRunStatus } from "#/types/automation";
import type { AutomationRun } from "#/types/automation";
import { ActivityLogItem } from "#/components/automations/detail/activity-log-item";

// Mock window.open
const mockWindowOpen = vi.fn();

describe("ActivityLogItem", () => {
  beforeEach(() => {
    vi.stubGlobal("open", mockWindowOpen);
    Object.defineProperty(window, "location", {
      value: { hostname: "app.all-hands.dev" },
      writable: true,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
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
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("opens conversation in new tab when clicked with conversation_id", async () => {
    const user = userEvent.setup();
    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    await user.click(row);

    expect(mockWindowOpen).toHaveBeenCalledWith(
      "https://app.all-hands.dev/conversations/conv-abc123",
      "_blank",
    );
  });

  it("opens conversation when Enter key is pressed", async () => {
    const user = userEvent.setup();
    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    row.focus();
    await user.keyboard("{Enter}");

    expect(mockWindowOpen).toHaveBeenCalledWith(
      "https://app.all-hands.dev/conversations/conv-abc123",
      "_blank",
    );
  });

  it("opens conversation when Space key is pressed", async () => {
    const user = userEvent.setup();
    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    row.focus();
    await user.keyboard(" ");

    expect(mockWindowOpen).toHaveBeenCalledWith(
      "https://app.all-hands.dev/conversations/conv-abc123",
      "_blank",
    );
  });

  it("does not open conversation when clicked without conversation_id", async () => {
    const user = userEvent.setup();
    const run = createRun({ conversation_id: null });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    await user.click(row);

    expect(mockWindowOpen).not.toHaveBeenCalled();
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

  it("has hover cursor when conversation exists", () => {
    const run = createRun({ conversation_id: "conv-123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    expect(row.className).toContain("cursor-pointer");
    expect(row.className).toContain("hover:bg-surface-elevated");
  });

  it("has default cursor when no conversation exists", () => {
    const run = createRun({ conversation_id: null });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    expect(row.className).toContain("cursor-default");
    expect(row.className).not.toContain("hover:bg-surface-elevated");
  });

  it("is focusable when conversation exists", () => {
    const run = createRun({ conversation_id: "conv-123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    expect(row).toHaveAttribute("tabindex", "0");
  });

  it("is not focusable when no conversation exists", () => {
    const run = createRun({ conversation_id: null });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    expect(row).toHaveAttribute("tabindex", "-1");
  });

  it("uses staging host when on staging domain", async () => {
    Object.defineProperty(window, "location", {
      value: { hostname: "staging.all-hands.dev" },
      writable: true,
    });

    const user = userEvent.setup();
    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    await user.click(row);

    expect(mockWindowOpen).toHaveBeenCalledWith(
      "https://staging.all-hands.dev/conversations/conv-abc123",
      "_blank",
    );
  });

  it("uses app.all-hands.dev for localhost", async () => {
    Object.defineProperty(window, "location", {
      value: { hostname: "localhost" },
      writable: true,
    });

    const user = userEvent.setup();
    const run = createRun({ conversation_id: "conv-abc123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    await user.click(row);

    expect(mockWindowOpen).toHaveBeenCalledWith(
      "https://app.all-hands.dev/conversations/conv-abc123",
      "_blank",
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

  it("has appropriate aria-label for accessible row with conversation", () => {
    const run = createRun({ conversation_id: "conv-123" });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    expect(row.getAttribute("aria-label")).toContain("View conversation");
  });

  it("has appropriate aria-label for row without conversation", () => {
    const run = createRun({ conversation_id: null });
    render(<ActivityLogItem run={run} />);

    const row = screen.getByRole("button");
    expect(row.getAttribute("aria-label")).toBe(
      "AUTOMATIONS$DETAIL$NO_CONVERSATION",
    );
  });
});
