import { act, fireEvent, render, screen } from '@testing-library/react'
import { projectKeyResults } from "./keyResultModel";
import { buildTreemapGroups } from "./viewModels";
import type { WorkspaceTask } from "./types";
import { cloneElement, isValidElement, type ReactElement } from "react";
import { localViewStorageKey } from "./localViewState";
import { TREEMAP_DRAG_THRESHOLD_PX } from "./treemapViewState";

/** Recording chart: captures the real data and invokes the real content element. */
type CapturedLeaf = { taskId: string; name: string; nodeKind?: string; children?: unknown[] };
type CapturedGroup = { name: string; nodeKind?: string; groupKey?: string; children: CapturedLeaf[] };
type CapturedObjective = { name: string; nodeKind?: string; objectiveId?: string; children: CapturedGroup[] };
const capturedCharts: { data: CapturedObjective[]; content: ReactElement }[] = [];

function flattenLeaves(nodes: Array<{ taskId?: string; nodeKind?: string; children?: unknown[] }>): CapturedLeaf[] {
  const leaves: CapturedLeaf[] = [];
  for (const node of nodes) {
    if (node.taskId && (node.nodeKind === "task" || !node.children?.length)) {
      leaves.push(node as CapturedLeaf);
    } else if (node.children) {
      leaves.push(...flattenLeaves(node.children as never));
    }
  }
  return leaves;
}

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactElement }) => children,
  Treemap: (props: { data: CapturedObjective[]; content: ReactElement }) => {
    capturedCharts.push({ data: props.data, content: props.content });
    const leaves = flattenLeaves(props.data as never);
    return (
      <div data-testid="treemap-chart">
        {leaves.map((leaf) => (
          <div key={leaf.taskId} data-testid={`treemap-leaf-${leaf.taskId}`}>
            {isValidElement(props.content)
              ? (
                <svg>
                  {cloneElement(props.content as ReactElement, {
                    ...leaf,
                    x: 0,
                    y: 0,
                    width: 220,
                    height: 100,
                    depth: 2,
                  } as never)}
                </svg>
              )
              : null}
          </div>
        ))}
      </div>
    );
  },
}));

function capturedTreemapData(): CapturedObjective[] {
  return capturedCharts[capturedCharts.length - 1]?.data ?? [];
}

function invokeTreemapContent(nodeProps: Record<string, unknown>) {
  const element = capturedCharts[capturedCharts.length - 1]?.content;
  if (!isValidElement(element)) throw new Error("no chart content captured");
  return cloneElement(element as ReactElement, nodeProps);
}
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { TreemapObjectiveNavigator, TreemapView } from './TreemapView'

describe('TreemapObjectiveNavigator', () => {
  it('focuses an aligned Objective without selecting a Task', async () => {
    const onSelectObjective = vi.fn()

    render(
      <TreemapObjectiveNavigator
        groups={[
          { name: 'O-1 · Release quality', objectiveId: 'O-1', children: [] },
          { name: 'Multiple objectives', objectiveId: 'multiple', children: [] },
          { name: 'Unaligned / Operations', objectiveId: 'none', children: [] },
        ]}
        onSelectObjective={onSelectObjective}
      />,
    )

    const group = screen.getByRole('button', { name: 'Focus objective O-1' })
    await userEvent.click(group)
    expect(onSelectObjective).toHaveBeenCalledWith('O-1')
    expect(screen.queryByRole('button', { name: 'Focus objective multiple' })).not.toBeInTheDocument()
  })

  it('keeps the unaligned operations group informational', () => {
    render(
      <TreemapObjectiveNavigator
        groups={[{ name: 'Unaligned / Operations', objectiveId: 'none', children: [] }]}
        onSelectObjective={vi.fn()}
      />,
    )

    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })
})


describe("NG1 typed hierarchy through the real renderer", () => {
  const objective = (id: string, keyResults: { id: string; text: string }[]) => ({
    id, objective: `${id} objective`, revision: 1, key_results: keyResults,
  });
  const hierarchyTask = (id: string, extra: Partial<WorkspaceTask> = {}): WorkspaceTask => ({
    id,
    title: `Outcome task ${id}`,
    status: "open",
    priority: "P2",
    due: null,
    tags: [],
    objective_ids: [],
    dependencies: [],
    subtasks: [],
    context_count: 0,
    revision: 1,
    ...extra,
  });

  const first = hierarchyTask("T-1", {
    objective_ids: ["O-A"],
    key_result_refs: [{ objective_id: "O-A", key_result_id: "K1" }],
  });
  const second = hierarchyTask("T-2", {
    objective_ids: ["O-A"],
    key_result_refs: [{ objective_id: "O-A", key_result_id: "K2" }],
  });
  const operational = hierarchyTask("T-3");
  const tasks = [first, second, operational];
  const projection = projectKeyResults({
    workspaceId: "W1",
    tasks: tasks as never,
    objectives: [objective("O-A", [{ id: "K1", text: "First" }, { id: "K2", text: "Second" }])] as never,
  });

  function renderHierarchy() {
    return render(
      <TreemapView
        tasks={tasks}
        objectives={[{ id: "O-A", objective: "O-A objective" }] as never}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
      />,
    );
  }

  it("supplies Objective -> Key Result -> Task with typed identities", () => {
    renderHierarchy();

    const data = capturedTreemapData();
    expect(data[0].nodeKind).toBe("objective");
    const objectiveNode = data.find((node) => node.objectiveId === "O-A");
    expect(objectiveNode?.children.map((group) => group.nodeKind)).toEqual(["key-result", "key-result"]);
    expect(new Set(objectiveNode?.children.map((group) => group.groupKey)).size).toBe(2);
    const leaves = data.flatMap((node) => node.children.flatMap((group) => group.children));
    expect(leaves.map((leaf) => leaf.taskId).sort()).toEqual(["T-1", "T-2", "T-3"]);
    expect(new Set(leaves.map((leaf) => leaf.taskId)).size).toBe(3);
  });

  it("keeps the Operations context for an unaligned Task", () => {
    renderHierarchy();

    const data = capturedTreemapData();
    const operations = data.find((node) => node.name === "Operations");
    expect(operations).toBeDefined();
    expect(operations?.children[0].name).toBe("Unassigned outcome");
    expect(operations?.children[0].children.map((leaf) => leaf.taskId)).toEqual(["T-3"]);
  });

  it("renders a Task leaf by kind rather than by depth", () => {
    renderHierarchy();

    const leaf = capturedTreemapData()
      .flatMap((node) => node.children.flatMap((group) => group.children))
      .find((item) => item.taskId === "T-1")!;
    render(<svg>{invokeTreemapContent({ ...leaf, x: 0, y: 0, width: 200, height: 120, depth: 3 })}</svg>);

    expect(screen.getAllByText("Outcome task T-1").length).toBeGreaterThan(0);
  });
});


describe("NG-R1 navigator uses typed identity", () => {
  const objective = (id: string, keyResults: { id: string; text: string }[]) =>
    ({ id, objective: `${id} objective`, revision: 1, key_results: keyResults }) as never;
  const navTask = (id: string, extra: Record<string, unknown> = {}) => ({
    id, title: `Task ${id}`, status: "open", priority: "P2", due: null, tags: [],
    objective_ids: [], dependencies: [], subtasks: [], context_count: 0, revision: 1, ...extra,
  }) as never;

  it("keeps legally named Objectives navigable and the Operations bucket not", async () => {
    const tasks = [
      navTask("T-1", { objective_ids: ["none"], key_result_refs: [{ objective_id: "none", key_result_id: "K1" }] }),
      navTask("T-2", { objective_ids: ["multiple"], key_result_refs: [{ objective_id: "multiple", key_result_id: "K1" }] }),
      navTask("T-3"),
    ];
    const objectives = [
      objective("none", [{ id: "K1", text: "Legally named none" }]),
      objective("multiple", [{ id: "K1", text: "Legally named multiple" }]),
    ];
    const projection = projectKeyResults({ workspaceId: "W1", tasks, objectives });
    const onSelectObjective = vi.fn();

    render(
      <TreemapView
        tasks={tasks}
        objectives={objectives}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={onSelectObjective}
      />,
    );

    const buttons = screen.getAllByRole("button", { name: /^Focus objective/ });
    expect(buttons.map((button) => button.getAttribute("aria-label")).sort()).toEqual([
      "Focus objective multiple",
      "Focus objective none",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Focus objective none" }));
    expect(onSelectObjective).toHaveBeenCalledExactlyOnceWith("none");
  });

  it("healthy control: one button per real Objective with two key results", () => {
    const tasks = [
      navTask("T-1", { objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "K1" }] }),
      navTask("T-2", { objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "K2" }] }),
    ];
    const objectives = [objective("O-A", [{ id: "K1", text: "First" }, { id: "K2", text: "Second" }])];
    const projection = projectKeyResults({ workspaceId: "W1", tasks, objectives });

    render(
      <TreemapView
        tasks={tasks}
        objectives={objectives}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("button", { name: /^Focus objective/ })).toHaveLength(1);
  });
});


describe("GN5 the typed Operations identity renders instead of crashing", () => {
  const objective = (id: string, keyResults: { id: string; text: string }[]) =>
    ({ id, objective: `${id} objective`, revision: 1, key_results: keyResults }) as never;
  const gnTask = (id: string, extra: Record<string, unknown> = {}) => ({
    id, title: `Task ${id}`, status: "open", priority: "P2", due: null, tags: [],
    objective_ids: [], dependencies: [], subtasks: [], context_count: 0, revision: 1, ...extra,
  }) as never;

  function renderWithOperations(objectiveId: string) {
    const tasks = [
      gnTask("T-1", { objective_ids: [objectiveId], key_result_refs: [{ objective_id: objectiveId, key_result_id: "K1" }] }),
      gnTask("T-OPS"),
    ];
    const objectives = [objective(objectiveId, [{ id: "K1", text: `${objectiveId} outcome` }])];
    const projection = projectKeyResults({ workspaceId: "W1", tasks, objectives });
    const onSelectObjective = vi.fn();
    render(
      <TreemapView
        tasks={tasks}
        objectives={objectives}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={onSelectObjective}
      />,
    );
    return { onSelectObjective };
  }

  it.each(["none", "multiple", "O-A"])(
    "renders the real chart content for Objective %s alongside an Operations Task",
    (objectiveId) => {
      const { onSelectObjective } = renderWithOperations(objectiveId);
      const data = capturedTreemapData();
      const operations = data.find((node) => node.objectiveId == null);
      expect(operations).toBeDefined();

      // The REAL content element is invoked for the Operations group; a null
      // parent identity must render, not throw.
      for (const node of data) {
        expect(() => render(invokeTreemapContent({
          x: 0, y: 0, width: 200, height: 120,
          name: node.name, nodeKind: node.nodeKind, depth: 1,
          objectiveId: (node as { objectiveId?: string | null }).objectiveId ?? null,
        }))).not.toThrow();
      }

      expect(screen.getAllByRole("button", { name: /^Focus objective/ }).map(
        (button) => button.getAttribute("aria-label"),
      )).toEqual([`Focus objective ${objectiveId}`]);
      expect(screen.queryByRole("button", { name: "Focus objective Unaligned / Operations" })).not.toBeInTheDocument();
    },
  );

  it("healthy control: an ordinary Objective keeps the three-level hierarchy and the exact 2.6 area", () => {
    const tasks = [gnTask("T-1", { objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "K1" }] })];
    const objectives = [objective("O-A", [{ id: "K1", text: "Outcome" }])];
    const projection = projectKeyResults({ workspaceId: "W1", tasks, objectives });

    render(
      <TreemapView
        tasks={tasks}
        objectives={objectives}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
      />,
    );

    const data = capturedTreemapData();
    expect(data).toHaveLength(1);
    expect(data[0].children).toHaveLength(1);
    expect(data[0].children[0].children).toHaveLength(1);
    const leaf = data[0].children[0].children[0] as CapturedLeaf & { size?: number };
    expect(leaf.taskId).toBe("T-1");
    // The outcome hierarchy reuses the legacy area verbatim; it never recomputes it.
    const legacy = buildTreemapGroups(tasks, objectives);
    expect(leaf.size).toBe(legacy.flatMap((group) => group.children).find((child) => child.taskId === "T-1")?.size);
  });
});

const WORKSPACE = "ws-treemap-drag";
const KEY = localViewStorageKey(WORKSPACE, "treemap");

function dragTask(id: string, extra: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    id,
    title: `Task ${id}`,
    status: "open",
    priority: "P2",
    due: null,
    tags: [],
    objective_ids: ["O-1"],
    dependencies: [],
    subtasks: [],
    context_count: 0,
    revision: 1,
    ...extra,
  };
}

function envelope(data: unknown, revision = 1, workspaceId = WORKSPACE) {
  return {
    schemaVersion: 1,
    workspaceId,
    view: "treemap",
    revision,
    writtenAt: 10,
    writerId: "peer",
    data,
  };
}

function withDropTarget<T>(target: Element, run: () => T): T {
  const doc = document as Document & {
    elementFromPoint: (x: number, y: number) => Element | null;
    elementsFromPoint?: (x: number, y: number) => Element[];
  };
  const previousPoint = typeof doc.elementFromPoint === "function"
    ? doc.elementFromPoint.bind(doc)
    : () => null;
  const previousStack = typeof doc.elementsFromPoint === "function"
    ? doc.elementsFromPoint.bind(doc)
    : undefined;
  doc.elementFromPoint = () => target;
  doc.elementsFromPoint = () => [target];
  try {
    return run();
  } finally {
    doc.elementFromPoint = previousPoint;
    if (previousStack) doc.elementsFromPoint = previousStack;
  }
}

function treemapSurface() {
  const node = document.querySelector("[data-treemap-surface]");
  if (!(node instanceof HTMLElement)) throw new Error("missing treemap surface");
  return node;
}

function pointerDrag(from: Element, to: Element, distance = TREEMAP_DRAG_THRESHOLD_PX + 2) {
  withDropTarget(to, () => {
    act(() => {
      fireEvent.pointerDown(from, {
        button: 0,
        buttons: 1,
        clientX: 10,
        clientY: 10,
        pointerId: 1,
      });
      fireEvent.pointerMove(treemapSurface(), { clientX: 10 + distance, clientY: 10, buttons: 1, pointerId: 1 });
      fireEvent.pointerUp(treemapSurface(), { clientX: 40, clientY: 12, button: 0, pointerId: 1 });
    });
  });
}

describe("Treemap local drag and persistence", () => {
  const objectives = [
    { id: "O-1", objective: "First" },
    { id: "O-2", objective: "Second" },
  ];
  const allTasks = [
    dragTask("T-1"),
    dragTask("T-2"),
    dragTask("T-3", { objective_ids: ["O-2"] }),
    dragTask("T-multi", { objective_ids: ["O-1", "O-2"] }),
    dragTask("T-ops", { objective_ids: [] }),
  ];

  beforeEach(() => {
    window.localStorage.clear();
    capturedCharts.length = 0;
  });

  function renderMap(options: {
    workspaceId?: string;
    tasks?: WorkspaceTask[];
    reference?: WorkspaceTask[];
    onSelectTask?: ReturnType<typeof vi.fn>;
    onSelectObjective?: ReturnType<typeof vi.fn>;
    keyResultProjection?: ReturnType<typeof projectKeyResults>;
  } = {}) {
    const props = {
      workspaceId: options.workspaceId ?? WORKSPACE,
      tasks: options.tasks ?? allTasks,
      referenceTasks: options.reference ?? allTasks,
      objectives,
      onSelectTask: options.onSelectTask ?? vi.fn(),
      onSelectObjective: options.onSelectObjective ?? vi.fn(),
      keyResultProjection: options.keyResultProjection,
    };
    return { ...render(<TreemapView {...props} />), props };
  }

  it("P1 keeps below-threshold handle interaction as focus only and writes once after an activated drop", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const view = renderMap();
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    handle.focus();
    expect(handle).toHaveFocus();
    act(() => {
      fireEvent.pointerDown(handle, { button: 0, buttons: 1, clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerMove(treemapSurface(), { clientX: 10 + TREEMAP_DRAG_THRESHOLD_PX - 1, clientY: 10, buttons: 1, pointerId: 1 });
      fireEvent.pointerUp(treemapSurface(), { clientX: 12, clientY: 10, button: 0, pointerId: 1 });
    });
    expect(view.props.onSelectTask).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(KEY)).toBeNull();

    pointerDrag(handle, screen.getByRole("button", { name: "Move task T-2" }));
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string);
    expect(stored.revision).toBe(1);
    expect(stored.view).toBe("treemap");
    expect(stored.data.orders[0].ids.slice(0, 2)).toEqual(["T-2", "T-1"]);
    expect(view.props.onSelectTask).not.toHaveBeenCalled();
    expect(view.props.onSelectObjective).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("P2 restores the snapshot and writes nothing when Escape cancels an active drag", () => {
    renderMap();
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    act(() => {
      fireEvent.pointerDown(handle, { button: 0, buttons: 1, clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerMove(treemapSurface(), { clientX: 10 + TREEMAP_DRAG_THRESHOLD_PX + 2, clientY: 10, buttons: 1, pointerId: 1 });
    });
    expect(screen.getByText(/Picked up task T-1/)).toBeInTheDocument();
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect(screen.getByText("Move cancelled")).toBeInTheDocument();
  });

  it("K1 reorders from the handle with arrows, keeps focus, and announces commit", async () => {
    const view = renderMap();
    const handle = screen.getByRole("button", { name: "Move objective O-1" });
    handle.focus();
    await userEvent.keyboard("{Enter}{ArrowRight}{Enter}");
    expect(screen.getByRole("button", { name: "Move objective O-1" })).toHaveFocus();
    expect(view.props.onSelectObjective).not.toHaveBeenCalled();
    expect(view.props.onSelectTask).not.toHaveBeenCalled();
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string);
    expect(stored.data.orders.some((item: { ids: string[] }) => item.ids[0] === "O-2")).toBe(true);
    expect(screen.getByText(/Moved objective O-1 to position 2 of 2/)).toBeInTheDocument();
  });

  it("TR1/TR2 keep navigator and Recharts order local and leave a multi-Objective Task in its bucket", () => {
    renderMap();
    const data = capturedTreemapData();
    expect(data.map((group) => group.objectiveId)).toEqual(["O-1", "O-2", "multiple", "none"]);
    const multi = data.find((group) => group.objectiveId === "multiple");
    expect(multi?.children.map((leaf) => (leaf as { taskId?: string }).taskId ?? (leaf as never))).toBeTruthy();
    pointerDrag(
      screen.getByRole("button", { name: "Move task T-multi" }),
      screen.getByRole("button", { name: "Move task T-multi" }),
    );
    const after = capturedTreemapData();
    const still = after.flatMap((group) => flattenLeaves([group] as never));
    expect(still.filter((leaf) => leaf.taskId === "T-multi")).toHaveLength(1);
    pointerDrag(
      screen.getByRole("button", { name: "Move task T-1" }),
      screen.getByRole("button", { name: "Move task T-ops" }),
    );
    expect(screen.getByText("Cannot move across groups")).toBeInTheDocument();
    const raw = window.localStorage.getItem(KEY);
    const leafOrder = raw
      ? JSON.parse(raw).data.orders.find((item: { scope: string }) => item.scope.includes("objective:O-1"))
      : null;
    expect(leafOrder).toBeFalsy();
  });

  it("F1 preserves hidden slots on visible reorder and does not write when filters change", () => {
    const view = renderMap();
    pointerDrag(
      screen.getByRole("button", { name: "Move task T-1" }),
      screen.getByRole("button", { name: "Move task T-2" }),
    );
    const before = window.localStorage.getItem(KEY);
    view.rerender(
      <TreemapView
        workspaceId={WORKSPACE}
        tasks={[dragTask("T-2"), dragTask("T-3", { objective_ids: ["O-2"] })]}
        referenceTasks={allTasks}
        objectives={objectives}
        onSelectTask={view.props.onSelectTask}
        onSelectObjective={view.props.onSelectObjective}
      />,
    );
    expect(window.localStorage.getItem(KEY)).toBe(before);
    expect(screen.queryByRole("button", { name: "Move task T-1" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move task T-2" })).toBeInTheDocument();
  });

  it("C1 buffers an external write during a gesture and rebases the affected scope on commit", () => {
    renderMap();
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    act(() => {
      fireEvent.pointerDown(handle, { button: 0, buttons: 1, clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerMove(treemapSurface(), { clientX: 20, clientY: 10, buttons: 1, pointerId: 1 });
    });
    const foreign = envelope({
      orders: [{ scope: "legacy/root", ids: ["O-2", "O-1"], touchedAt: 4 }],
    }, 4);
    window.localStorage.setItem(KEY, JSON.stringify(foreign));
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", {
        key: KEY,
        newValue: JSON.stringify(foreign),
        storageArea: window.localStorage,
      }));
    });
    withDropTarget(screen.getByRole("button", { name: "Move task T-2" }), () => {
      act(() => {
        fireEvent.pointerUp(treemapSurface(), { clientX: 40, clientY: 10, button: 0, pointerId: 1 });
      });
    });
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string);
    expect(stored.data.orders.some((item: { scope: string; ids: string[] }) => (
      item.scope === "legacy/root" && item.ids[0] === "O-2"
    ))).toBe(true);
    expect(stored.data.orders.some((item: { ids: string[] }) => item.ids[0] === "T-2")).toBe(true);
  });

  it("R1 reset clears this workspace treemap key and leaves other views alone", async () => {
    const tableKey = localViewStorageKey(WORKSPACE, "table");
    window.localStorage.setItem(KEY, JSON.stringify(envelope({
      orders: [{ scope: "legacy/root", ids: ["O-2", "O-1"], touchedAt: 1 }],
    })));
    window.localStorage.setItem(tableKey, '{"keep":true}');
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const view = renderMap();
    await userEvent.click(screen.getByRole("button", { name: "Reset treemap order" }));
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect(window.localStorage.getItem(tableKey)).toBe('{"keep":true}');
    expect(screen.getByText("Treemap order reset")).toBeInTheDocument();
    expect(view.props.onSelectTask).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("isolates workspaces, nested selection, Operations, and legal none/multiple Objective IDs", async () => {
    const view = renderMap();
    screen.getByRole("button", { name: "Move objective O-1" }).focus();
    await userEvent.keyboard("{Enter}{ArrowRight}{Enter}");
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).workspaceId).toBe(WORKSPACE);
    await userEvent.click(screen.getByRole("button", { name: "Move task T-1" }));
    expect(view.props.onSelectTask).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "T-1: Task T-1" }));
    expect(view.props.onSelectTask).toHaveBeenCalledWith("T-1");
    expect(screen.queryByRole("button", { name: "Move objective none" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move task T-ops" })).toBeInTheDocument();

    const named = [
      dragTask("T-n", { objective_ids: ["none"], key_result_refs: [{ objective_id: "none", key_result_id: "K1" }] }),
      dragTask("T-m", { objective_ids: ["multiple"], key_result_refs: [{ objective_id: "multiple", key_result_id: "K1" }] }),
      dragTask("T-ops2", { objective_ids: [] }),
    ];
    const namedObjectives = [
      { id: "none", objective: "Legally named none", key_results: [{ id: "K1", text: "N" }] },
      { id: "multiple", objective: "Legally named multiple", key_results: [{ id: "K1", text: "M" }] },
    ];
    const projection = projectKeyResults({ workspaceId: "W1", tasks: named as never, objectives: namedObjectives as never });
    view.rerender(
      <TreemapView
        workspaceId="ws-other"
        tasks={named}
        referenceTasks={named}
        objectives={namedObjectives}
        keyResultProjection={projection}
        onSelectTask={view.props.onSelectTask}
        onSelectObjective={view.props.onSelectObjective}
      />,
    );
    expect(screen.getByRole("button", { name: "Move objective none" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move objective multiple" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Move objective Unaligned / Operations" })).not.toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).workspaceId).toBe(WORKSPACE);
    expect(window.localStorage.getItem(localViewStorageKey("ws-other", "treemap"))).toBeNull();
  });

  it("keeps duplicate raw KR IDs in distinct Objectives on isolated scopes", () => {
    const tasks = [
      dragTask("T-a1", { objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "K1" }] }),
      dragTask("T-a2", { objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "K1" }] }),
      dragTask("T-b1", { objective_ids: ["O-B"], key_result_refs: [{ objective_id: "O-B", key_result_id: "K1" }] }),
    ];
    const namedObjectives = [
      { id: "O-A", objective: "A", key_results: [{ id: "K1", text: "Shared raw" }] },
      { id: "O-B", objective: "B", key_results: [{ id: "K1", text: "Shared raw" }] },
    ];
    const projection = projectKeyResults({ workspaceId: "W1", tasks: tasks as never, objectives: namedObjectives as never });
    render(
      <TreemapView
        workspaceId={WORKSPACE}
        tasks={tasks}
        referenceTasks={tasks}
        objectives={namedObjectives}
        keyResultProjection={projection}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
      />,
    );
    pointerDrag(
      screen.getByRole("button", { name: "Move task T-a1" }),
      screen.getByRole("button", { name: "Move task T-a2" }),
    );
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string);
    const scopes = stored.data.orders.map((item: { scope: string }) => item.scope);
    expect(new Set(scopes).size).toBe(scopes.length);
    expect(scopes.some((scope: string) => scope.startsWith("outcome/group:"))).toBe(true);
    expect(capturedTreemapData().flatMap((group) => group.children.flatMap((child) => child.children ?? []))
      .filter((leaf) => leaf.taskId === "T-b1")).toHaveLength(1);
  });

  it("cancels an activated pointer gesture on lostpointercapture without writing", () => {
    renderMap();
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    act(() => {
      fireEvent.pointerDown(handle, { button: 0, buttons: 1, clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerMove(treemapSurface(), { clientX: 10 + TREEMAP_DRAG_THRESHOLD_PX + 2, clientY: 10, buttons: 1, pointerId: 1 });
    });
    expect(screen.getByText(/Picked up task T-1/)).toBeInTheDocument();
    expect(handle).toHaveAttribute("aria-pressed", "true");
    act(() => {
      const lost = new Event("lostpointercapture", { bubbles: true });
      Object.defineProperty(lost, "buttons", { value: 1 });
      treemapSurface().dispatchEvent(lost);
    });
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect(screen.getByText("Move cancelled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move task T-1" })).not.toHaveAttribute("aria-pressed");
  });

  it("cancels an activated pointer gesture on pointercancel without writing", () => {
    renderMap();
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    act(() => {
      fireEvent.pointerDown(handle, { button: 0, buttons: 1, clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerMove(treemapSurface(), { clientX: 10 + TREEMAP_DRAG_THRESHOLD_PX + 2, clientY: 10, buttons: 1, pointerId: 1 });
    });
    act(() => {
      treemapSurface().dispatchEvent(new Event("pointercancel"));
    });
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect(screen.getByText("Move cancelled")).toBeInTheDocument();
  });

  it("does not undo a committed drop when releasePointerCapture fires lostpointercapture", () => {
    renderMap();
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    pointerDrag(
      handle,
      screen.getByRole("button", { name: "Move task T-2" }),
    );
    const before = window.localStorage.getItem(KEY);
    expect(before).toBeTruthy();
    act(() => {
      const lost = new Event("lostpointercapture", { bubbles: true });
      Object.defineProperty(lost, "buttons", { value: 0 });
      treemapSurface().dispatchEvent(lost);
    });
    expect(window.localStorage.getItem(KEY)).toBe(before);
    expect(screen.queryByText("Move cancelled")).not.toBeInTheDocument();
  });

  it("does not nest the move handle inside the cell selection control", () => {
    renderMap();
    const select = screen.getByRole("button", { name: "T-1: Task T-1" });
    const handle = screen.getByRole("button", { name: "Move task T-1" });
    expect(select.contains(handle)).toBe(false);
    pointerDrag(handle, screen.getByRole("button", { name: "T-2: Task T-2" }));
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string);
    expect(stored.data.orders[0].ids.slice(0, 2)).toEqual(["T-2", "T-1"]);
  });

  it("distinguishes legal none/multiple Objective IDs from synthetic buckets in legacy mode", () => {
    render(
      <TreemapView
        workspaceId={WORKSPACE}
        tasks={[
          dragTask("T-n", { objective_ids: ["none"] }),
          dragTask("T-ops", { objective_ids: [] }),
          dragTask("T-named", { objective_ids: ["multiple"] }),
          dragTask("T-multi", { objective_ids: ["O-1", "O-2"] }),
          dragTask("T-1", { objective_ids: ["O-1"] }),
        ]}
        referenceTasks={[
          dragTask("T-n", { objective_ids: ["none"] }),
          dragTask("T-ops", { objective_ids: [] }),
          dragTask("T-named", { objective_ids: ["multiple"] }),
          dragTask("T-multi", { objective_ids: ["O-1", "O-2"] }),
          dragTask("T-1", { objective_ids: ["O-1"] }),
        ]}
        objectives={[
          { id: "O-1", objective: "First" },
          { id: "O-2", objective: "Second" },
          { id: "none", objective: "Legal none" },
          { id: "multiple", objective: "Legal multiple" },
        ]}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Move objective none" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Focus objective none" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move objective multiple" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Focus objective multiple" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move objective O-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move task T-ops" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move task T-multi" })).toBeInTheDocument();
    const data = capturedTreemapData();
    expect(data.some((group) => group.objectiveId === "none" && group.name === "none")).toBe(true);
    expect(data.some((group) => group.name === "Unaligned / Operations")).toBe(true);
    expect(data.some((group) => group.objectiveId === "multiple" && group.name === "multiple")).toBe(true);
    expect(data.some((group) => group.name === "Multiple objectives")).toBe(true);
  });

  it("S1 fails closed on malformed storage without planning writes", () => {
    window.localStorage.setItem(KEY, "{not json");
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const view = renderMap();
    expect(screen.getByRole("button", { name: "Reset treemap order" })).toBeInTheDocument();
    expect(window.localStorage.getItem(KEY)).toBeNull();
    expect(view.props.onSelectTask).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});
