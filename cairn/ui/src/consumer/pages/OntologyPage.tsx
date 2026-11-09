import { Alert, AutoComplete, Button, Empty, InputNumber, Select, Space, Spin, Table, Tabs, Tag } from 'antd';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useHistory, useLocation, useParams } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi } from '../api/hooks';
import { Muted, Page, tokens } from '../components/tokens';
import {
    ElementStatus,
    ExplorerData,
    ExplorerRelationship,
    ExplorerTerm,
    ExportFormat,
    TYPE_LABEL,
    assetHref,
    cardinalityLabel,
    ontologyApi,
    relationshipType,
    tableLabel,
    termHref,
} from '../ontology/api';
import { CompileWizard } from '../ontology/CompileWizard';
import {
    CLUSTER_THRESHOLD,
    Filters,
    GNode,
    NO_FILTERS,
    applyFilters,
    buildGraph,
    cluster,
    facets,
    layout,
    neighbours,
    searchNodes,
} from '../ontology/graph';
import { OntologyCanvas } from '../ontology/OntologyCanvas';
import { ReviewQueue } from '../ontology/ReviewQueue';
import { StatusTag, TermDetailView } from '../ontology/TermDetail';

const Head = styled.header`
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 12px;
    h1 {
        margin: 0;
        font-size: 26px;
        font-weight: 700;
    }
    p {
        margin: 4px 0 0;
        color: ${tokens.color.textMuted};
        max-width: 760px;
    }
`;

const Bar = styled.div`
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin-bottom: 8px;
`;

const Split = styled.div<{ $open: boolean }>`
    display: grid;
    grid-template-columns: ${(p) => (p.$open ? 'minmax(0, 1fr) 440px' : 'minmax(0, 1fr)')};
    gap: 12px;
    align-items: start;
    @media (max-width: 1100px) {
        grid-template-columns: minmax(0, 1fr);
    }
`;

const Side = styled.aside`
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    background: ${tokens.color.surface};
    padding: 14px;
    max-height: 760px;
    overflow: auto;
`;

const TABS = ['graph', 'list', 'review', 'compile'] as const;
type Tab = (typeof TABS)[number];
const STATUSES: ElementStatus[] = ['accepted', 'proposed', 'compiled', 'rejected'];
const EXT: Record<ExportFormat, [string, string]> = {
    jsonld: ['ontology.jsonld', 'application/ld+json'],
    turtle: ['ontology.ttl', 'text/turtle'],
    csv: ['ontology.csv', 'text/csv'],
};

function download(name: string, type: string, body: string) {
    const url = URL.createObjectURL(new Blob([body], { type }));
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The graph tab: filters, search (zoom to a term and light up its neighbours), the canvas, and a side panel. */
function GraphTab({ data }: { data: ExplorerData }) {
    const full = useMemo(() => buildGraph(data), [data]);
    const { groups, schemas } = useMemo(() => facets(full), [full]);
    const [filters, setFilters] = useState<Filters>(NO_FILTERS);
    const [everything, setEverything] = useState(false);
    const [selected, setSelected] = useState<string | null>(null);
    const [focus, setFocus] = useState<{ id: string; nonce: number } | null>(null);
    const [query, setQuery] = useState('');
    const filtered = useMemo(() => applyFilters(full, filters), [full, filters]);
    const clustered = !everything && filtered.nodes.length > CLUSTER_THRESHOLD;
    const shown = useMemo(() => (clustered ? cluster(filtered) : filtered), [clustered, filtered]);
    const positions = useMemo(() => layout(shown), [shown]);
    const highlight = useMemo(() => (selected && !clustered ? neighbours(shown, selected) : null), [selected, shown, clustered]);
    const options = useMemo(() => searchNodes(filtered, query).map((n) => ({ value: n.id, label: n.label })), [filtered, query]);
    const selectedNode = selected ? shown.nodes.find((n) => n.id === selected) : undefined;

    const goTo = (id: string) => {
        const n = filtered.nodes.find((x) => x.id === id);
        if (!n) return;
        if (clustered) setFilters((f) => ({ ...f, domains: [n.group] }));
        setSelected(id);
        setFocus({ id, nonce: Date.now() });
    };
    const onSelect = (n: GNode) => {
        if (n.kind === 'cluster') {
            setFilters((f) => ({ ...f, domains: [n.group] }));
            setSelected(null);
            return;
        }
        setSelected(n.id === selected ? null : n.id);
    };

    return (
        <div>
            <Bar>
                <AutoComplete
                    aria-label="Find a term"
                    placeholder="Find a term and zoom to it"
                    style={{ width: 260 }}
                    options={options}
                    value={query}
                    onChange={setQuery}
                    onSelect={(id: string) => {
                        setQuery(filtered.nodes.find((n) => n.id === id)?.label ?? '');
                        goTo(id);
                    }}
                />
                <Select mode="multiple" allowClear aria-label="Domains" placeholder="Domains" style={{ minWidth: 180 }} value={filters.domains}
                    onChange={(domains) => setFilters((f) => ({ ...f, domains }))} options={groups.map((g) => ({ value: g, label: g }))} />
                <Select mode="multiple" allowClear aria-label="Schemas" placeholder="Schemas" style={{ minWidth: 160 }} value={filters.schemas}
                    onChange={(s) => setFilters((f) => ({ ...f, schemas: s }))} options={schemas.map((g) => ({ value: g, label: g }))} />
                <Select mode="multiple" allowClear aria-label="Status" placeholder="Status" style={{ minWidth: 150 }} value={filters.statuses}
                    onChange={(statuses) => setFilters((f) => ({ ...f, statuses }))} options={STATUSES.map((g) => ({ value: g, label: g }))} />
                <label>
                    Confidence ≥{' '}
                    <InputNumber aria-label="Minimum confidence" min={0} max={1} step={0.05} value={filters.minConfidence} style={{ width: 80 }}
                        onChange={(v) => setFilters((f) => ({ ...f, minConfidence: Number(v ?? 0) }))} />
                </label>
                {(filters !== NO_FILTERS) && <Button onClick={() => setFilters(NO_FILTERS)}>Clear filters</Button>}
                {filtered.nodes.length > CLUSTER_THRESHOLD && (
                    <Button onClick={() => setEverything((x) => !x)}>{everything ? 'Group by domain' : 'Show every term'}</Button>
                )}
            </Bar>
            <Muted style={{ display: 'block', fontSize: 12, marginBottom: 6 }} aria-live="polite">
                {clustered
                    ? `${filtered.nodes.length} terms grouped into ${shown.nodes.length} domains: open a group, filter, or search to see its terms.`
                    : `${shown.nodes.length} nodes, ${shown.edges.length} relationships. Click a term for its details; its neighbours light up.`}
            </Muted>
            <Split $open={!!selectedNode}>
                {shown.nodes.length === 0 ? (
                    <Empty description="Nothing matches these filters" />
                ) : (
                    <OntologyCanvas graph={shown} positions={positions} groups={groups} selected={selected} highlight={highlight} focus={focus} onSelect={onSelect} />
                )}
                {selectedNode && (
                    <Side aria-label={`Details of ${selectedNode.label}`}>
                        <Button size="small" style={{ float: 'right' }} onClick={() => setSelected(null)} aria-label="Close the details">Close</Button>
                        {selectedNode.kind === 'term' && <TermDetailView id={selectedNode.id} compact />}
                        {selectedNode.kind !== 'term' && selectedNode.table && (
                            <div>
                                <h3>{selectedNode.label}</h3>
                                <p>{selectedNode.kind === 'bridge' ? 'A bridge table: it only joins other tables (every column is a foreign key).' : 'A table without a business term.'}</p>
                                <Link to={assetHref(selectedNode.table)}>Open {tableLabel(selectedNode.table)}</Link>
                            </div>
                        )}
                    </Side>
                )}
            </Split>
        </div>
    );
}

/** The same ontology as tables: the accessible alternative to the graph, and quicker for big ontologies. */
function ListTab({ data }: { data: ExplorerData }) {
    const terms = new Map(data.terms.map((t) => [t.id, t]));
    const end = (r: ExplorerRelationship, side: 'source' | 'target') => {
        const t = terms.get((side === 'source' ? r.source_term : r.target_term) ?? '');
        const col = side === 'source' ? r.source_column : r.target_column;
        return t ? <><Link to={termHref(t.id)}>{t.name}</Link> <code>{col}</code></> : <><Link to={assetHref(r[side])}>{tableLabel(r[side])}</Link> <code>{col}</code></>;
    };
    return (
        <div>
            <h2 style={{ fontSize: 16 }}>Terms ({data.terms.length})</h2>
            <Table<ExplorerTerm>
                size="small"
                rowKey="id"
                dataSource={[...data.terms].sort((a, b) => a.name.localeCompare(b.name))}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    { title: 'Term', dataIndex: 'name', render: (n: string, t) => <Link to={termHref(t.id)}>{n}</Link> },
                    { title: 'Synonyms', dataIndex: 'synonyms', render: (s: string[]) => s.join(', ') },
                    { title: 'Domain', dataIndex: 'domain_name', render: (d: string | null, t) => d ?? <Muted>schema {t.schema}</Muted> },
                    { title: 'Table', dataIndex: 'table', render: (u: string, t) => <Link to={assetHref(u)}>{t.table_name}</Link> },
                    { title: 'Columns', dataIndex: 'attributes' },
                    { title: 'Status', dataIndex: 'status', render: (s: ElementStatus) => <StatusTag status={s} /> },
                    { title: 'Confidence', dataIndex: 'confidence', render: (c: number) => `${Math.round(c * 100)}%` },
                ]}
            />
            <h2 style={{ fontSize: 16, marginTop: 16 }}>Relationships ({data.relationships.length})</h2>
            <Table<ExplorerRelationship>
                size="small"
                rowKey="id"
                dataSource={data.relationships}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    { title: 'From', key: 'from', render: (_: unknown, r) => end(r, 'source') },
                    { title: 'To', key: 'to', render: (_: unknown, r) => end(r, 'target') },
                    { title: 'Cardinality', dataIndex: 'cardinality', render: (c: string) => cardinalityLabel(c) },
                    { title: 'Known from', dataIndex: 'provenance', render: (p: string[], r) => <>{TYPE_LABEL[relationshipType(p)]}{r.bridge && <Tag color="orange" style={{ marginLeft: 4 }}>bridge</Tag>}{r.queries ? ` · ${r.queries} queries` : ''}</> },
                    { title: 'Status', dataIndex: 'status', render: (s: ElementStatus) => <StatusTag status={s} /> },
                    { title: 'Confidence', dataIndex: 'confidence', render: (c: number) => `${Math.round(c * 100)}%` },
                ]}
            />
            {data.bridges.length > 0 && (
                <>
                    <h2 style={{ fontSize: 16, marginTop: 16 }}>Bridge tables ({data.bridges.length})</h2>
                    <ul>
                        {data.bridges.map((b) => (
                            <li key={b.id}><Link to={assetHref(b.table)}>{b.table_name}</Link> joins {b.targets.map((t) => tableLabel(t.table)).join(' and ')}</li>
                        ))}
                    </ul>
                </>
            )}
        </div>
    );
}

/** Cairn Ontology's explorer (`/ontology`): the compiled ontology as a graph and as tables, the review queue, the
 * compile wizard, and exports, over what the signed-in person may see. */
export function OntologyPage() {
    const { client } = useConsumerApi();
    const api = useMemo(() => ontologyApi(client), [client]);
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const tab: Tab = (TABS as readonly string[]).includes(params.get('tab') ?? '') ? (params.get('tab') as Tab) : 'graph';
    const [data, setData] = useState<ExplorerData | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [exporting, setExporting] = useState<ExportFormat | null>(null);

    const load = useCallback(async () => {
        try {
            setData(await api.explorer());
            setError(null);
        } catch (e) {
            setError((e as Error).message);
        }
    }, [api]);
    useEffect(() => {
        void load();
    }, [load]);

    const exportAs = async (format: ExportFormat) => {
        setExporting(format);
        try {
            const out = await api.exportGraph(format);
            const [name, type] = EXT[format];
            download(name, type, typeof out === 'string' ? out : JSON.stringify(out, null, 2));
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setExporting(null);
        }
    };

    const pending = data
        ? [...data.terms, ...data.relationships, ...data.metrics].filter((x) => x.status === 'proposed').length
        : 0;
    return (
        <Page>
            <Head>
                <div>
                    <h1>Ontology</h1>
                    <p>
                        The business terms, relationships, metrics and bridge tables Cairn Ontology compiled from the catalog and the query log
                        {data?.version ? ` (version ${data.version})` : ''}. Only datasets you may see are shown.{' '}
                        <Link to="/semantic?tab=ontology-terms">Semantic models</Link>
                    </p>
                </div>
                <Space wrap>
                    <Muted>Download</Muted>
                    <Button size="small" loading={exporting === 'jsonld'} onClick={() => void exportAs('jsonld')}>JSON-LD</Button>
                    <Button size="small" loading={exporting === 'turtle'} onClick={() => void exportAs('turtle')}>Turtle (RDF)</Button>
                    <Button size="small" loading={exporting === 'csv'} onClick={() => void exportAs('csv')}>CSV</Button>
                </Space>
            </Head>
            {error && <Alert style={{ marginBottom: 8 }} type="warning" showIcon message="The ontology is not available" description={error} />}
            <Tabs
                activeKey={tab}
                onChange={(k) => history.replace({ pathname: location.pathname, search: `?tab=${k}` })}
                items={[
                    { key: 'graph', label: 'Graph', children: tab === 'graph' && (data ? (data.terms.length ? <GraphTab data={data} /> : <Empty description="No compiled terms you may see yet: compile the ontology first" />) : !error && <Spin />) },
                    { key: 'list', label: 'List', children: tab === 'list' && (data ? <ListTab data={data} /> : !error && <Spin />) },
                    { key: 'review', label: `Review${pending ? ` (${pending})` : ''}`, children: tab === 'review' && <ReviewQueue onChanged={() => void load()} /> },
                    { key: 'compile', label: 'Compile', children: tab === 'compile' && <CompileWizard onFinished={() => void load()} /> },
                ]}
            />
        </Page>
    );
}

/** One term's page (`/ontology/terms/:id`). */
export function OntologyTermPage() {
    const { id } = useParams<{ id: string }>();
    return (
        <Page>
            <p><Link to="/ontology">← Ontology</Link></p>
            <TermDetailView id={decodeURIComponent(id)} />
        </Page>
    );
}
