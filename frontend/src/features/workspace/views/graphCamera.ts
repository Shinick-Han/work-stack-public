import { GRAPH_NODE_SIZES } from "./graphLayout";
import {
  clampGraphCoord,
  clampGraphZoom,
  GRAPH_ZOOM_MIN,
  type GraphViewport,
} from "./graphViewState";

export const GRAPH_NEIGHBORHOOD_PADDING = 0.16;
export const GRAPH_NEIGHBORHOOD_MIN_ZOOM = GRAPH_ZOOM_MIN;
export const GRAPH_NEIGHBORHOOD_MAX_ZOOM = 1.15;

export type GraphCameraPane = { width: number; height: number };

export type GraphCameraRect = { x: number; y: number; width: number; height: number };

export type GraphCameraNode = {
  id: string;
  position: { x: number; y: number };
  data: { id: string; kind: string };
};

export type GraphCameraEdge = {
  source: string;
  target: string;
};

export type NeighborhoodFitView = {
  nodes: { id: string }[];
  padding: number;
  minZoom: number;
  maxZoom: number;
  duration: number;
};

export function originObjectiveNode(
  nodes: readonly GraphCameraNode[],
  objectiveId: string,
) {
  return nodes.find((node) => (
    node.data.kind === "objective" && node.data.id === objectiveId
  ));
}

/**
 * Direct neighborhood: the Objective plus every currently visible node joined
 * by an incident edge. Two-hop nodes stay out.
 */
export function objectiveNeighborhoodIds(
  originId: string,
  edges: readonly GraphCameraEdge[],
) {
  const ids = new Set<string>([originId]);
  for (const edge of edges) {
    if (edge.source === originId) ids.add(edge.target);
    if (edge.target === originId) ids.add(edge.source);
  }
  return ids;
}

function nodeSizeFor(kind: string) {
  if (kind === "objective") return GRAPH_NODE_SIZES.objective;
  if (kind === "note") return GRAPH_NODE_SIZES.note;
  if (kind === "key-result") return GRAPH_NODE_SIZES["key-result"];
  return GRAPH_NODE_SIZES.task;
}

function nodeRect(node: GraphCameraNode): GraphCameraRect {
  const size = nodeSizeFor(node.data.kind);
  return {
    x: node.position.x,
    y: node.position.y,
    width: size.width,
    height: size.height,
  };
}

function includeRect(bounds: GraphCameraRect | null, rect: GraphCameraRect): GraphCameraRect {
  if (!bounds) return rect;
  const minX = Math.min(bounds.x, rect.x);
  const minY = Math.min(bounds.y, rect.y);
  const maxX = Math.max(bounds.x + bounds.width, rect.x + rect.width);
  const maxY = Math.max(bounds.y + bounds.height, rect.y + rect.height);
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

export function neighborhoodWorldBounds(
  objectiveId: string,
  nodes: readonly GraphCameraNode[],
  edges: readonly GraphCameraEdge[],
): GraphCameraRect | null {
  const origin = originObjectiveNode(nodes, objectiveId);
  if (!origin) return null;
  const ids = objectiveNeighborhoodIds(origin.id, edges);
  let bounds: GraphCameraRect | null = null;
  for (const node of nodes) {
    if (ids.has(node.id)) bounds = includeRect(bounds, nodeRect(node));
  }
  return bounds;
}

export function viewportForBounds(
  bounds: GraphCameraRect,
  pane: GraphCameraPane,
  padding = GRAPH_NEIGHBORHOOD_PADDING,
  minZoom = GRAPH_NEIGHBORHOOD_MIN_ZOOM,
  maxZoom = GRAPH_NEIGHBORHOOD_MAX_ZOOM,
): GraphViewport {
  const innerWidth = Math.max(pane.width * (1 - 2 * padding), 1);
  const innerHeight = Math.max(pane.height * (1 - 2 * padding), 1);
  const boxWidth = Math.max(bounds.width, 1);
  const boxHeight = Math.max(bounds.height, 1);
  const zoom = clampGraphZoom(Math.min(
    Math.max(Math.min(innerWidth / boxWidth, innerHeight / boxHeight), minZoom),
    maxZoom,
  ));
  const centerX = bounds.x + bounds.width / 2;
  const centerY = bounds.y + bounds.height / 2;
  return {
    x: clampGraphCoord(pane.width / 2 - centerX * zoom),
    y: clampGraphCoord(pane.height / 2 - centerY * zoom),
    zoom,
  };
}

export function projectWorldRect(
  bounds: GraphCameraRect,
  viewport: GraphViewport,
): GraphCameraRect {
  return {
    x: bounds.x * viewport.zoom + viewport.x,
    y: bounds.y * viewport.zoom + viewport.y,
    width: bounds.width * viewport.zoom,
    height: bounds.height * viewport.zoom,
  };
}

export function neighborhoodFitsPane(
  bounds: GraphCameraRect,
  viewport: GraphViewport,
  pane: GraphCameraPane,
) {
  const projected = projectWorldRect(bounds, viewport);
  return projected.x >= 0
    && projected.y >= 0
    && projected.x + projected.width <= pane.width
    && projected.y + projected.height <= pane.height;
}

export function objectiveNeighborhoodFitView(
  objectiveId: string | null | undefined,
  nodes: readonly GraphCameraNode[],
  edges: readonly GraphCameraEdge[],
): NeighborhoodFitView | null {
  if (!objectiveId) return null;
  const origin = originObjectiveNode(nodes, objectiveId);
  if (!origin) return null;
  const ids = objectiveNeighborhoodIds(origin.id, edges);
  const neighborhood = nodes
    .filter((node) => ids.has(node.id))
    .map((node) => ({ id: node.id }))
    .sort((left, right) => left.id.localeCompare(right.id));
  if (!neighborhood.length) return null;
  return {
    nodes: neighborhood,
    padding: GRAPH_NEIGHBORHOOD_PADDING,
    minZoom: GRAPH_NEIGHBORHOOD_MIN_ZOOM,
    maxZoom: GRAPH_NEIGHBORHOOD_MAX_ZOOM,
    duration: 0,
  };
}

export function neighborhoodLayoutReady(
  settledTopologyKey: string | null | undefined,
  requestedTopologyKey: string | null | undefined,
) {
  return Boolean(
    settledTopologyKey
    && requestedTopologyKey
    && settledTopologyKey === requestedTopologyKey
  );
}
