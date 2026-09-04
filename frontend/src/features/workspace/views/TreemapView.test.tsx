import { render, screen } from '@testing-library/react'
import { projectKeyResults } from "./keyResultModel";
import { buildTreemapGroups } from "./viewModels";
import type { WorkspaceTask } from "./types";
import { cloneElement, isValidElement, type ReactElement } from "react";

/** Recording chart: captures the real data and invokes the real content element. */
type CapturedLeaf = { taskId: string; name: string; nodeKind?: string };
type CapturedGroup = { name: string; nodeKind?: string; groupKey?: string; children: CapturedLeaf[] };
type CapturedObjective = { name: string; nodeKind?: string; objectiveId?: string; children: CapturedGroup[] };
const capturedCharts: { data: CapturedObjective[]; content: ReactElement }[] = [];
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactElement }) => children,
  Treemap: (props: { data: CapturedObjective[]; content: ReactElement }) => {
    capturedCharts.push({ data: props.data, content: props.content });
    return null;
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
    render(invokeTreemapContent({ ...leaf, x: 0, y: 0, width: 200, height: 120, depth: 3 }));

    expect(screen.getByText("Outcome task T-1")).toBeInTheDocument();
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
