import React from "react"
import { act, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { GraphView } from "./GraphView"
import {
  GRAPH_NEIGHBORHOOD_MAX_ZOOM,
  GRAPH_NEIGHBORHOOD_MIN_ZOOM,
  GRAPH_NEIGHBORHOOD_PADDING,
  objectiveNeighborhoodFitView,
} from "./graphCamera"
import { localViewStorageKey } from "./localViewState"
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
  data: { id: string; kind: string; selected: boolean; related: boolean }
}

type FlowEdge = { id: string; source: string; target: string; data?: { route?: unknown } }

type FlowProps = {
  nodes: FlowNode[]
  edges: FlowEdge[]
  nodeTypes: { workspace: React.ComponentType<{ data: FlowNode["data"] }> }
  onInit?: (instance: { fitView: ReturnType<typeof vi.fn>; setViewport: ReturnType<typeof vi.fn> }) => void
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
          <div key={node.id} data-node-id={node.id} data-x={node.position.x} data-y={node.position.y}>
            <Node data={node.data} />
          </div>
        ))}
      </div>
    )
  },
  Handle: () => null,
}))

const WORKSPACE = "ws-graph-camera"
const KEY = localViewStorageKey(WORKSPACE, "graph")

function tasks(...ids: string[]): WorkspaceTask[] {
  return ids.map((id, index) => ({
    ...task,
    id,
    uid: `uid-${id}`,
    title: `Task ${id}`,
    objective_ids: index === 0 ? ["O-1"] : ["O-2"],
  }))
}

const objectives: WorkspaceObjective[] = [
  { id: "O-1", objective: "First" },
  { id: "O-2", objective: "Second" },
]

async function settleLayout() {
  await act(async () => {
    await Promise.all(layoutPlanningGraphMock.mock.results.map((result) => result.value))
  })
}

async function renderGraph(extra: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const all = extra.tasks as WorkspaceTask[] | undefined ?? tasks("T-0001", "T-0002")
  const props = {
    workspaceId: WORKSPACE,
    tasks: all,
    referenceTasks: all,
    objectives,
    notes: [],
    edges: [],
    onSelectTask: vi.fn(),
    onSelectObjective: vi.fn(),
    ...extra,
  }
  const view = render(
    <QueryClientProvider client={client}>
      <GraphView {...props} />
    </QueryClientProvider>,
  )
  await settleLayout()
  const rerenderWith = async (next: Record<string, unknown>) => {
    view.rerender(
      <QueryClientProvider client={client}>
        <GraphView {...props} {...next} />
      </QueryClientProvider>,
    )
    await settleLayout()
  }
  return { ...view, props, rerenderWith }
}

function expectedFit(objectiveId: string) {
  return objectiveNeighborhoodFitView(objectiveId, lastFlow?.nodes ?? [], lastFlow?.edges ?? [])
}

function neighborhoodFits() {
  return fitSnapshots
    .map((snapshot) => snapshot.options)
    .filter((options): options is NonNullable<FitSnapshot["options"]> & { nodes: { id: string }[] } =>
      Array.isArray(options?.nodes),
    )
}

function allNodeFits() {
  return fitSnapshots.filter((snapshot) => !snapshot.options?.nodes)
}

function nodePositions() {
  return (lastFlow?.nodes ?? []).map((node) => [node.id, node.position.x, node.position.y])
}

function currentNodeSet() {
  return (lastFlow?.nodes ?? []).map((node) => ({
    id: node.id,
    x: node.position.x,
    y: node.position.y,
  }))
}

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

function expectOneNeighborhoodFit() {
  const fits = neighborhoodFits()
  expect(fits).toHaveLength(1)
  const fit = fits[0]
  if (!fit) throw new Error("expected one neighborhood fit")
  return fit
}

beforeEach(() => {
  window.localStorage.clear()
  lastFlow = null
  clearFitTracking()
  setViewport.mockReset()
  layoutPlanningGraphMock.mockReset()
  layoutPlanningGraphMock.mockImplementation(async (nodes: FlowNode[], edges: FlowEdge[]) => ({
    nodes: nodes.map((node, index) => ({ ...node, position: { x: 40 + index * 120, y: 80 + index * 30 } })),
    edgeRoutes: Object.fromEntries(edges.map((edge) => [edge.id, {
      points: [{ x: 0, y: 0 }, { x: 8, y: 8 }],
      label: { x: 4, y: 4 },
    }])),
  }))
})

describe("Graph Objective neighborhood camera", () => {
  it("fits the Objective neighborhood through fitView nodes, not an all-nodes jump", async () => {
    await renderGraph({ selectedObjectiveId: "O-1" })
    const options = expectedFit("O-1")
    expect(options).not.toBeNull()
    expect(neighborhoodFits()).toEqual([options])
    expect(allNodeFits()).toHaveLength(0)
    expect(setViewport).not.toHaveBeenCalled()
    expect(lastFlow?.nodes.find((node) => node.data.id === "T-0001")?.data.related).toBe(true)
    expect(lastFlow?.nodes.find((node) => node.data.id === "T-0002")?.data.related).toBe(false)
  })

  it("keeps the same fitView nodes option on re-select and does not jump for Task selection", async () => {
    const view = await renderGraph()
    const layoutCalls = layoutPlanningGraphMock.mock.calls.length
    expect(allNodeFits().length).toBeGreaterThan(0)
    clearFitTracking()
    setViewport.mockClear()

    await view.rerenderWith({ selectedTaskId: "T-0001" })
    expect(fitView).not.toHaveBeenCalled()
    expect(setViewport).not.toHaveBeenCalled()
    expect(layoutPlanningGraphMock.mock.calls.length).toBe(layoutCalls)
    const afterTask = nodePositions()

    await view.rerenderWith({ selectedObjectiveId: "O-1" })
    const first = expectedFit("O-1")
    expect(neighborhoodFits()).toEqual([first])
    expect(allNodeFits()).toHaveLength(0)
    expect(nodePositions()).toEqual(afterTask)

    clearFitTracking()
    await view.rerenderWith({ selectedObjectiveId: "O-1" })
    expect(fitView).not.toHaveBeenCalled()

    await view.rerenderWith({ selectedObjectiveId: null })
    expect(fitView).not.toHaveBeenCalled()
    expect(setViewport).not.toHaveBeenCalled()

    await view.rerenderWith({ selectedObjectiveId: "O-1" })
    expect(neighborhoodFits()).toEqual([first])
    expect(nodePositions()).toEqual(afterTask)
    expect(layoutPlanningGraphMock.mock.calls.length).toBe(layoutCalls)
  })

  it("uses a distinct neighborhood per Objective and keeps saved manual coordinates", async () => {
    const taskId = "flow|task|T-0001"
    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writtenAt: 10,
      writerId: "peer",
      data: {
        positions: [{ id: taskId, x: 500, y: 600, touchedAt: 9 }],
        viewport: { x: 11, y: 22, zoom: 0.8 },
      },
    }))
    const view = await renderGraph({ selectedObjectiveId: "O-1" })
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${taskId}"]`)?.getAttribute("data-x"))).toBe(500)
    expect(setViewport).not.toHaveBeenCalled()
    expect(expectOneNeighborhoodFit()).toEqual(expectedFit("O-1"))
    expect(expectOneNeighborhoodFit()).toMatchObject({
      padding: GRAPH_NEIGHBORHOOD_PADDING,
      minZoom: GRAPH_NEIGHBORHOOD_MIN_ZOOM,
      maxZoom: GRAPH_NEIGHBORHOOD_MAX_ZOOM,
      duration: 0,
    })

    clearFitTracking()
    await view.rerenderWith({ selectedObjectiveId: "O-2" })
    expect(neighborhoodFits()).toEqual([expectedFit("O-2")])
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${taskId}"]`)?.getAttribute("data-x"))).toBe(500)
  })

  it("fits once after the filtered topology layout resolves, not against stale positions", async () => {
    const all = tasks("T-0001", "T-0002")
    const view = await renderGraph({ tasks: all, referenceTasks: all })
    clearFitTracking()
    const pending: Array<() => void> = []
    layoutPlanningGraphMock.mockImplementation(async (nodes: FlowNode[], edges: FlowEdge[]) => (
      new Promise((resolve) => {
        pending.push(() => resolve({
          nodes: nodes.map((node, index) => ({ ...node, position: { x: 900 + index * 80, y: 700 } })),
          edgeRoutes: Object.fromEntries(edges.map((edge) => [edge.id, {
            points: [{ x: 0, y: 0 }, { x: 8, y: 8 }],
            label: { x: 4, y: 4 },
          }])),
        }))
      })
    ))
    const visible = tasks("T-0001")
    await act(async () => {
      view.rerender(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
          <GraphView
            workspaceId={WORKSPACE}
            tasks={visible}
            referenceTasks={all}
            objectives={objectives}
            notes={[]}
            edges={[]}
            selectedObjectiveId="O-1"
            onSelectTask={view.props.onSelectTask}
            onSelectObjective={view.props.onSelectObjective}
          />
        </QueryClientProvider>,
      )
    })
    expect(neighborhoodFits()).toEqual([])
    expect(pending.length).toBeGreaterThan(0)
    await act(async () => {
      pending.forEach((flush) => flush())
      await Promise.all(layoutPlanningGraphMock.mock.results.map((result) => result.value))
    })
    const neighborhoodFit = expectOneNeighborhoodFit()
    const ids = neighborhoodFit.nodes.map((item) => item.id)
    expect(ids).toEqual(expectedFit("O-1")?.nodes.map((item) => item.id))
    expect(ids).toContain("flow|objective|O-1")
    expect(ids).toContain("flow|task|T-0001")
    expect(ids).not.toContain("flow|task|T-0002")
    expect(ids).not.toContain("flow|objective|O-2")
    expect(neighborhoodFit).toMatchObject({
      minZoom: GRAPH_NEIGHBORHOOD_MIN_ZOOM,
      maxZoom: GRAPH_NEIGHBORHOOD_MAX_ZOOM,
      duration: 0,
    })
    const laid = lastFlow?.nodes.find((node) => node.data.id === "T-0001")
    expect(laid?.position.y).toBe(700)
    expect(laid?.position.x).toBeGreaterThanOrEqual(900)
  })

  it("refits once after reset restores ELK coordinates of a far manual neighbor", async () => {
    const taskId = "flow|task|T-0001"
    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writtenAt: 10,
      writerId: "peer",
      data: {
        positions: [{ id: taskId, x: 8000, y: 6000, touchedAt: 9 }],
        viewport: null,
      },
    }))
    await renderGraph({ selectedObjectiveId: "O-1" })
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${taskId}"]`)?.getAttribute("data-x"))).toBe(8000)
    clearFitTracking()
    await userEvent.click(screen.getByRole("button", { name: "Reset graph layout" }))
    await settleLayout()
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${taskId}"]`)?.getAttribute("data-x"))).not.toBe(8000)
    const snapshot = expectOneFitSnapshot()
    expect(snapshot.nodes).toEqual(currentNodeSet())
    expect(snapshot.nodes.find((node) => node.id === taskId)).not.toEqual({
      id: taskId,
      x: 8000,
      y: 6000,
    })
    expect(snapshot.options?.nodes?.map((node) => node.id)).toEqual(
      expectedFit("O-1")?.nodes.map((node) => node.id),
    )
    expect(expectOneNeighborhoodFit()).toEqual(expectedFit("O-1"))
    expect(allNodeFits()).toHaveLength(0)
  })

  it("defers all-graph reset fit until nodes show the current ELK generation", async () => {
    const taskId = "flow|task|T-0001"
    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writtenAt: 10,
      writerId: "peer",
      data: {
        positions: [{ id: taskId, x: 8000, y: 6000, touchedAt: 9 }],
        viewport: { x: 0, y: 0, zoom: 1 },
      },
    }))
    await renderGraph()
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${taskId}"]`)?.getAttribute("data-x"))).toBe(8000)
    clearFitTracking()
    await userEvent.click(screen.getByRole("button", { name: "Reset graph layout" }))
    await settleLayout()
    expect(Number(screen.getByTestId("graph-flow").querySelector(`[data-node-id="${taskId}"]`)?.getAttribute("data-x"))).not.toBe(8000)
    const snapshot = expectOneFitSnapshot()
    expect(snapshot.options?.nodes).toBeUndefined()
    expect(snapshot.nodes).toEqual(currentNodeSet())
    expect(snapshot.nodes.find((node) => node.id === taskId)).not.toEqual({
      id: taskId,
      x: 8000,
      y: 6000,
    })
    expect(allNodeFits()).toHaveLength(1)
    expect(neighborhoodFits()).toHaveLength(0)
  })

  it("defers unselected reset fit until the replacement layout generation is current", async () => {
    const all = tasks("T-0001", "T-0002")
    const view = await renderGraph({ tasks: all, referenceTasks: all })
    const pending: Array<() => void> = []
    layoutPlanningGraphMock.mockImplementation(async (nodes: FlowNode[], edges: FlowEdge[]) => (
      new Promise((resolve) => {
        pending.push(() => resolve({
          nodes: nodes.map((node, index) => ({ ...node, position: { x: 900 + index * 80, y: 700 } })),
          edgeRoutes: Object.fromEntries(edges.map((edge) => [edge.id, {
            points: [{ x: 0, y: 0 }, { x: 8, y: 8 }],
            label: { x: 4, y: 4 },
          }])),
        }))
      })
    ))
    const visible = tasks("T-0001")
    await act(async () => {
      view.rerender(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
          <GraphView
            workspaceId={WORKSPACE}
            tasks={visible}
            referenceTasks={all}
            objectives={objectives}
            notes={[]}
            edges={[]}
            onSelectTask={view.props.onSelectTask}
            onSelectObjective={view.props.onSelectObjective}
          />
        </QueryClientProvider>,
      )
    })
    expect(pending.length).toBeGreaterThan(0)
    clearFitTracking()
    await userEvent.click(screen.getByRole("button", { name: "Reset graph layout" }))
    expect(fitSnapshots).toHaveLength(0)
    await act(async () => {
      pending.forEach((flush) => flush())
      await Promise.all(layoutPlanningGraphMock.mock.results.map((result) => result.value))
    })
    const snapshot = expectOneFitSnapshot()
    expect(snapshot.options?.nodes).toBeUndefined()
    expect(snapshot.nodes).toEqual(currentNodeSet())
    expect(snapshot.nodes.find((node) => node.id === "flow|task|T-0001")).toMatchObject({ y: 700 })
    expect(snapshot.nodes.find((node) => node.id === "flow|task|T-0001")?.x).toBeGreaterThanOrEqual(900)
    expect(snapshot.nodes.some((node) => node.id === "flow|task|T-0002")).toBe(false)
    expect(allNodeFits()).toHaveLength(1)
    expect(neighborhoodFits()).toHaveLength(0)
  })

  it("fits the neighborhood once when the graph store switches workspace", async () => {
    const view = await renderGraph({ workspaceId: "ws-graph-a", selectedObjectiveId: "O-1" })
    clearFitTracking()
    setViewport.mockClear()
    await view.rerenderWith({ workspaceId: "ws-graph-b", selectedObjectiveId: "O-1" })
    expectOneNeighborhoodFit()
    expect(allNodeFits()).toHaveLength(0)
    expect(setViewport).not.toHaveBeenCalled()
  })
})
