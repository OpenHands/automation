import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect } from "vitest";
import { SearchInput } from "#/components/automations/search-input";

describe("SearchInput", () => {
  it("calls onChange when user types", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<SearchInput value="" onChange={onChange} />);

    await user.type(
      screen.getByPlaceholderText("AUTOMATIONS$SEARCH_PLACEHOLDER"),
      "test",
    );

    expect(onChange).toHaveBeenCalled();
    expect(onChange).toHaveBeenLastCalledWith("t");
  });

  it("displays the current value", () => {
    render(<SearchInput value="security" onChange={vi.fn()} />);

    expect(
      screen.getByPlaceholderText("AUTOMATIONS$SEARCH_PLACEHOLDER"),
    ).toHaveValue("security");
  });
});
