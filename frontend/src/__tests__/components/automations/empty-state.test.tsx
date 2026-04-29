import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { EmptyState } from "#/components/automations/empty-state";

describe("EmptyState", () => {
  it("renders empty state message", () => {
    render(<EmptyState />);

    expect(screen.getByText("AUTOMATIONS$EMPTY")).toBeInTheDocument();
  });

  it("renders how to create automation section", () => {
    render(<EmptyState />);

    expect(
      screen.getByText("AUTOMATIONS$EMPTY_HOW_TO_CREATE_TITLE"),
    ).toBeInTheDocument();
  });

  it("renders plugin option with command", () => {
    render(<EmptyState />);

    expect(
      screen.getByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_DESC"),
    ).toBeInTheDocument();
    // The command is displayed in a code block
    const codeBlock = screen.getByRole("code");
    expect(codeBlock).toHaveTextContent("/openhands-automation create");
  });

  it("renders conversation option with link", () => {
    render(<EmptyState />);

    expect(
      screen.getByText("AUTOMATIONS$EMPTY_OPTION_CONVERSATION_TITLE"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("AUTOMATIONS$EMPTY_OPTION_CONVERSATION_DESC"),
    ).toBeInTheDocument();

    const conversationLink = screen.getByRole("link", {
      name: /AUTOMATIONS\$EMPTY_START_CONVERSATION/,
    });
    expect(conversationLink).toBeInTheDocument();
    expect(conversationLink).toHaveAttribute("href", "/conversations");
  });

  it("renders documentation link", () => {
    render(<EmptyState />);

    const docsLink = screen.getByRole("link", {
      name: "AUTOMATIONS$EMPTY_LEARN_MORE",
    });
    expect(docsLink).toBeInTheDocument();
    expect(docsLink).toHaveAttribute(
      "href",
      "https://docs.openhands.dev/openhands/usage/automations/overview",
    );
    expect(docsLink).toHaveAttribute("target", "_blank");
    expect(docsLink).toHaveAttribute("rel", "noopener noreferrer");
  });
});
