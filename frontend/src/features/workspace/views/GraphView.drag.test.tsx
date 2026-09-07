import React from "react"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { GraphView } from "./GraphView"
import { LAYOUT_SAVE_FAILURE_MESSAGE, localViewStorageKey } from "./localViewState"
import { GRAPH_DRAG_THRESHOLD_PX } from "./graphViewState"
import { task } from "../../../test/fixtures"
import type { WorkspaceObjective, WorkspaceTask } from "./types"

const { layoutPlanningGraphMock } = vi.hoisted(() => ({
  layoutPlanningGraphMock: vi.fn(),
}))

vi.mock("./graphLayout", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./graphLayout")>()
  return {
    ...actual,
    layoutPlanningGraph: layoutPlanningGraphMock,
  }
})

type FlowNode = {
  id: string
  position: { x: number; y: number }
  data: {
    id: string
    kind: string
    title: string
    eyebrow: string
    contextCount: number
    selected: boolean
    related: boolean
    moveHandleLabel?: string
    onActivate?: () => void
    onMoveKeyDown?: (event: React.KeyboardEvent<HTMLButtonElement>) => void
  }
}

type FlowEdge = { id: string; source: string; target: string; data?: { route?: unknown } }

type FlowProps = {
  nodes: FlowNode[]
  edges: FlowEdge[]
  nodeTypes: { workspace: React.ComponentType<{ data: FlowNode["data"] }> }
  nodeDragThreshold?: number
  nodesDraggable?: boolean
  onInit?: (instance: { fitView: ReturnType<typeof vi.fn>; setViewport: ReturnType<typeof vi.fn> }) => void
  onNodeDragStart?: (event: unknown, node: FlowNode) => void
  onNodeDrag?: (event: unknown, node: FlowNode) => void
  onNodeDragStop?: (event: unknown, node: FlowNode) => void
  onMoveEnd?: (event: unknown, viewport: { x: number; y: number; zoom: number }) => void
}

type FitSnapshot = {
  options: { nodes?: { id: string }[] } | undefined
  nodes: { id: string; x: number; y: number }[]
}

let lastFlow: FlowProps | null = null
let fitSnapshots: FitSnapshot[] = []
const fitView = vi.fn((options?: FitSnapshot["options"]) => {
  fitSnapshots.push({
    options,
    nodes: (lastFlow?.nodes ?? []).map((node) => ({
      id: node.id,
      x: node.position.x,
      y: node.position.y,
    })),
  })
})
const setViewport = vi.fn()

vi.mock("@xyflow/react", async (importOriginal) => ({
  ...await importOriginal<typeof import("@xyflow/react")>(),
  ReactFlow: (props: FlowProps) => {
    lastFlow = props
    const Node = props.nodeTypes.workspace
    React.useLayoutEffect(() => {
      props.onInit?.({ fitView, setViewport })
    }, [props])
    return (
      <div data-testid="graph-flow">
        {(props.nodes ?? []).map((node) => (
          <div
            key={node.id}
            data-node-id={node.id}
            data-x={node.position.x}
            data-y={node.position.y}
          >
            <Node data={node.data} />
          </div>
        ))}
      </div>
    )
  },
  Handle: () => null,
}))

const WORKSPACE = "ws-graph-drag"
const KEY = localViewStorageKey(WORKSPACE, "graph")

function envelope(data: unknown, revision = 1) {
  return {
    schemaVersion: 1,
    workspaceId: WORKSPACE,
    view: "graph",
    revision,
    writtenAt: 10,
    writerId: "peer",
    data,
  }
}

function tasks(...ids: string[]): WorkspaceTask[] {
  return ids.map((id, index) => ({
    ...task,
    id,
    uid: `uid-${id}`,
    title: `Task ${id}`,
    objective_ids: index === 0 ? ["O-1", "O-2"] : ["O-1"],
  }))
}

const objectives: WorkspaceObjective[] = [
  { id: "O-1", objective: "First" },
  { id: "O-2", objective: "Second" },
]

function clearFitTracking() {
  fitView.mockClear()
  fitSnapshots = []
}

function expectOneFitSnapshot(): FitSnapshot {
  expect(fitSnapshots).toHaveLength(1)
  const snapshot = fitSnapshots[0]
  if (!snapshot) throw new Error("expected one fitView snapshot")
  return snapshot
}

function currentNodeSet() {
  return (lastFlow?.nodes ?? []).map((node) => ({
    id: node.id,
    x: node.position.x,
    y: node.position.y,
  }))
}

beforeEach(() => {
  window.localStorage.clear()
  lastFlow = null
  clearFitTracking()
  setViewport.mockReset()
  layoutPlanningGraphMock.mockReset()
  layoutPlanningGraphMock.mockImplementation(async (nodes: FlowNode[], edges: FlowEdge[]) => ({
    nodes: nodes.map((node, index) => ({ ...node, position: { x: 40 + index * 10, y: 80 } })),
    edgeRoutes: Object.fromEntries(edges.map((edge) => [edge.id, {
      points: [{ x: 0, y: 0 }, { x: 8, y: 8 }],
      label: { x: 4, y: 4 },
    }])),
  }))
})

async function settleLayout() {
  await act(async () => {
    await Promise.all(layoutPlanningGraphMock.mock.results.map((result) => result.value))
  })
}

async function renderGraph(options: {
  workspaceId?: string
  visible?: WorkspaceTask[]
  reference?: WorkspaceTask[]
  onSelectTask?: ReturnType<typeof vi.fn>
  onSelectObjective?: ReturnType<typeof vi.fn>
  onSelectOutcome?: ReturnType<typeof vi.fn>
} = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const all = options.reference ?? tasks("T-0001", "T-0002")
  const props = {
    workspaceId: options.workspaceId ?? WORKSPACE,
    tasks: options.visible ?? all,
    referenceTasks: all,
    objectives,
    notes: [],
    edges: [],
    onSelectTask: options.onSelectTask ?? vi.fn(),
    onSelectObjective: options.onSelectObjective ?? vi.fn(),
    onSelectOutcome: options.onSelectOutcome ?? vi.fn(),
  }
  const view = render(
    <QueryClientProvider client={client}>
      <GraphView {...props} />
    </QueryClientProvider>,
  )
  await settleLayout()
  return { ...view, props }
}

function taskNode(id = "T-0001") {
  const node = lastFlow?.nodes.find((item) => item.data.id === id && item.data.kind === "task")
  expect(node).toBeTruthy()
  return node as FlowNode
}

describe("Graph local drag and persistence", () => {
  it("P1 keeps below-threshold handle interaction as focus only and writes once after an activated drop", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch")
    const view = await renderGraph()
    expect(lastFlow?.nodeDragThreshold).toBe(GRAPH_DRAG_THRESHOLD_PX)
    expect(lastFlow?.nodesDraggable).toBe(true)
    expect(lastFlow?.nodes.every((node) => node.id)).toBe(true)

    const handle = screen.getByRole("button", { name: "Move task T-0001" })
    handle.focus()
    expect(handle).toHaveFocus()
    await userEvent.click(handle)
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(window.localStorage.getItem(KEY)).toBeNull()

    const node = taskNode()
    act(() => {
      lastFlow?.onNodeDragStart?.({}, node)
      lastFlow?.onNodeDragStop?.({}, { ...node, position: { x: 120, y: 160 } })
    })
    await waitFor(() => expect(window.localStorage.getItem(KEY)).toBeTruthy())
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string)
    expect(stored.revision).toBe(1)
    expect(stored.data.positions).toEqual([
      expect.objectContaining({ id: node.id, x: 120, y: 160 }),
    ])
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(view.props.onSelectObjective).not.toHaveBeenCalled()
    expect(view.props.onSelectOutcome).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })

  it("P2 restores the snapshot and writes nothing when Escape cancels an active drag", async () => {
    await renderGraph()
    const node = taskNode()
    act(() => lastFlow?.onNodeDragStart?.({}, node))
    expect(screen.getByText(/Picked up task T-0001/)).toBeInTheDocument()
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))
    })
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(screen.getByText("Move cancelled")).toBeInTheDocument()
  })

  it("K1 moves from the handle with arrows, keeps focus, and announces commit", async () => {
    const view = await renderGraph()
    const handle = screen.getByRole("button", { name: "Move task T-0001" })
    const node = taskNode()
    const origin = node.position
    handle.focus()
    await userEvent.keyboard("{Enter}{ArrowRight}{Shift>}{ArrowDown}{/Shift}{Enter}")
    expect(handle).toHaveFocus()
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string)
    expect(stored.data.positions[0]).toMatchObject({
      id: node.id,
      x: origin.x + 16,
      y: origin.y + 64,
    })
    expect(screen.getByText(/Moved task T-0001/)).toBeInTheDocument()
  })

  it("G1 restores coordinates and viewport per workspace and keeps ELK for untouched nodes", async () => {
    const nodeId = "flow|task|T-0001"
    window.localStorage.setItem(KEY, JSON.stringify(envelope({
      positions: [{ id: nodeId, x: 500, y: 600, touchedAt: 9 }],
      viewport: { x: 11, y: 22, zoom: 0.8 },
    })))
    await renderGraph()
    expect(Number(screen.getByTestId("graph-flow").querySelector('[data-node-id="flow|task|T-0001"]')?.getAttribute("data-x"))).toBe(500)
    expect(Number(screen.getByTestId("graph-flow").querySelector('[data-node-id="flow|task|T-0002"]')?.getAttribute("data-x"))).not.toBe(500)
    expect(setViewport).toHaveBeenCalledWith({ x: 11, y: 22, zoom: 0.8 })
    expect(fitView).not.toHaveBeenCalled()
  })

  it("G2 moves one multi-objective Task node and drops only incident ELK routes", async () => {
    await renderGraph()
    const moved = taskNode("T-0001")
    const other = taskNode("T-0002")
    const before = lastFlow?.edges.find((edge) => edge.source === other.id || edge.target === other.id)
    expect(before?.data?.route).toBeTruthy()
    act(() => {
      lastFlow?.onNodeDragStart?.({}, moved)
      lastFlow?.onNodeDragStop?.({}, { ...moved, position: { x: 90, y: 90 } })
    })
    const incident = lastFlow?.edges.filter((edge) => edge.source === moved.id || edge.target === moved.id) ?? []
    expect(incident.length).toBeGreaterThan(0)
    expect(incident.every((edge) => edge.data?.route === undefined)).toBe(true)
    const untouched = lastFlow?.edges.filter((edge) => edge.source !== moved.id && edge.target !== moved.id) ?? []
    expect(untouched.every((edge) => edge.data?.route)).toBe(true)
  })

  it("C1 buffers an external write during a gesture and rebases distinct nodes on commit", async () => {
    await renderGraph()
    const moved = taskNode("T-0001")
    const otherId = taskNode("T-0002").id
    act(() => lastFlow?.onNodeDragStart?.({}, moved))
    const foreign = envelope({
      positions: [{ id: otherId, x: 15, y: 25, touchedAt: 4 }],
      viewport: null,
    }, 4)
    window.localStorage.setItem(KEY, JSON.stringify(foreign))
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", {
        key: KEY,
        newValue: JSON.stringify(foreign),
        storageArea: window.localStorage,
      }))
    })
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${moved.id}"]`)?.getAttribute("data-x"))).not.toBe(15)
    act(() => lastFlow?.onNodeDragStop?.({}, { ...moved, position: { x: 300, y: 301 } }))
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string)
    const ids = stored.data.positions.map((item: { id: string }) => item.id).sort()
    expect(ids).toEqual([moved.id, otherId].sort())
    expect(stored.data.positions.find((item: { id: string; x: number }) => item.id === moved.id).x).toBe(300)
    expect(stored.data.positions.find((item: { id: string; x: number }) => item.id === otherId).x).toBe(15)
  })

  it("R1 reset clears this workspace graph key, fits when nodes exist, and leaves other views alone", async () => {
    const tableKey = localViewStorageKey(WORKSPACE, "table")
    window.localStorage.setItem(KEY, JSON.stringify(envelope({
      positions: [{ id: "flow|task|T-0001", x: 1, y: 2, touchedAt: 1 }],
      viewport: { x: 0, y: 0, zoom: 1 },
    })))
    window.localStorage.setItem(tableKey, JSON.stringify({ keep: true }))
    const fetchSpy = vi.spyOn(globalThis, "fetch")
    const view = await renderGraph()
    expect(Number(screen.getByTestId("graph-flow").querySelector('[data-node-id="flow|task|T-0001"]')?.getAttribute("data-x"))).toBe(1)
    clearFitTracking()
    await userEvent.click(screen.getByRole("button", { name: "Reset graph layout" }))
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(window.localStorage.getItem(tableKey)).toBe('{"keep":true}')
    expect(screen.getByText("Graph layout reset")).toBeInTheDocument()
    const snapshot = expectOneFitSnapshot()
    expect(snapshot.options?.nodes).toBeUndefined()
    expect(snapshot.nodes).toEqual(currentNodeSet())
    expect(snapshot.nodes.find((node) => node.id === "flow|task|T-0001")).not.toEqual({
      id: "flow|task|T-0001",
      x: 1,
      y: 2,
    })
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })

  it("keeps a saved viewport through the empty overlay and isolates workspaces", async () => {
    window.localStorage.setItem(KEY, JSON.stringify(envelope({
      positions: [{ id: "flow|task|T-0001", x: 70, y: 80, touchedAt: 1 }],
      viewport: { x: 3, y: 4, zoom: 1.2 },
    })))
    const all = tasks("T-0001")
    const view = await renderGraph({ visible: all, reference: all })
    view.rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
        <GraphView
          workspaceId={WORKSPACE}
          tasks={[]}
          referenceTasks={all}
          objectives={objectives}
          notes={[]}
          edges={[]}
          emptyKind="all-complete"
          onSelectTask={view.props.onSelectTask}
          onSelectObjective={view.props.onSelectObjective}
        />
      </QueryClientProvider>,
    )
    await settleLayout()
    expect(screen.getByText("All matching tasks are completed")).toBeInTheDocument()
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).data.viewport).toEqual({
      x: 3, y: 4, zoom: 1.2,
    })

    view.rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
        <GraphView
          workspaceId="ws-other"
          tasks={all}
          referenceTasks={all}
          objectives={objectives}
          notes={[]}
          edges={[]}
          onSelectTask={view.props.onSelectTask}
          onSelectObjective={view.props.onSelectObjective}
        />
      </QueryClientProvider>,
    )
    await settleLayout()
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).workspaceId).toBe(WORKSPACE)
    expect(window.localStorage.getItem(localViewStorageKey("ws-other", "graph"))).toBeNull()
  })

  it("cancels an active same-ID gesture when the workspace identity changes", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch")
    const all = tasks("T-0001")
    const view = await renderGraph({ visible: all, reference: all })
    const moved = taskNode("T-0001")
    act(() => lastFlow?.onNodeDragStart?.({}, moved))
    expect(screen.getByText(/Picked up task T-0001/)).toBeInTheDocument()

    view.rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
        <GraphView
          workspaceId="ws-other"
          tasks={all}
          referenceTasks={all}
          objectives={objectives}
          notes={[]}
          edges={[]}
          onSelectTask={view.props.onSelectTask}
          onSelectObjective={view.props.onSelectObjective}
        />
      </QueryClientProvider>,
    )
    await settleLayout()
    const sameId = taskNode("T-0001")
    act(() => {
      lastFlow?.onNodeDragStop?.({}, { ...sameId, position: { x: 999, y: 999 } })
    })
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(window.localStorage.getItem(localViewStorageKey("ws-other", "graph"))).toBeNull()
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })

  it("ignores Ctrl/Alt/Meta arrows and only nudges unmodified or Shift arrows", async () => {
    const view = await renderGraph()
    const handle = screen.getByRole("button", { name: "Move task T-0001" })
    const origin = taskNode().position
    handle.focus()
    await userEvent.keyboard("{Enter}")
    fireEvent.keyDown(handle, { key: "ArrowRight", ctrlKey: true })
    fireEvent.keyDown(handle, { key: "ArrowRight", altKey: true })
    fireEvent.keyDown(handle, { key: "ArrowRight", metaKey: true })
    fireEvent.keyDown(handle, { key: "ArrowDown", ctrlKey: true, shiftKey: true })
    expect(taskNode().position).toEqual(origin)
    expect(window.localStorage.getItem(KEY)).toBeNull()
    fireEvent.keyDown(handle, { key: "ArrowRight" })
    expect(taskNode().position).toEqual({ x: origin.x + 16, y: origin.y })
    fireEvent.keyDown(handle, { key: "ArrowDown", shiftKey: true })
    expect(taskNode().position).toEqual({ x: origin.x + 16, y: origin.y + 64 })
    await userEvent.keyboard("{Enter}")
    const stored = JSON.parse(window.localStorage.getItem(KEY) as string)
    expect(stored.data.positions[0]).toMatchObject({
      id: taskNode().id,
      x: origin.x + 16,
      y: origin.y + 64,
    })
    expect(view.props.onSelectTask).not.toHaveBeenCalled()
  })

  it("announces once when stored graph state is corrupt", async () => {
    window.localStorage.setItem(KEY, "{not json")
    await renderGraph()
    expect(await screen.findByText(LAYOUT_SAVE_FAILURE_MESSAGE)).toBeInTheDocument()
    expect(window.localStorage.getItem(KEY)).toBeNull()
  })
})
