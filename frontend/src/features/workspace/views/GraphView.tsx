import { GraphSurface } from "./GraphSurface";
import { GraphNodeFrame, miniMapNodeClassName, miniMapNodeColor } from "./graphPresentation";
import { makeGraphModel, shouldVirtualizeGraph } from "./graphViewModel";
import { roundedOrthogonalPath } from "./graphPath";
import { useGraphCanvas } from "./useGraphCanvas";
import type { GraphViewProps } from "./graphViewTypes";
import "./GraphContextPopover.css";
import "./GraphView.drag.css";
import "@xyflow/react/dist/style.css";

export type {
  DeferredKeyResultDetailCoordinator,
  GraphNodeData,
  GraphViewProps,
} from "./graphViewTypes";
export { GraphNodeFrame, miniMapNodeClassName, miniMapNodeColor };
export { makeGraphModel, shouldVirtualizeGraph };
export { roundedOrthogonalPath };

export function GraphView(props: GraphViewProps) {
  const surface = useGraphCanvas(props);
  return <GraphSurface {...surface} />;
}
