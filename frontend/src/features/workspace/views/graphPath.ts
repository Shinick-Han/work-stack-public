import type { GraphRoutePoint } from "./graphLayout";

function offsetToward(from: GraphRoutePoint, to: GraphRoutePoint, distance: number) {
  const length = Math.abs(to.x - from.x) + Math.abs(to.y - from.y);
  if (!length) return from;
  const ratio = Math.min(1, distance / length);
  return {
    x: from.x + (to.x - from.x) * ratio,
    y: from.y + (to.y - from.y) * ratio,
  };
}

export function roundedOrthogonalPath(points: readonly GraphRoutePoint[], radius = 10) {
  if (!points.length) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  let path = `M ${points[0].x} ${points[0].y}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1];
    const corner = points[index];
    const next = points[index + 1];
    const incoming = Math.abs(corner.x - previous.x) + Math.abs(corner.y - previous.y);
    const outgoing = Math.abs(next.x - corner.x) + Math.abs(next.y - corner.y);
    const bendRadius = Math.min(radius, incoming / 2, outgoing / 2);
    const before = offsetToward(corner, previous, bendRadius);
    const after = offsetToward(corner, next, bendRadius);
    path += ` L ${before.x} ${before.y} Q ${corner.x} ${corner.y} ${after.x} ${after.y}`;
  }
  const last = points[points.length - 1];
  return `${path} L ${last.x} ${last.y}`;
}
