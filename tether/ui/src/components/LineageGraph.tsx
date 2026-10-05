import { useMemo } from "react";
import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import type { Lineage, LineageNode, NodeType } from "../types";

export const TYPE_META: Record<NodeType, { label: string; color: string; column: number }> = {
  subject: { label: "Subject", color: "#7c3aed", column: 0 },
  source_record: { label: "Source record", color: "#2563eb", column: 1 },
  chunk: { label: "Chunk", color: "#0891b2", column: 2 },
  memory: { label: "Memory", color: "#db2777", column: 2 },
  snapshot: { label: "Dataset snapshot", color: "#ca8a04", column: 2 },
  vector: { label: "Vector", color: "#059669", column: 3 },
  model: { label: "Model version", color: "#dc2626", column: 3 },
  collection: { label: "Collection", color: "#4b5563", column: 4 },
};

// Order of types stacked within one column.
const STACK_ORDER: NodeType[] = [
  "subject", "source_record", "chunk", "memory", "snapshot", "vector", "model", "collection",
];

const COL_WIDTH = 270;
const ROW_HEIGHT = 46;
const GROUP_GAP = 40;

const EDGE_COLORS: Record<string, string> = {
  derived_from: "#94a3b8",
  stored_in: "#64748b",
  contained_in: "#ca8a04",
  trained_on: "#dc2626",
};

function shortLabel(n: LineageNode): string {
  switch (n.type) {
    case "source_record":
    case "chunk":
      return n.ref.replace(/^[^/]+\//, "");
    case "vector":
      return n.ref.replace(/^[^/]+\//, "");
    default:
      return n.ref;
  }
}

function statusClass(status: string): string {
  if (status === "purged" || status === "deleted") return "purged";
  if (status === "needs_regeneration") return "regen";
  if (status === "needs_retraining") return "retrain";
  return "active";
}

function layout(lineage: Lineage): { nodes: Node[]; edges: Edge[] } {
  const columns = new Map<number, LineageNode[]>();
  for (const n of lineage.nodes) {
    const col = TYPE_META[n.type]?.column ?? 4;
    columns.set(col, [...(columns.get(col) ?? []), n]);
  }
  const nodes: Node[] = [];
  for (const [col, members] of columns) {
    members.sort(
      (a, b) =>
        STACK_ORDER.indexOf(a.type) - STACK_ORDER.indexOf(b.type) ||
        a.ref.localeCompare(b.ref, undefined, { numeric: true }),
    );
    let y = 0;
    let prevType: string | undefined;
    for (const n of members) {
      if (prevType && prevType !== n.type) y += GROUP_GAP;
      prevType = n.type;
      const meta = TYPE_META[n.type];
      nodes.push({
        id: n.id,
        position: { x: col * COL_WIDTH, y },
        data: {
          label: (
            <div className="flow-node-label">
              <span className="flow-node-type" style={{ color: meta.color }}>
                {meta.label}
              </span>
              <span className="flow-node-ref">{shortLabel(n)}</span>
            </div>
          ),
        },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        className: `flow-node ${statusClass(n.status)}`,
        style: { borderLeftColor: meta.color },
      });
      y += ROW_HEIGHT;
    }
  }
  // Draw edges upstream -> downstream: derived_from / trained_on point child -> parent.
  const edges: Edge[] = lineage.edges.map((e) => {
    const reversed = e.type === "derived_from" || e.type === "trained_on";
    const color = EDGE_COLORS[e.type] ?? "#94a3b8";
    return {
      id: `${e.source}|${e.target}|${e.type}`,
      source: reversed ? e.target : e.source,
      target: reversed ? e.source : e.target,
      style: { stroke: color, strokeWidth: e.type === "trained_on" ? 2 : 1.2 },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
      animated: e.type === "trained_on",
    };
  });
  return { nodes, edges };
}

interface Props {
  lineage: Lineage;
  onSelect: (node: LineageNode | undefined) => void;
}

export default function LineageGraph({ lineage, onSelect }: Props) {
  const { nodes, edges } = useMemo(() => layout(lineage), [lineage]);
  const byId = useMemo(() => new Map(lineage.nodes.map((n) => [n.id, n])), [lineage]);
  return (
    <div className="graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.08 }}
        minZoom={0.1}
        nodesConnectable={false}
        onNodeClick={(_, n) => onSelect(byId.get(n.id))}
        onPaneClick={() => onSelect(undefined)}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function GraphLegend() {
  return (
    <div className="legend">
      {(Object.keys(TYPE_META) as NodeType[]).map((t) => (
        <span key={t} className="legend-item">
          <span className="swatch" style={{ background: TYPE_META[t].color }} />
          {TYPE_META[t].label}
        </span>
      ))}
      <span className="legend-item">
        <span className="swatch purged-swatch" /> purged
      </span>
      <span className="legend-item">
        <span className="swatch regen-swatch" /> needs regeneration
      </span>
      <span className="legend-item">
        <span className="swatch retrain-swatch" /> needs retraining
      </span>
    </div>
  );
}
