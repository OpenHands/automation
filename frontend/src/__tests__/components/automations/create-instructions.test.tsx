import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect } from "vitest";
import { CreateInstructions } from "#/components/automations/create-instructions";

describe("CreateInstructions", () => {
  describe("non-collapsible mode (default)", () => {
    it("renders the how to create title", () => {
      render(<CreateInstructions />);

      expect(
        screen.getByText("AUTOMATIONS$EMPTY_HOW_TO_CREATE_TITLE"),
      ).toBeInTheDocument();
    });

    it("renders the plugin option with command", () => {
      render(<CreateInstructions />);

      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_DESC"),
      ).toBeInTheDocument();

      const codeElement = screen.getByRole("code");
      expect(codeElement).toHaveTextContent("/openhands-automation create");
    });

    it("renders the conversation option with link", () => {
      render(<CreateInstructions />);

      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_CONVERSATION_TITLE"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_CONVERSATION_DESC"),
      ).toBeInTheDocument();

      const link = screen.getByRole("link", {
        name: /AUTOMATIONS\$EMPTY_START_CONVERSATION/,
      });
      expect(link).toHaveAttribute("href", "/");
    });

    it("renders the documentation link", () => {
      render(<CreateInstructions />);

      const docsLink = screen.getByRole("link", {
        name: "AUTOMATIONS$EMPTY_LEARN_MORE",
      });
      expect(docsLink).toHaveAttribute(
        "href",
        "https://docs.openhands.dev/openhands/usage/automations/overview",
      );
      expect(docsLink).toHaveAttribute("target", "_blank");
      expect(docsLink).toHaveAttribute("rel", "noopener noreferrer");
    });
  });

  describe("collapsible mode", () => {
    it("starts collapsed and shows only the title button", () => {
      render(<CreateInstructions collapsible />);

      // Title button should be visible
      expect(
        screen.getByRole("button", {
          name: "AUTOMATIONS$EMPTY_HOW_TO_CREATE_TITLE",
        }),
      ).toBeInTheDocument();

      // Content should not be visible
      expect(
        screen.queryByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE"),
      ).not.toBeInTheDocument();
    });

    it("expands when clicked to show content", async () => {
      const user = userEvent.setup();
      render(<CreateInstructions collapsible />);

      // Click to expand
      await user.click(
        screen.getByRole("button", {
          name: "AUTOMATIONS$EMPTY_HOW_TO_CREATE_TITLE",
        }),
      );

      // Content should now be visible
      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_CONVERSATION_TITLE"),
      ).toBeInTheDocument();
    });

    it("collapses when clicked again", async () => {
      const user = userEvent.setup();
      render(<CreateInstructions collapsible />);

      const button = screen.getByRole("button", {
        name: "AUTOMATIONS$EMPTY_HOW_TO_CREATE_TITLE",
      });

      // Expand
      await user.click(button);
      expect(
        screen.getByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE"),
      ).toBeInTheDocument();

      // Collapse
      await user.click(button);
      expect(
        screen.queryByText("AUTOMATIONS$EMPTY_OPTION_PLUGIN_TITLE"),
      ).not.toBeInTheDocument();
    });
  });
});
