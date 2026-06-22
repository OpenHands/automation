import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PromptSection } from "#/components/automations/detail/prompt-section";

describe("PromptSection", () => {
  it("renders the section title and prompt text", () => {
    const prompt = "Review newly opened pull requests and flag risky changes.";
    render(<PromptSection prompt={prompt} />);

    expect(screen.getByText("AUTOMATIONS$DETAIL$PROMPT")).toBeInTheDocument();
    expect(screen.getByText(prompt)).toBeInTheDocument();
  });

  it("preserves whitespace in multi-line prompts", () => {
    const prompt = "Step 1: Scan the repo.\nStep 2: Report findings.";
    render(<PromptSection prompt={prompt} />);

    const el = screen.getByText(/Step 1: Scan the repo/);
    expect(el).toHaveClass("whitespace-pre-wrap");
  });
});
