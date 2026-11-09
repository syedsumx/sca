import { Alert, Button, Empty, Input, Modal, Space, Spin, Table, Tag } from 'antd';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi } from '../api/hooks';
import { Muted, tokens } from '../components/tokens';
import {
    ElementStatus,
    Proposal,
    TermDetail as Detail,
    TermRelationship,
    TYPE_LABEL,
    assetHref,
    cardinalityLabel,
    ontologyApi,
    relationshipType,
    tableLabel,
    termHref,
} from './api';

const Section = styled.section`
    margin-top: 16px;
    h3 {
        font-size: 14px;
        font-weight: 600;
        margin: 0 0 6px;
    }
    dl {
        display: grid;
        grid-template-columns: max-content 1fr;
        gap: 4px 12px;
        margin: 0;
    }
    dt {
        color: ${tokens.color.textMuted};
    }
    dd {
        margin: 0;
    }
`;

export const STATUS_COLOUR: Record<ElementStatus, string> = { accepted: 'green', proposed: 'gold', rejected: 'red', compiled: 'default' };

export function StatusTag({ status }: { status: ElementStatus }) {
    return <Tag color={STATUS_COLOUR[status]}>{status}</Tag>;
}

function who(urn?: string | null): string {
    return urn ? urn.replace(/^urn:li:corpuser:/, '') : '';
}

function Evidence({ r }: { r: TermRelationship }) {
    const e = r.evidence;
    return (
        <Space size={4} wrap>
            {e.foreign_key && <Tag color="blue" title={e.foreign_key.detail}>declared foreign key</Tag>}
            {e.query_log && (
                <Tag color="purple" title={e.query_log.query_ids.join(', ')}>
                    joined in {e.query_log.queries} {e.query_log.queries === 1 ? 'query' : 'queries'} ({e.query_log.executions} runs)
                </Tag>
            )}
            {e.model && (
                <Tag color="cyan" title={e.model.detail}>
                    {e.model.inferred_without_model ? 'inferred' : 'model'}: confidence {Math.round(e.model.confidence * 100)}%
                </Tag>
            )}
        </Space>
    );
}

/** A reviewer's decision on one proposal, with an optional comment (and, for a term, its name, definition and
 * synonyms edited as it is accepted). */
export function ProposalActions({ p, onDone, term }: { p: Proposal; onDone: () => void; term?: Detail }) {
    const { client } = useConsumerApi();
    const api = useMemo(() => ontologyApi(client), [client]);
    const [comment, setComment] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [editing, setEditing] = useState(false);
    const [name, setName] = useState(term?.name ?? '');
    const [definition, setDefinition] = useState(term?.description ?? '');
    const [synonyms, setSynonyms] = useState((term?.synonyms ?? []).join(', '));

    const decide = async (action: 'accept' | 'reject', edits?: { name?: string; definition?: string; synonyms?: string[] }) => {
        setBusy(true);
        setError(null);
        try {
            await api.decide(p.id, action, comment, edits);
            setEditing(false);
            onDone();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <div>
            <Space wrap>
                <Input
                    size="small"
                    aria-label={`Comment on ${p.title}`}
                    placeholder="Comment (optional)"
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                    style={{ width: 220 }}
                />
                <Button size="small" type="primary" loading={busy} onClick={() => void decide('accept')}>Accept</Button>
                <Button size="small" danger loading={busy} onClick={() => void decide('reject')}>Reject</Button>
                {p.kind === 'term' && term && <Button size="small" onClick={() => setEditing(true)}>Edit and accept</Button>}
            </Space>
            {error && <Alert style={{ marginTop: 6 }} type="error" showIcon message={error} />}
            <Modal
                title={`Edit “${term?.name}” before accepting`}
                open={editing}
                okText="Accept with these changes"
                confirmLoading={busy}
                onCancel={() => setEditing(false)}
                onOk={() => void decide('accept', {
                    name: name.trim() || undefined,
                    definition: definition.trim() || undefined,
                    synonyms: synonyms.split(',').map((s) => s.trim()).filter(Boolean),
                })}
            >
                <Space direction="vertical" style={{ width: '100%' }}>
                    <label>
                        Name
                        <Input value={name} onChange={(e) => setName(e.target.value)} />
                    </label>
                    <label>
                        Definition
                        <Input.TextArea rows={3} value={definition} onChange={(e) => setDefinition(e.target.value)} />
                    </label>
                    <label>
                        Synonyms (comma separated)
                        <Input value={synonyms} onChange={(e) => setSynonyms(e.target.value)} />
                    </label>
                </Space>
            </Modal>
        </div>
    );
}

/** Everything known about one term: definition and synonyms, domain and owners, mapped columns, relationships with
 * their evidence, metrics, provenance, date formats and the proposals still open on it. */
export function TermDetailView({ id, compact = false }: { id: string; compact?: boolean }) {
    const { client } = useConsumerApi();
    const api = useMemo(() => ontologyApi(client), [client]);
    const [d, setD] = useState<Detail | null>(null);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setError(null);
        try {
            setD(await api.term(id));
        } catch (e) {
            setError((e as Error).message);
        }
    }, [api, id]);
    useEffect(() => {
        setD(null);
        void load();
    }, [load]);

    if (error) return <Alert type="warning" showIcon message="This term is not available" description={error} />;
    if (!d) return <Spin />;
    const H = compact ? 'h3' : 'h2';
    return (
        <div data-testid="term-detail">
            <Space align="baseline" wrap>
                {React.createElement(H, { style: { margin: 0 } }, d.name)}
                <StatusTag status={d.status} />
                {compact && <Link to={termHref(d.id)}>Open the term page</Link>}
            </Space>
            <p style={{ margin: '6px 0 0' }}>{d.description || <Muted>No definition yet.</Muted>}</p>
            <Section aria-label="About">
                <dl>
                    <dt>Synonyms</dt>
                    <dd>{d.synonyms.length ? d.synonyms.map((s) => <Tag key={s}>{s}</Tag>) : <Muted>none</Muted>}</dd>
                    <dt>Domain</dt>
                    <dd>{d.domain_name ?? <Muted>none (schema {d.schema || '—'})</Muted>}</dd>
                    <dt>Owners</dt>
                    <dd>{d.owners.length ? d.owners.map((o) => <Tag key={o}>{who(o)}</Tag>) : <Muted>none</Muted>}</dd>
                    <dt>Table</dt>
                    <dd><Link to={assetHref(d.table)}>{d.table_name}</Link></dd>
                    {d.glossary_term && (
                        <>
                            <dt>Glossary</dt>
                            <dd><code>{d.glossary_term}</code></dd>
                        </>
                    )}
                </dl>
            </Section>
            <Section aria-label="Mapped columns">
                <h3>Mapped columns ({d.attributes.length})</h3>
                <Table
                    size="small"
                    rowKey="id"
                    pagination={false}
                    dataSource={d.attributes}
                    columns={[
                        { title: 'Column', dataIndex: 'column', render: (c: string) => <Link to={assetHref(d.table)}><code>{c}</code></Link> },
                        { title: 'Label', dataIndex: 'name' },
                        { title: 'Type', dataIndex: 'datatype' },
                        { title: 'Date format', dataIndex: 'format', render: (f: string | null) => f ? <Tag>{f}</Tag> : '' },
                        ...(compact ? [] : [{ title: 'Description', dataIndex: 'description', render: (v: string | null) => v ?? '' }]),
                    ]}
                />
            </Section>
            {d.date_formats.length > 0 && (
                <Section aria-label="Date formats">
                    <h3>Date formats detected</h3>
                    {d.date_formats.map((f) => (
                        <div key={f.column}><code>{f.column}</code> is written as <Tag>{f.format}</Tag></div>
                    ))}
                </Section>
            )}
            <Section aria-label="Relationships">
                <h3>Relationships ({d.relationships.length})</h3>
                {d.relationships.length === 0 && <Muted>None you may see.</Muted>}
                {d.relationships.map((r) => (
                    <div key={r.id} style={{ padding: '6px 0', borderBottom: `1px solid ${tokens.color.border}` }}>
                        <Space wrap size={6}>
                            <code>{r.join}</code>
                            <Tag>{cardinalityLabel(r.cardinality)}</Tag>
                            <Tag>{TYPE_LABEL[relationshipType(r.evidence.sources)]}</Tag>
                            {r.other_term ? <Link to={termHref(r.other_term.id)}>{r.other_term.name}</Link> :
                                <Link to={assetHref(r.other_table)}>{tableLabel(r.other_table)}</Link>}
                            {!r.join_default && <Tag title="Another relationship between these tables is the default join">another role</Tag>}
                            <StatusTag status={r.status} />
                        </Space>
                        <div style={{ marginTop: 4 }}><Evidence r={r} /></div>
                        {r.rationale && <Muted style={{ fontSize: 12 }}>{r.rationale}</Muted>}
                    </div>
                ))}
            </Section>
            <Section aria-label="Metrics">
                <h3>Metrics using this term ({d.metrics.length})</h3>
                {d.metrics.length === 0 && <Muted>None yet.</Muted>}
                {d.metrics.map((m) => (
                    <div key={m.id} style={{ marginBottom: 6 }}>
                        <strong>{m.name}</strong> <code>{m.expression}</code> <StatusTag status={m.status} />
                        {m.source === 'bridgeTable' && <Tag color="orange">through a bridge table</Tag>}
                        {m.description && <div><Muted>{m.description}</Muted></div>}
                    </div>
                ))}
            </Section>
            {d.bridges.length > 0 && (
                <Section aria-label="Bridge tables">
                    <h3>Bridge tables</h3>
                    {d.bridges.map((b) => (
                        <div key={b.id}>
                            <Link to={assetHref(b.table)}>{b.table_name}</Link> joins {b.targets.map(tableLabel).join(' and ')}
                        </div>
                    ))}
                </Section>
            )}
            <Section aria-label="Provenance">
                <h3>Provenance</h3>
                <dl>
                    <dt>Run</dt>
                    <dd>
                        <code>{d.provenance.run.id}</code>
                        {d.provenance.run.version !== undefined && ` (version ${d.provenance.run.version}, ${d.provenance.run.mode}`}
                        {d.provenance.run.started_ms ? `, ${new Date(d.provenance.run.started_ms).toLocaleString()}` : ''}
                        {d.provenance.run.requested_by ? `, by ${who(d.provenance.run.requested_by)}` : ''}
                        {d.provenance.run.version !== undefined && ')'}
                    </dd>
                    <dt>From</dt>
                    <dd>{d.provenance.sources.map((s) => <Tag key={s.source} title={s.detail}>{s.source}{s.confidence != null ? ` ${Math.round(s.confidence * 100)}%` : ''}</Tag>)}</dd>
                    <dt>Input tables</dt>
                    <dd>{d.provenance.inputs.tables.map((t) => <Link key={t} to={assetHref(t)} style={{ marginRight: 8 }}>{tableLabel(t)}</Link>)}</dd>
                    <dt>Queries</dt>
                    <dd>{d.provenance.inputs.queries.length ? `${d.provenance.inputs.queries.length} from the query log` : <Muted>none</Muted>}</dd>
                </dl>
            </Section>
            <Section aria-label="Open proposals">
                <h3>Pending proposals ({d.proposals.length})</h3>
                {d.proposals.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Nothing waiting for review" />}
                {d.proposals.map((p) => (
                    <div key={p.id} style={{ padding: '6px 0', borderBottom: `1px solid ${tokens.color.border}` }}>
                        <div><Tag>{p.kind}</Tag>{p.title} <Muted>({Math.round(p.confidence * 100)}%)</Muted></div>
                        <ProposalActions p={p} term={p.kind === 'term' ? d : undefined} onDone={() => void load()} />
                    </div>
                ))}
            </Section>
        </div>
    );
}
