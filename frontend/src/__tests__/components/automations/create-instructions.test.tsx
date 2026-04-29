import { render, screen } from "@testing-library/react";
import { CreateInstructions } from "#/components/automations/create-instructions";

describe("CreateInstructions", () => {
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
