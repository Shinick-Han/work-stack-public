import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import type { KeyResultNode } from "./keyResultModel";
import { KeyResultCatalog, TaskOutcomeChipList } from "./KeyResultPresentation";
import type { OutcomeChip } from "./keyResultViewModel";

function node(overrides: Partial<KeyResultNode> = {}): KeyResultNode {
  return {
    key: "ws|W1|kr|O-A|KR-1",
    workspaceId: "W1",
    objectiveId: "O-A",
    keyResultId: "KR-1",
    objectiveTitle: "Objective A",
    objectiveStatus: "active",
    objectiveRevision: 2,
    text: "A outcome",
    target: null,
    status: null,
    recordedProgress: null,
    linkedTaskIds: [],
    counts: { total: 0, open: 0, started: 0, done: 0, dropped: 0 },
    visibleTaskIds: [],
    visibleCounts: { total: 0, open: 0, started: 0, done: 0, dropped: 0 },
    ...overrides,
  };
}

test("a zero-linked Key Result is listed with explicit Unrecorded progress", () => {
  render(<KeyResultCatalog keyResults={[node()]} />);

  const item = within(screen.getByRole("region", { name: "Outcomes" })).getByRole("listitem");
  expect(within(item).getByText("O-A · KR-1")).toBeInTheDocument();
  expect(within(item).getByText("Unrecorded")).toBeInTheDocument();
  expect(within(item).getByText("0 linked · 0 open · 0 started · 0 done · 0 dropped")).toBeInTheDocument();
  expect(within(item).getByText("0 visible of 0 linked")).toBeInTheDocument();
});

test("a recorded zero is shown as a measurement, not as unrecorded", () => {
  render(<KeyResultCatalog keyResults={[node({ recordedProgress: 0 })]} />);

  expect(screen.getByText("Recorded progress 0")).toBeInTheDocument();
  expect(screen.queryByText("Unrecorded")).toBeNull();
});

test("target and status appear only when recorded, and no percentage is derived", () => {
  const { rerender } = render(<KeyResultCatalog keyResults={[node()]} />);
  expect(screen.queryByText(/^Target/)).toBeNull();
  expect(screen.queryByText(/^Status/)).toBeNull();

  rerender(<KeyResultCatalog keyResults={[node({
    target: "12",
    status: "active",
    counts: { total: 4, open: 1, started: 1, done: 1, dropped: 1 },
    visibleCounts: { total: 2, open: 1, started: 1, done: 0, dropped: 0 },
  })]} />);

  expect(screen.getByText("Target 12")).toBeInTheDocument();
  expect(screen.getByText("Status active")).toBeInTheDocument();
  expect(screen.getByText("4 linked · 1 open · 1 started · 1 done · 1 dropped")).toBeInTheDocument();
  expect(screen.getByText("2 visible of 4 linked")).toBeInTheDocument();
  expect(screen.queryByText(/%/)).toBeNull();
});

test("selecting a catalog entry reports the exact scoped pair", async () => {
  const onSelectOutcome = vi.fn();
  render(<KeyResultCatalog keyResults={[node()]} onSelectOutcome={onSelectOutcome} />);

  await userEvent.click(screen.getByRole("button", { name: /O-A · KR-1/ }));

  expect(onSelectOutcome).toHaveBeenCalledExactlyOnceWith({
    objectiveId: "O-A",
    keyResultId: "KR-1",
  });
});

test("the selected outcome is marked pressed and an empty catalog explains itself", () => {
  const { rerender } = render(
    <KeyResultCatalog
      keyResults={[node()]}
      onSelectOutcome={vi.fn()}
      selected={{ objectiveId: "O-A", keyResultId: "KR-1" }}
    />,
  );
  expect(screen.getByRole("button", { name: /O-A · KR-1/ })).toHaveAttribute("aria-pressed", "true");

  rerender(<KeyResultCatalog keyResults={[]} />);
  expect(screen.getByText("No outcomes are defined yet.")).toBeInTheDocument();
});

const chip = (overrides: Partial<OutcomeChip> = {}): OutcomeChip => ({
  key: "T-1|O-A · KR-1",
  objectiveId: "O-A",
  keyResultId: "KR-1",
  label: "O-A · KR-1",
  resolved: true,
  text: "A outcome",
  reason: null,
  ...overrides,
});

test("a Task chip selects its pair without activating the surrounding card", async () => {
  const onSelectOutcome = vi.fn()
  const onCardActivate = vi.fn()
  render(
    <div onClick={onCardActivate}>
      <TaskOutcomeChipList chips={[chip()]} onSelectOutcome={onSelectOutcome} />
    </div>,
  );

  await userEvent.click(screen.getByRole("button", { name: "Filter by outcome O-A KR-1" }));

  expect(onSelectOutcome).toHaveBeenCalledExactlyOnceWith({
    objectiveId: "O-A",
    keyResultId: "KR-1",
  });
  expect(onCardActivate).not.toHaveBeenCalled();
});

test("unresolved chips stay visible and labelled, and no refs reads as unassigned", () => {
  const { rerender } = render(
    <TaskOutcomeChipList chips={[chip({ resolved: false, text: null, label: "O-A · KR-9", keyResultId: "KR-9" })]} />,
  );
  expect(screen.getByText("O-A · KR-9")).toBeInTheDocument();
  expect(screen.getByText("Unresolved outcome")).toBeInTheDocument();

  rerender(<TaskOutcomeChipList chips={[]} />);
  expect(screen.getByText("Unassigned outcome")).toBeInTheDocument();
});


test("NG4 an unresolved chip explains why it cannot resolve", () => {
  render(
    <TaskOutcomeChipList
      chips={[chip({
        resolved: false,
        text: null,
        reason: "missing-key-result",
        label: "O-A · KR-404",
        keyResultId: "KR-404",
      })]}
    />,
  );

  const button = screen.getByRole("button", { name: /Filter by outcome O-A KR-404/ });
  expect(button).toHaveAccessibleDescription(/no matching key result/i);
  expect(screen.getByText("O-A · KR-404")).toBeInTheDocument();
});

test("NG4 healthy control: a resolved chip has no unresolved explanation", () => {
  render(<TaskOutcomeChipList chips={[chip()]} />);

  expect(screen.queryByText(/no matching key result/i)).toBeNull();
});
