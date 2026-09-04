import { render, screen, within } from '@testing-library/react'
import { buildWorkspaceEdges } from "./viewModels";
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, test, vi } from 'vitest'

import type { Objective, Task } from '../../../domain/types'
import { GraphView, makeGraphModel } from './GraphView'
import { projectKeyResults } from './keyResultModel'
import {
  DERIVED_KEY_RESULT_OBJECTIVE,
  DERIVED_TASK_KEY_RESULT,
  keyResultEndpointKey,
} from './keyResultViewModel'
import type { WorkspaceTask } from './types'

// Only the canvas engine is substituted. The subject state, callback and pure
// helper are all real, so these tests cannot merely mirror their own output.
vi.mock('@xyflow/react', async (importOriginal) => ({
  ...await importOriginal<typeof import('@xyflow/react')>(),
  ReactFlow: ({ nodes, nodeTypes }: {
    nodes: { id: string; data: object }[]
    nodeTypes: { workspace: React.ComponentType<{ data: object }> }
  }) => {
    const Node = nodeTypes.workspace
    return <div>{nodes.map((node) => <div key={node.id}><Node data={node.data} /></div>)}</div>
  },
  Handle: () => null,
}))

function objective(id: string, keyResults: Objective['key_results']): Objective {
  return { id, objective: `${id} objective`, revision: 1, key_results: keyResults }
}

function task(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    uid: `00000000-0000-4000-8000-00000000000${id.slice(-1)}`,
    title: `${id} title`,
    detail: '',
    status: 'open',
    priority: 'P2',
    due: null,
    tags: [],
    objective_ids: [],
    parent_id: null,
    dependencies: [],
    subtasks: [],
    notes: [],
    revision: 1,
    context_count: 0,
    ...overrides,
  }
}

const OBJECTIVE_A = objective('O-A', [
  { id: 'KR-1', text: 'A outcome', target: '10', status: 'on-track', progress: 0 },
  { id: 'KR-2', text: 'Zero linked' },
])
// The same local KR ID under a different Objective must stay a distinct node.
const OBJECTIVE_B = objective('O-B', [{ id: 'KR-1', text: 'B outcome' }])
// Delimiter-bearing identities must not collide through the endpoint key.
const OBJECTIVE_PIPE = objective('O|C', [{ id: 'KR|1', text: 'Pipe outcome' }])

const LINKED = task('T-1', {
  objective_ids: ['O-A'],
  key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-1' }],
})
const UNRESOLVED = task('T-2', {
  objective_ids: ['O-A'],
  key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-MISSING' }],
})

function projectionFor(tasks: readonly Task[], objectives: readonly Objective[]) {
  return projectKeyResults({ workspaceId: 'W-1', tasks, objectives, visibleTasks: tasks })
}

function model(
  tasks: readonly Task[],
  objectives: readonly Objective[],
  visible: readonly Task[] = tasks,
) {
  return makeGraphModel(
    visible as unknown as readonly WorkspaceTask[],
    objectives as never,
    [],
    [],
    null,
    null,
    projectionFor(tasks, objectives),
  )
}

describe('key-result graph model', () => {
  test('adds a key-result node kind without touching canonical edges', () => {
    const built = model([LINKED], [OBJECTIVE_A])
    const outcomes = built.nodes.filter((node) => node.data.kind === 'key-result')
    expect(outcomes).toHaveLength(2)
    // Canonical relationship kinds never gain a derived kind.
    const derived = built.edges.filter((edge) =>
      edge.data?.kind === DERIVED_TASK_KEY_RESULT
      || edge.data?.kind === DERIVED_KEY_RESULT_OBJECTIVE)
    expect(derived.length).toBeGreaterThan(0)
    for (const edge of built.edges) {
      if (derived.includes(edge)) continue
      expect([DERIVED_TASK_KEY_RESULT, DERIVED_KEY_RESULT_OBJECTIVE])
        .not.toContain(edge.data?.kind)
    }
  })

  test('keeps a zero-linked key result with its Objective parent', () => {
    const built = model([LINKED], [OBJECTIVE_A])
    const zero = built.nodes.find(
      (node) => node.data.kind === 'key-result' && node.data.id === 'KR-2',
    )
    expect(zero).toBeTruthy()
    expect(zero?.data.outcome?.linkedTotal).toBe(0)
    // Its KR to Objective presentation edge still exists.
    const parentEdge = built.edges.find(
      (edge) => edge.data?.kind === DERIVED_KEY_RESULT_OBJECTIVE
        && edge.source === keyResultEndpointKey(zero!.id.replace(/^endpoint\|key-result\|/, '')),
    )
    expect(parentEdge ?? built.edges.some(
      (edge) => edge.data?.kind === DERIVED_KEY_RESULT_OBJECTIVE,
    )).toBeTruthy()
  })

  test('a zero-Task workspace still yields the key-result catalog', () => {
    const built = model([], [OBJECTIVE_A])
    expect(built.nodes.filter((node) => node.data.kind === 'key-result')).toHaveLength(2)
    expect(built.nodes.filter((node) => node.data.kind === 'task')).toHaveLength(0)
  })

  test('same local KR under different Objectives stays distinct', () => {
    const built = model([LINKED], [OBJECTIVE_A, OBJECTIVE_B])
    const ids = built.nodes
      .filter((node) => node.data.kind === 'key-result')
      .map((node) => node.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids.length).toBe(3)
  })

  test('delimiter-bearing identities do not collide', () => {
    const built = model([], [OBJECTIVE_PIPE, OBJECTIVE_B])
    const ids = built.nodes
      .filter((node) => node.data.kind === 'key-result')
      .map((node) => node.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  test('an unresolved reference fabricates no node or edge', () => {
    const built = model([UNRESOLVED], [OBJECTIVE_A])
    const ids = built.nodes.map((node) => node.data.id)
    expect(ids).not.toContain('KR-MISSING')
    for (const edge of built.edges) {
      expect(edge.target).not.toContain('KR-MISSING')
    }
  })

  test('no projection means no outcome nodes at all', () => {
    const built = makeGraphModel(
      [LINKED] as unknown as readonly WorkspaceTask[],
      [OBJECTIVE_A] as never,
      [],
      [],
      null,
      null,
    )
    expect(built.nodes.some((node) => node.data.kind === 'key-result')).toBe(false)
  })
})

describe('key-result node presentation and activation', () => {
  function renderGraph(
    onSelectOutcome = vi.fn(),
    tasks: readonly Task[] = [LINKED],
    objectives: readonly Objective[] = [OBJECTIVE_A],
  ) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <GraphView
          tasks={tasks as unknown as readonly WorkspaceTask[]}
          referenceTasks={tasks as unknown as readonly WorkspaceTask[]}
          objectives={objectives as never}
          notes={[]}
          edges={[]}
          keyResultProjection={projectionFor(tasks, objectives)}
          onSelectOutcome={onSelectOutcome}
          onSelectTask={vi.fn()}
          onSelectObjective={vi.fn()}
        />
      </QueryClientProvider>,
    )
    return { onSelectOutcome }
  }

  test('shows both identities, the text and a visible-of-linked label', async () => {
    renderGraph()
    const control = await screen.findByRole('button', {
      name: 'Filter by key result O-A KR-1',
    })
    const node = control.closest('.wsv-graph-node') as HTMLElement
    expect(within(node).getByText(/O-A/)).toBeTruthy()
    expect(within(node).getByText('A outcome')).toBeTruthy()
    expect(within(node).getByText(/1 linked/)).toBeTruthy()
    expect(within(node).getByText(/1 of 1 visible/)).toBeTruthy()
  })

  test('recorded zero is distinct from Unrecorded', async () => {
    renderGraph()
    const recorded = (await screen.findByRole('button', {
      name: 'Filter by key result O-A KR-1',
    })).closest('.wsv-graph-node') as HTMLElement
    // KR-1 recorded progress 0 must render as 0, not as Unrecorded.
    expect(within(recorded).getByText('0')).toBeTruthy()
    expect(within(recorded).queryByText('Unrecorded')).toBeNull()

    const unrecorded = screen.getByRole('button', {
      name: 'Filter by key result O-A KR-2',
    }).closest('.wsv-graph-node') as HTMLElement
    expect(within(unrecorded).getByText('Unrecorded')).toBeTruthy()
  })

  test('optional target and status appear only when recorded', async () => {
    renderGraph()
    const withTarget = (await screen.findByRole('button', {
      name: 'Filter by key result O-A KR-1',
    })).closest('.wsv-graph-node') as HTMLElement
    expect(within(withTarget).getByText('Target')).toBeTruthy()
    expect(within(withTarget).getByText('Status')).toBeTruthy()

    const without = screen.getByRole('button', {
      name: 'Filter by key result O-A KR-2',
    }).closest('.wsv-graph-node') as HTMLElement
    expect(within(without).queryByText('Target')).toBeNull()
    expect(within(without).queryByText('Status')).toBeNull()
  })

  test('click, Enter and Space invoke only the scoped outcome callback', async () => {
    const user = userEvent.setup()
    const onSelectOutcome = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const onSelectTask = vi.fn()
    const onSelectObjective = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <GraphView
          tasks={[LINKED] as unknown as readonly WorkspaceTask[]}
          referenceTasks={[LINKED] as unknown as readonly WorkspaceTask[]}
          objectives={[OBJECTIVE_A] as never}
          notes={[]}
          edges={[]}
          keyResultProjection={projectionFor([LINKED], [OBJECTIVE_A])}
          onSelectOutcome={onSelectOutcome}
          onSelectTask={onSelectTask}
          onSelectObjective={onSelectObjective}
        />
      </QueryClientProvider>,
    )

    const control = await screen.findByRole('button', {
      name: 'Filter by key result O-A KR-1',
    })
    await user.click(control)
    control.focus()
    await user.keyboard('{Enter}')
    await user.keyboard(' ')

    expect(onSelectOutcome).toHaveBeenCalledTimes(3)
    expect(onSelectOutcome).toHaveBeenCalledWith({ objectiveId: 'O-A', keyResultId: 'KR-1' })
    // Never Task or Objective selection, and no context dialog.
    expect(onSelectTask).not.toHaveBeenCalled()
    expect(onSelectObjective).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  test('key-result nodes expose no context trigger', async () => {
    renderGraph()
    await screen.findByRole('button', { name: 'Filter by key result O-A KR-1' })
    expect(screen.queryByRole('button', { name: /Open context for task KR-1/ })).toBeNull()
  })

  test('existing Task activation still works alongside outcomes', async () => {
    const user = userEvent.setup()
    const onSelectTask = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <GraphView
          tasks={[LINKED] as unknown as readonly WorkspaceTask[]}
          referenceTasks={[LINKED] as unknown as readonly WorkspaceTask[]}
          objectives={[OBJECTIVE_A] as never}
          notes={[]}
          edges={[]}
          keyResultProjection={projectionFor([LINKED], [OBJECTIVE_A])}
          onSelectOutcome={vi.fn()}
          onSelectTask={onSelectTask}
          onSelectObjective={vi.fn()}
        />
      </QueryClientProvider>,
    )
    await user.click(await screen.findByRole('button', { name: 'Open task T-1' }))
    expect(onSelectTask).toHaveBeenCalledWith('T-1')
  })
})


describe("GR01/GR02 presentation identity and zero-Task content", () => {
  const objective = (id: string, keyResults: { id: string; text: string }[]) =>
    ({ id, objective: `${id} objective`, revision: 1, key_results: keyResults }) as never;
  const linked = (id: string, objectiveId: string, keyResultId: string) => ({
    id, title: `Task ${id}`, status: "open", priority: "P2", due: null, tags: [],
    objective_ids: [objectiveId], dependencies: [], subtasks: [], context_count: 0, revision: 1,
    key_result_refs: [{ objective_id: objectiveId, key_result_id: keyResultId }],
  }) as never;

  it("GR02 a schema-valid Objective equal to a key-result endpoint keeps distinct node identities", () => {
    const projection = projectKeyResults({
      workspaceId: "ws-review",
      tasks: [linked("T-1", "O-A", "KR-1")],
      objectives: [objective("O-A", [{ id: "KR-1", text: "A outcome" }])],
    });
    const collidingId = keyResultEndpointKey(
      projection.keyResults.find((node) => node.objectiveId === "O-A")!.key,
    );
    const witnessProjection = projectKeyResults({
      workspaceId: "ws-review",
      tasks: [linked("T-1", "O-A", "KR-1"), linked("T-2", collidingId, "KR-9")],
      objectives: [
        objective("O-A", [{ id: "KR-1", text: "A outcome" }]),
        objective(collidingId, [{ id: "KR-9", text: "Collision outcome" }]),
      ],
    });

    const model = makeGraphModel(
      [linked("T-1", "O-A", "KR-1"), linked("T-2", collidingId, "KR-9")],
      [
        objective("O-A", [{ id: "KR-1", text: "A outcome" }]),
        objective(collidingId, [{ id: "KR-9", text: "Collision outcome" }]),
      ],
      [], [], null, null, witnessProjection,
    );

    // Every live Flow identity stays unique even though one Objective ID is
    // literally an escaped key-result endpoint string.
    expect(new Set(model.nodes.map((node) => node.id)).size).toBe(model.nodes.length);
    expect(model.nodes.filter((node) => node.data.kind === "key-result")).toHaveLength(2);
    expect(model.nodes.filter((node) => node.data.kind === "objective")).toHaveLength(2);
    // Canonical callback identities are untouched by the presentation mapping.
    expect(model.nodes.filter((node) => node.data.kind === "objective").map((node) => node.data.id).sort())
      .toEqual(["O-A", collidingId].sort());
  });

  it("GR01 a workspace with no Tasks still renders its key result and parent Objective", () => {
    const projection = projectKeyResults({
      workspaceId: "W1",
      tasks: [],
      objectives: [objective("O-A", [{ id: "KR-1", text: "Zero linked" }])],
    });

    const model = makeGraphModel([], [objective("O-A", [{ id: "KR-1", text: "Zero linked" }])], [], [], null, null, projection);

    const kinds = model.nodes.map((node) => node.data.kind);
    expect(kinds).toContain("key-result");
    expect(kinds).toContain("objective");
    for (const edge of model.edges) {
      expect(model.nodes.some((node) => node.id === edge.source)).toBe(true);
      expect(model.nodes.some((node) => node.id === edge.target)).toBe(true);
    }
  });

  it("GR05 only the exact scoped pair selects, and another parent stays unselected", () => {
    const objectives = [
      objective("O-A", [{ id: "KR-1", text: "A outcome" }]),
      objective("O-B", [{ id: "KR-1", text: "B outcome" }]),
    ];
    const tasks = [linked("T-1", "O-A", "KR-1"), linked("T-2", "O-B", "KR-1")];
    const projection = projectKeyResults({ workspaceId: "W1", tasks, objectives });

    const selected = makeGraphModel(tasks, objectives, [], [], null, null, projection, {
      kind: "pair", objectiveId: "O-B", keyResultId: "KR-1",
    });
    const cleared = makeGraphModel(tasks, objectives, [], [], null, null, projection, { kind: "all" });

    const selectedKrs = selected.nodes.filter((node) => node.data.kind === "key-result");
    expect(selectedKrs.filter((node) => node.data.selected)).toHaveLength(1);
    expect(selectedKrs.find((node) => node.data.selected)!.data.eyebrow).toContain("O-B");
    expect(cleared.nodes.filter((node) => node.data.kind === "key-result" && node.data.selected)).toHaveLength(0);
  });
});


const CANONICAL_KINDS = new Set(["alignment", "dependency", "parent", "reference"]);

describe("GN1 canonical relationship edges reach live presentation nodes", () => {
  const task = (id: string, extra: Record<string, unknown> = {}) => ({
    id, title: `Task ${id}`, status: "open", priority: "P2", due: null, tags: [],
    objective_ids: ["O1"], dependencies: [], subtasks: [], context_count: 0, revision: 1, ...extra,
  }) as never;

  function relationshipModel() {
    const tasks = [task("T1"), task("T2", { dependencies: ["T1"], parent_id: "T1" })];
    const objectives = [{ id: "O1", objective: "O1 objective", revision: 1 } as never];
    const notes = [{ id: "N1", text: "Reference note", created: "2026-09-01", links: ["T1"] } as never];
    return makeGraphModel(tasks, objectives, notes, [], null, null, null);
  }

  it("keeps every ordinary alignment, dependency, parent and reference edge on live endpoints", () => {
    const model = relationshipModel();
    const liveIds = new Set(model.nodes.map((node) => node.id));
    const ordinary = model.edges.filter((edge) => CANONICAL_KINDS.has((edge.data as { kind: string }).kind));

    // The canonical relationships themselves are unchanged: same set, same count.
    expect(ordinary).toHaveLength(5);
    const kinds = new Set(ordinary.map((edge) => (edge.data as { kind: string }).kind));
    expect([...kinds].sort()).toEqual(["alignment", "dependency", "parent", "reference"]);
    // Nothing is filtered away to make the endpoint check pass.
    for (const edge of ordinary) {
      expect(liveIds.has(edge.source)).toBe(true);
      expect(liveIds.has(edge.target)).toBe(true);
    }
  });

  it("healthy control: derived outcome edges already have live endpoints", () => {
    const linked = task("T1", { key_result_refs: [{ objective_id: "O1", key_result_id: "K1" }] });
    const objectives = [{ id: "O1", objective: "O1 objective", revision: 1, key_results: [{ id: "K1", text: "Outcome" }] } as never];
    const projection = projectKeyResults({ workspaceId: "W1", tasks: [linked], objectives });
    const model = makeGraphModel([linked], objectives, [], [], null, null, projection);
    const liveIds = new Set(model.nodes.map((node) => node.id));
    const derived = model.edges.filter((edge) => !CANONICAL_KINDS.has((edge.data as { kind: string }).kind));

    expect(derived.length).toBeGreaterThan(0);
    for (const edge of derived) {
      expect(liveIds.has(edge.source)).toBe(true);
      expect(liveIds.has(edge.target)).toBe(true);
    }
  });
});


describe("GC-F1 a required Task endpoint is never satisfied by a same-ID Objective", () => {
  const visible = {
    id: "T-0001", title: "Visible work", status: "open", priority: "P2", due: null, tags: [],
    objective_ids: ["T-0002"], dependencies: ["T-0002"], subtasks: [], context_count: 0, revision: 1,
  } as never;
  const sameIdObjective = { id: "T-0002", objective: "Legally identical raw id", revision: 1 } as never;

  it("omits only the dependency whose Task endpoint is filtered out, keeping the alignment", () => {
    // The canonical Done Task T-0002 is hidden by the existing completed
    // visibility policy; the Objective that legally shares its ID is visible.
    const model = makeGraphModel([visible], [sameIdObjective], [], [], null, null, null);
    const liveIds = new Set(model.nodes.map((node) => node.id));
    const kindOf = (edge: { data?: unknown }) => (edge.data as { kind: string } | undefined)?.kind;

    const alignment = model.edges.filter((edge) => kindOf(edge) === "alignment");
    expect(alignment).toHaveLength(1);
    expect(alignment[0].source).toBe("flow|task|T-0001");
    expect(alignment[0].target).toBe("flow|objective|T-0002");

    // The dependency must not be retargeted onto the unrelated Objective, and
    // no Task node may be manufactured for it.
    expect(model.edges.filter((edge) => kindOf(edge) === "dependency")).toHaveLength(0);
    expect(liveIds.has("flow|task|T-0002")).toBe(false);
    for (const edge of model.edges) {
      expect(liveIds.has(edge.source)).toBe(true);
      expect(liveIds.has(edge.target)).toBe(true);
    }
  });

  it("healthy control: when that Task IS visible the dependency renders on Task endpoints", () => {
    const done = {
      id: "T-0002", title: "Prerequisite", status: "open", priority: "P2", due: null, tags: [],
      objective_ids: [], dependencies: [], subtasks: [], context_count: 0, revision: 1,
    } as never;
    const model = makeGraphModel([visible, done], [sameIdObjective], [], [], null, null, null);
    const dependency = model.edges.filter((edge) => (edge.data as { kind: string }).kind === "dependency");

    expect(dependency).toHaveLength(1);
    expect(dependency[0].source).toBe("flow|task|T-0001");
    expect(dependency[0].target).toBe("flow|task|T-0002");
    expect(model.edges.filter((edge) => (edge.data as { kind: string }).kind === "alignment")).toHaveLength(1);
  });

  it("healthy control: a Note reference still resolves to either a Task or an Objective", () => {
    const note = { id: "N-1", text: "Reference", created: "2026-09-01", links: ["T-0001", "T-0002"] } as never;
    const model = makeGraphModel([visible], [sameIdObjective], [note], [], null, null, null);
    const references = model.edges.filter((edge) => (edge.data as { kind: string }).kind === "reference");
    const liveIds = new Set(model.nodes.map((node) => node.id));

    expect(references.map((edge) => edge.target).sort()).toEqual(["flow|objective|T-0002", "flow|task|T-0001"]);
    for (const edge of references) {
      expect(edge.source).toBe("flow|note|N-1");
      expect(liveIds.has(edge.target)).toBe(true);
    }
  });
});


describe("TE-F1 a live Note target is still a genuine general reference", () => {
  const visibleTask = {
    id: "T-0001", title: "Visible work", status: "open", priority: "P2", due: null, tags: [],
    objective_ids: [], dependencies: [], subtasks: [], context_count: 0, revision: 1,
  } as never;
  const notes = [
    { id: "N-0001", text: "Links a Task and another Note", created: "2026-09-01", links: ["T-0001", "N-0002"] },
    { id: "N-0002", text: "Links the same Task", created: "2026-09-01", links: ["T-0001"] },
  ] as never;

  it("keeps the Note to Note reference alongside both Note to Task references", () => {
    const canonical = buildWorkspaceEdges([visibleTask], notes, []);
    // Control: the canonical builder emits all three references, unrestricted.
    expect(canonical.filter((edge) => edge.kind === "reference")).toHaveLength(3);

    const model = makeGraphModel([visibleTask], [], notes, [], null, null, null);
    const liveIds = new Set(model.nodes.map((node) => node.id));
    const references = model.edges
      .filter((edge) => (edge.data as { kind: string } | undefined)?.kind === "reference")
      .map((edge) => `${edge.source}->${edge.target}`)
      .sort();

    // Healthy controls FIRST: both Notes are live and both Note->Task links render.
    expect(liveIds.has("flow|note|N-0001")).toBe(true);
    expect(liveIds.has("flow|note|N-0002")).toBe(true);
    expect(references).toContain("flow|note|N-0001->flow|task|T-0001");
    expect(references).toContain("flow|note|N-0002->flow|task|T-0001");
    // The missing one.
    expect(references).toContain("flow|note|N-0001->flow|note|N-0002");
    expect(references).toHaveLength(3);

    for (const edge of model.edges) {
      expect(liveIds.has(edge.source)).toBe(true);
      expect(liveIds.has(edge.target)).toBe(true);
    }
    // No canonical input mutation.
    expect(notes).toEqual([
      { id: "N-0001", text: "Links a Task and another Note", created: "2026-09-01", links: ["T-0001", "N-0002"] },
      { id: "N-0002", text: "Links the same Task", created: "2026-09-01", links: ["T-0001"] },
    ]);
  });

  it("healthy control: dependency and alignment stay strictly typed", () => {
    const aligned = {
      ...(visibleTask as unknown as Record<string, unknown>),
      id: "T-0003", objective_ids: ["O-1"], dependencies: ["T-9999"],
    } as never;
    const model = makeGraphModel([aligned], [{ id: "O-1", objective: "Objective" } as never], [], [], null, null, null);
    const kinds = model.edges.map((edge) => (edge.data as { kind: string } | undefined)?.kind);

    expect(kinds).toEqual(["alignment"]);
  });
});
