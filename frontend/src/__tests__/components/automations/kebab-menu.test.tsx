import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect } from "vitest";
import { KebabMenu } from "#/components/automations/kebab-menu";
import type { KebabMenuItem } from "#/components/automations/kebab-menu";

const createItems = (): KebabMenuItem[] => [
  {
    label: "Edit",
    icon: <span data-testid="edit-icon" />,
    onClick: vi.fn(),
  },
  {
    label: "Delete",
    icon: <span data-testid="delete-icon" />,
    onClick: vi.fn(),
    variant: "danger",
  },
];

describe("KebabMenu", () => {
  it("does not show menu items initially", () => {
    render(<KebabMenu items={createItems()} />);

    expect(screen.queryByText("Edit")).not.toBeInTheDocument();
  });

  it("shows menu items when trigger button is clicked", async () => {
    const user = userEvent.setup();
    render(<KebabMenu items={createItems()} />);

    await user.click(screen.getByLabelText("Automation actions"));

    expect(screen.getByText("Edit")).toBeInTheDocument();
    expect(screen.getByText("Delete")).toBeInTheDocument();
  });

  it("calls item onClick and closes menu when an item is clicked", async () => {
    const items = createItems();
    const user = userEvent.setup();
    render(<KebabMenu items={items} />);

    await user.click(screen.getByLabelText("Automation actions"));
    await user.click(screen.getByText("Edit"));

    expect(items[0].onClick).toHaveBeenCalledOnce();
    expect(screen.queryByText("Edit")).not.toBeInTheDocument();
  });

  it("stops event propagation on menu interactions", async () => {
    const parentClick = vi.fn();
    const user = userEvent.setup();
    render(
      <div onClick={parentClick} onKeyDown={parentClick} role="presentation">
        <KebabMenu items={createItems()} />
      </div>,
    );

    await user.click(screen.getByLabelText("Automation actions"));

    expect(parentClick).not.toHaveBeenCalled();
  });
});
