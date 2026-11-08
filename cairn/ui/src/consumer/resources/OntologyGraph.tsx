import { Alert, Empty, Spin } from 'antd';
import React, { useMemo, useState } from 'react';
import styled from 'styled-components';

import { useDheQuery } from '../api/hooks';
import { Muted, tokens } from '../components/tokens';

/** The compiled ontology's graph as Cairn serves it (`GET /v1/ontology/graph?format=json`). */
export interface OntologyGraphData {
    run_id?: string;
    version?: number;
    nodes: { id: string; kind: string; label: string; description?: string }[];
    edges: { source: string; target: string; kind: string; on?: string; confidence?: number; provenance?: string[];
        source_term?: string | null; target_term?: string | null }[];
}

const Box = styled.div`
    border: 1px solid ${tokens.color.border};
    border-radius: 12px;
    background: ${tokens.color.surface};
    padding: 12px;
    svg { width: 100%; height: auto; display: block; }
    text { font-size: 12px; fill: ${tokens.color.text}; }
`;

interface Laid {
    terms: { id: string; label: string; x: number; y: number; table: string }[];
    links: { from: { x: number; y: number }; to: { x: number; y: number }; label: string; observed: boolean;
        confidence: number }[];
}

/** Terms on a circle, a line for each relationship between the tables they represent (dashed: only seen in queries). */
export function layout(g: OntologyGraphData, size = 560): Laid {
    const termOfTable = new Map<string, string>();
    for (const e of g.edges) if (e.kind === 'represents' && !termOfTable.has(e.target)) termOfTable.set(e.target, e.source);
    const termNodes = g.nodes.filter((n) => n.kind === 'term');
    const r = size / 2 - 70;
    const terms = termNodes.map((n, i) => {
        const a = (2 * Math.PI * i) / Math.max(termNodes.length, 1) - Math.PI / 2;
        const table = [...termOfTable.entries()].find(([, t]) => t === n.id)?.[0] ?? '';
        return { id: n.id, label: n.label, x: size / 2 + r * Math.cos(a), y: size / 2 + r * Math.sin(a), table };
    });
    const at = new Map(terms.map((t) => [t.id, t]));
    const links = g.edges
        .filter((e) => e.kind === 'references' && e.source_term && e.target_term && at.has(e.source_term) && at.has(e.target_term))
        .map((e) => ({
            from: at.get(e.source_term as string)!, to: at.get(e.target_term as string)!, label: e.on ?? '',
            observed: (e.provenance ?? []).length === 1 && e.provenance?.[0] === 'observed_in_queries',
            confidence: e.confidence ?? 0,
        }));
    return { terms, links };
}

export function OntologyGraphView({ path }: { path: string }) {
    const q = useDheQuery<OntologyGraphData>(`resource:${path}`, (c, signal) => c.request<OntologyGraphData>('GET', path, undefined, { signal }));
    const [hover, setHover] = useState<string | null>(null);
    const laid = useMemo(() => (q.data ? layout(q.data) : null), [q.data]);
    if (q.error) return <Alert type="warning" showIcon message="The ontology graph is not available" description={q.error.message} />;
    if (!laid) return <Spin />;
    if (!laid.terms.length) return <Empty description="No compiled terms you may see yet" />;
    const size = 560;
    return (
        <Box>
            <Muted>
                Version {q.data?.version}: {laid.terms.length} terms, {laid.links.length} relationships between them.
                Dashed lines are joins seen only in queries. Only datasets you may see are shown.
            </Muted>
            <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Ontology graph of terms and relationships">
                {laid.links.map((l, i) => (
                    <g key={i} opacity={hover && hover !== l.label ? 0.35 : 1}>
                        <line x1={l.from.x} y1={l.from.y} x2={l.to.x} y2={l.to.y} stroke={tokens.color.primary}
                            strokeWidth={1 + 2 * l.confidence} strokeDasharray={l.observed ? '5 4' : undefined}
                            onMouseEnter={() => setHover(l.label)} onMouseLeave={() => setHover(null)}>
                            <title>{l.label}</title>
                        </line>
                    </g>
                ))}
                {laid.terms.map((t) => (
                    <g key={t.id}>
                        <circle cx={t.x} cy={t.y} r={9} fill={tokens.color.primarySoft} stroke={tokens.color.primary} />
                        <text x={t.x} y={t.y - 14} textAnchor="middle">{t.label}</text>
                        <title>{t.table}</title>
                    </g>
                ))}
            </svg>
        </Box>
    );
}
