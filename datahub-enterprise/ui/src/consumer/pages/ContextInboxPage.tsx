import { Alert, Button, Empty, Radio, Select, Tabs, Tag, message } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { Finding, FindingType, Ontology } from '../api/types';
import { assetPath } from '../components/Assets';
import { Muted, Page, SectionTitle, Skeleton, Surface, tokens } from '../components/tokens';
import { ConfidenceMeter } from '../components/TrustBadges';
import { relativeTime, shortUrn } from '../utils/format';
import { forceLayout } from '../utils/forceLayout';
import { MarkdownView } from '../utils/markdown';

export const FINDING_LABELS: Record<FindingType, { label: string; icon: string; color: string }> = {
    TERM_SUGGESTION: { label: 'New business term', icon: '📖', color: 'purple' },
    TERM_LINK: { label: 'Term ↔ column link', icon: '🔗', color: 'geekblue' },
    METRIC_DEFINITION: { label: 'Metric definition', icon: '📐', color: 'cyan' },
    DEFINITION_DRIFT: { label: 'Definition drift', icon: '⚠️', color: 'orange' },
    DOC_STALENESS: { label: 'Stale documentation', icon: '🕰', color: 'gold' },
    JOIN_RELATIONSHIP: { label: 'Join relationship', icon: '⋈', color: 'blue' },
    RUN_ANOMALY: { label: 'Pipeline run anomaly', icon: '🏃', color: 'red' },
};

const FindingCard = styled(Surface)`
    padding: 14px 16px;
    margin-bottom: 12px;
    pre {
        white-space: pre-wrap;
        font-size: 12px;
        background: ${tokens.color.surfaceAlt};
        padding: 6px 8px;
        border-radius: 6px;
        margin: 4px 0 0;
    }
`;

export function FindingView({ finding, onAct }: { finding: Finding; onAct?: (action: 'accept' | 'dismiss') => void }) {
    const meta = FINDING_LABELS[finding.type] ?? { label: finding.type, icon: '•', color: 'default' };
    const [showEvidence, setShowEvidence] = useState(false);
    return (
        <FindingCard aria-label={`${meta.label}: ${finding.title}`}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
                <div>
                    <Tag color={meta.color}>
                        {meta.icon} {meta.label}
                    </Tag>
                    <strong>{finding.title}</strong>
                    <div style={{ marginTop: 4 }}>
                        <ConfidenceMeter value={finding.confidence} /> <Muted>· {relativeTime(finding.created_at)}</Muted>
                    </div>
                </div>
                {finding.status === 'OPEN' && onAct ? (
                    <span style={{ display: 'flex', gap: 6 }}>
                        <Button type="primary" size="small" onClick={() => onAct('accept')}>
                            Accept
                        </Button>
                        <Button size="small" onClick={() => onAct('dismiss')}>
                            Dismiss
                        </Button>
                    </span>
                ) : (
                    <Tag>{finding.status}</Tag>
                )}
            </div>
            <div style={{ marginTop: 8 }}>
                <MarkdownView source={finding.summary_md} />
            </div>
            <div>
                {finding.entities.slice(0, 6).map((u) => (
                    <Link key={u} to={assetPath(u)} style={{ marginRight: 8, fontSize: 12 }}>
                        {shortUrn(u)}
                    </Link>
                ))}
            </div>
            {finding.evidence.length > 0 && (
                <div style={{ marginTop: 6 }}>
                    <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setShowEvidence(!showEvidence)} aria-expanded={showEvidence}>
                        {showEvidence ? 'Hide' : 'Show'} evidence ({finding.evidence.length})
                    </Button>
                    {showEvidence &&
                        finding.evidence.map((e, i) => (
                            // eslint-disable-next-line react/no-array-index-key
                            <div key={i} style={{ marginTop: 6 }}>
                                <Tag>{e.source}</Tag> <Muted style={{ fontSize: 12 }}>{e.ref}</Muted>
                                {e.snippet && <pre>{e.snippet}</pre>}
                            </div>
                        ))}
                </div>
            )}
        </FindingCard>
    );
}

const KIND_COLORS: Record<string, string> = { term: '#7c3aed', column: '#0e7490', metric: '#b45309', dataset: '#1d4ed8' };

export function OntologyView({ ontology }: { ontology: Ontology }) {
    const width = 900;
    const height = 560;
    const positions = useMemo(() => forceLayout(ontology.nodes, ontology.edges, { width, height }), [ontology]);
    const [hover, setHover] = useState<string | null>(null);
    const neighbours = useMemo(() => {
        if (!hover) return null;
        const s = new Set([hover]);
        ontology.edges.forEach((e) => {
            if (e.source === hover) s.add(e.target);
            if (e.target === hover) s.add(e.source);
        });
        return s;
    }, [hover, ontology.edges]);
    if (!ontology.nodes.length) return <Empty description="The ontology grows as you accept findings and add glossary terms." />;
    return (
        <Surface style={{ padding: 8, overflow: 'auto' }}>
            <svg viewBox={`0 0 ${width} ${height}`} width="100%" role="img" aria-label="Business ontology graph">
                {ontology.edges.map((e) => {
                    const a = positions[e.source];
                    const b = positions[e.target];
                    if (!a || !b) return null;
                    const dim = neighbours && !(neighbours.has(e.source) && neighbours.has(e.target));
                    return (
                        <line
                            key={`${e.source}->${e.target}-${e.kind}`}
                            x1={a.x}
                            y1={a.y}
                            x2={b.x}
                            y2={b.y}
                            stroke="#9ca3af"
                            strokeOpacity={dim ? 0.15 : 0.7}
                            strokeWidth={Math.min(1 + e.weight * 0.3, 4)}
                        >
                            <title>{e.kind}</title>
                        </line>
                    );
                })}
                {ontology.nodes.map((n) => {
                    const p = positions[n.id];
                    const dim = neighbours && !neighbours.has(n.id);
                    return (
                        <g
                            key={n.id}
                            transform={`translate(${p.x},${p.y})`}
                            opacity={dim ? 0.25 : 1}
                            onMouseEnter={() => setHover(n.id)}
                            onMouseLeave={() => setHover(null)}
                            tabIndex={0}
                            onFocus={() => setHover(n.id)}
                            onBlur={() => setHover(null)}
                            aria-label={`${n.kind} ${n.label}`}
                        >
                            <circle r={n.kind === 'term' ? 9 : 6} fill={KIND_COLORS[n.kind] ?? '#6b7280'} />
                            <text x={12} y={4} fontSize={11} fill={tokens.color.text}>
                                {n.label.length > 28 ? `${n.label.slice(0, 27)}…` : n.label}
                            </text>
                        </g>
                    );
                })}
            </svg>
            <div style={{ fontSize: 12, padding: '4px 8px' }}>
                {Object.entries(KIND_COLORS).map(([k, c]) => (
                    <span key={k} style={{ marginRight: 14 }}>
                        <span style={{ display: 'inline-block', width: 9, height: 9, borderRadius: 5, background: c, marginRight: 4 }} />
                        {k}
                    </span>
                ))}
            </div>
        </Surface>
    );
}

export function ContextInboxPage() {
    const { client, cache } = useConsumerApi();
    const [status, setStatus] = useState<string>('OPEN');
    const [type, setType] = useState<string | undefined>(undefined);
    const key = `/context/findings?status=${status}&type=${type ?? ''}`;
    const findings = useDheQuery<Finding[]>(key, (c, signal) => c.listFindings({ status, type }, { signal }));
    const ontology = useDheQuery<Ontology>('/context/ontology', (c, signal) => c.ontology({ limit: 150 }, { signal }));

    const act = async (f: Finding, action: 'accept' | 'dismiss') => {
        cache.set<Finding[]>(key, (prev) => (prev ?? []).filter((x) => x.id !== f.id));
        try {
            await client.actOnFinding(f.id, action);
            message.success(action === 'accept' ? 'Accepted — metadata updated' : 'Dismissed');
            cache.invalidate('/context');
        } catch (e) {
            cache.invalidate(key);
            message.error((e as Error).message);
        }
    };

    const grouped = useMemo(() => {
        const g: Record<string, Finding[]> = {};
        (findings.data ?? []).forEach((f) => {
            (g[f.type] ??= []).push(f);
        });
        return g;
    }, [findings.data]);

    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                Context inbox
            </SectionTitle>
            <Muted>
                Context Agents continuously mine queries, dbt, BI semantics and pipeline runs to suggest business definitions, detect
                drift and keep documentation alive.
            </Muted>
            <Tabs
                style={{ marginTop: 12 }}
                items={[
                    {
                        key: 'findings',
                        label: 'Findings',
                        children: (
                            <>
                                <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
                                    <Radio.Group
                                        aria-label="Finding status"
                                        optionType="button"
                                        value={status}
                                        onChange={(e) => setStatus(String(e.target.value))}
                                        options={[
                                            { label: 'Open', value: 'OPEN' },
                                            { label: 'Accepted', value: 'ACCEPTED' },
                                            { label: 'Dismissed', value: 'DISMISSED' },
                                        ]}
                                    />
                                    <Select
                                        aria-label="Finding type"
                                        allowClear
                                        placeholder="All types"
                                        style={{ minWidth: 220 }}
                                        value={type}
                                        onChange={(v) => setType(v)}
                                        options={Object.entries(FINDING_LABELS).map(([k, v]) => ({ value: k, label: `${v.icon} ${v.label}` }))}
                                    />
                                </div>
                                {findings.loading && <Skeleton h={90} />}
                                {findings.error && <Alert type="error" message={findings.error.message} />}
                                {findings.data?.length === 0 && <Empty description="Inbox zero — no findings with these filters." />}
                                {Object.entries(grouped).map(([t, items]) => (
                                    <section key={t} aria-label={FINDING_LABELS[t as FindingType]?.label ?? t}>
                                        <h3 style={{ fontSize: 14, margin: '12px 0 8px' }}>
                                            {FINDING_LABELS[t as FindingType]?.icon} {FINDING_LABELS[t as FindingType]?.label ?? t} ({items.length})
                                        </h3>
                                        {items.map((f) => (
                                            <FindingView key={f.id} finding={f} onAct={(a) => void act(f, a)} />
                                        ))}
                                    </section>
                                ))}
                            </>
                        ),
                    },
                    {
                        key: 'ontology',
                        label: 'Ontology',
                        children: ontology.loading ? (
                            <Skeleton h={300} />
                        ) : ontology.error ? (
                            <Alert type="error" message={ontology.error.message} />
                        ) : (
                            <OntologyView ontology={ontology.data ?? { nodes: [], edges: [] }} />
                        ),
                    },
                ]}
            />
        </Page>
    );
}
