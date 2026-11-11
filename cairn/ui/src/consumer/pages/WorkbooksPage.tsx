import { Alert, Button, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Select, Space, Table, Tag, message } from 'antd';
import React, { useEffect, useMemo, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DataResult, SchemaTable, Workbook, WorkbookElement, WorkbookFilter } from '../api/types';
import { toCsv } from '../components/ResultChart';
import { BiAssetButtons } from '../components/BiAdmin';
import { Muted, Page, Surface } from '../components/tokens';
import { describeError, notifyError } from '../components/ErrorNotice';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

const OPS: { value: WorkbookFilter['op']; label: string; arity: 0 | 1 | 2 | 'list' }[] = [
    { value: '=', label: '=', arity: 1 }, { value: '!=', label: '≠', arity: 1 }, { value: '>', label: '>', arity: 1 },
    { value: '>=', label: '≥', arity: 1 }, { value: '<', label: '<', arity: 1 }, { value: '<=', label: '≤', arity: 1 },
    { value: 'in', label: 'is one of', arity: 'list' }, { value: 'not_in', label: 'is none of', arity: 'list' },
    { value: 'contains', label: 'contains', arity: 1 }, { value: 'starts_with', label: 'starts with', arity: 1 },
    { value: 'between', label: 'between', arity: 2 }, { value: 'is_null', label: 'is empty', arity: 0 },
    { value: 'not_null', label: 'is not empty', arity: 0 },
];

/** A typed value: numbers stay numbers, so filters compare like the spreadsheet user expects. */
const typed = (v: string): string | number => (v.trim() !== '' && !Number.isNaN(Number(v)) ? Number(v) : v);

type Named = { name: string; formula: string };

function NamedList({ label, items, onChange, placeholder, testid }: {
    label: string; items: Named[]; onChange: (v: Named[]) => void; placeholder: string; testid: string;
}) {
    return (
        <div data-testid={testid} style={{ marginBottom: 12 }}>
            <strong>{label}</strong>
            {items.map((c, i) => (
                <Space key={i} style={{ display: 'flex', marginTop: 6 }}>
                    <Input aria-label={`${label} name ${i + 1}`} value={c.name} style={{ width: 140 }} placeholder="Name"
                        onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                    <Input aria-label={`${label} formula ${i + 1}`} value={c.formula} style={{ width: 360, fontFamily: 'ui-monospace, Menlo, monospace' }}
                        placeholder={placeholder}
                        onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, formula: e.target.value } : x)))} />
                    <Button aria-label={`Remove ${label.toLowerCase()} ${i + 1}`} onClick={() => onChange(items.filter((_, j) => j !== i))}>×</Button>
                </Space>
            ))}
            <div style={{ marginTop: 6 }}>
                <Button size="small" onClick={() => onChange([...items, { name: '', formula: '' }])}>Add {label.toLowerCase().replace(/s$/, '')}</Button>
            </div>
        </div>
    );
}

/** Workbooks: explore a table like a spreadsheet. Formulas, filters, grouping and pivots are compiled to one SQL
 * query and run as you, so every mask and row filter that applies to you applies here too. */
export function WorkbooksPage() {
    const { client } = useConsumerApi();
    const conns = useDheQuery('/data/sources', (c, signal) => c.dataConnections({ signal }));
    const [connection, setConnection] = useState<string | undefined>();
    const [table, setTable] = useState<string | undefined>();
    const [columns, setColumns] = useState<Named[]>([]);
    const [filters, setFilters] = useState<WorkbookFilter[]>([]);
    const [groupBy, setGroupBy] = useState<string[]>([]);
    const [aggregates, setAggregates] = useState<Named[]>([]);
    const [pivot, setPivot] = useState<{ column?: string; aggregate: string }>({ aggregate: '' });
    const [sort, setSort] = useState<{ column?: string; desc: boolean }>({ desc: false });
    const [limit, setLimit] = useState(1000);
    const [result, setResult] = useState<(DataResult & { sql: string }) | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [running, setRunning] = useState(false);
    const [current, setCurrent] = useState<Workbook | null>(null);
    const [saving, setSaving] = useState(false);
    const [form, setForm] = useState({ name: '', shared: false });
    const [tick, setTick] = useState(0);

    useEffect(() => {
        if (!connection && conns.data?.length) setConnection(conns.data[0].connection);
    }, [conns.data, connection]);
    const schema = useDheQuery<SchemaTable[]>(`/sql/schema?${connection ?? ''}`, (c, signal) =>
        connection ? c.sqlSchema(connection, { signal }) : Promise.resolve([]));
    const saved = useDheQuery<Workbook[]>(`/sql/workbooks#${tick}`, (c, signal) => c.workbooks({ signal }));

    const tableColumns = useMemo(() => (schema.data ?? []).find((t) => t.name === table)?.columns.map((c) => c.name) ?? [], [schema.data, table]);
    const allColumns = [...tableColumns, ...columns.map((c) => c.name).filter(Boolean)];
    const groupNames = [...groupBy, ...aggregates.map((a) => a.name).filter(Boolean)];

    const element = (): WorkbookElement => ({
        source: { table: table ?? '' },
        columns: columns.filter((c) => c.name && c.formula),
        filters,
        group_by: groupBy,
        aggregates: aggregates.filter((a) => a.name && a.formula),
        pivot: pivot.column && pivot.aggregate ? { column: pivot.column, aggregate: pivot.aggregate } : null,
        sort: sort.column ? [{ column: sort.column, desc: sort.desc }] : [],
        limit,
    });

    const run = async () => {
        if (!connection || !table) return;
        setRunning(true);
        setError(null);
        try {
            setResult(await client.runWorkbook(element(), connection));
        } catch (e) {
            setResult(null);
            setError(describeError(e));
        } finally {
            setRunning(false);
        }
    };

    const open = (w: Workbook) => {
        const el = w.spec.elements[0];
        setCurrent(w);
        setForm({ name: w.name, shared: w.shared });
        setConnection(w.connection);
        setTable(el.source.table);
        setColumns(el.columns ?? []);
        setFilters(el.filters ?? []);
        setGroupBy(el.group_by ?? []);
        setAggregates(el.aggregates ?? []);
        setPivot(el.pivot ? { column: el.pivot.column, aggregate: el.pivot.aggregate } : { aggregate: '' });
        setSort(el.sort?.[0] ? { column: el.sort[0].column, desc: !!el.sort[0].desc } : { desc: false });
        setLimit(el.limit ?? 1000);
        setResult(null);
    };

    const save = async () => {
        if (!connection) return;
        try {
            const w = await client.saveWorkbook({ name: form.name, shared: form.shared, connection, spec: { elements: [element()] } }, current?.id);
            setCurrent(w);
            setSaving(false);
            setTick((t) => t + 1);
            message.success(form.shared ? 'Saved and shared' : 'Saved');
        } catch (e) {
            notifyError(e);
        }
    };

    const setFilter = (i: number, f: Partial<WorkbookFilter>) => setFilters(filters.map((x, j) => (j === i ? { ...x, ...f } : x)));

    return (
        <Page>
            <PageHeader title="Workbooks" help={pageHelp('/sql/workbooks')}
                description="Explore a table like a spreadsheet. It runs as you: your masks and row filters apply." />
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 1fr) 3fr', gap: 16, marginTop: 16 }}>
                <Surface>
                    <strong>Saved</strong>
                    {!(saved.data ?? []).length && <Empty description="No workbooks yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
                    {/* A compact row per workbook: its name opens it, the rest is in its menu (L150). */}
                    <ul style={{ listStyle: 'none', padding: 0, margin: '8px 0 0' }}>
                        {(saved.data ?? []).map((w) => (
                            <li key={w.id} style={{ display: 'flex', alignItems: 'center', gap: 6, minHeight: 36 }}>
                                <a onClick={() => open(w)} role="button" tabIndex={0} style={{ flex: 1, minWidth: 0, overflow: 'hidden',
                                    textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(w); } }}>{w.name}</a>
                                {w.shared && <Tag color="blue">shared</Tag>}
                                {w.role && w.role !== 'OWNER' && <Tag>{w.role.toLowerCase()}</Tag>}
                                <BiAssetButtons compact kind="workbook" id={w.id} name={w.name} role={w.role} onRestored={() => setTick((x) => x + 1)} />
                            </li>
                        ))}
                    </ul>
                </Surface>
                <div>
                    <Surface>
                        <Space wrap style={{ marginBottom: 12 }}>
                            <Select aria-label="Connection" style={{ width: 200 }} value={connection} onChange={(v) => { setConnection(v); setTable(undefined); }}
                                options={(conns.data ?? []).map((c) => ({ value: c.connection, label: c.connection }))} />
                            <Select aria-label="Source table" showSearch style={{ width: 280 }} placeholder="Table" value={table}
                                onChange={(v) => { setTable(v); setResult(null); }}
                                options={(schema.data ?? []).map((t) => ({ value: t.name, label: t.name }))} />
                        </Space>
                        <NamedList label="Columns" testid="calc-columns" items={columns} onChange={setColumns}
                            placeholder='Formula, e.g. [amount] * (1 - [discount])' />
                        <div style={{ marginBottom: 12 }}>
                            <strong>Filters</strong>
                            {filters.map((f, i) => {
                                const arity = OPS.find((o) => o.value === f.op)?.arity ?? 1;
                                return (
                                    <Space key={i} style={{ display: 'flex', marginTop: 6 }} wrap>
                                        <Select aria-label={`Filter column ${i + 1}`} style={{ width: 160 }} value={f.column}
                                            onChange={(column) => setFilter(i, { column })}
                                            options={(f.on === 'groups' ? groupNames : allColumns).map((c) => ({ value: c, label: c }))} />
                                        <Select aria-label={`Filter operator ${i + 1}`} style={{ width: 130 }} value={f.op}
                                            onChange={(op) => setFilter(i, { op, value: undefined, values: undefined })}
                                            options={OPS.map((o) => ({ value: o.value, label: o.label }))} />
                                        {arity === 1 && <Input aria-label={`Filter value ${i + 1}`} style={{ width: 160 }} value={String(f.value ?? '')}
                                            onChange={(e) => setFilter(i, { value: typed(e.target.value) })} />}
                                        {arity === 'list' && <Select mode="tags" aria-label={`Filter values ${i + 1}`} style={{ minWidth: 200 }}
                                            value={(f.values ?? []).map(String)} onChange={(v: string[]) => setFilter(i, { values: v.map(typed) })} />}
                                        {arity === 2 && <>
                                            <Input aria-label={`Filter low ${i + 1}`} style={{ width: 90 }} value={String(f.values?.[0] ?? '')}
                                                onChange={(e) => setFilter(i, { values: [typed(e.target.value), f.values?.[1] ?? null] })} />
                                            <Input aria-label={`Filter high ${i + 1}`} style={{ width: 90 }} value={String(f.values?.[1] ?? '')}
                                                onChange={(e) => setFilter(i, { values: [f.values?.[0] ?? null, typed(e.target.value)] })} />
                                        </>}
                                        <Checkbox checked={f.on === 'groups'} onChange={(e) => setFilter(i, { on: e.target.checked ? 'groups' : 'rows' })}>
                                            on groups
                                        </Checkbox>
                                        <Button aria-label={`Remove filter ${i + 1}`} onClick={() => setFilters(filters.filter((_, j) => j !== i))}>×</Button>
                                    </Space>
                                );
                            })}
                            <div style={{ marginTop: 6 }}>
                                <Button size="small" onClick={() => setFilters([...filters, { column: allColumns[0] ?? '', op: '=', on: 'rows' }])}>Add filter</Button>
                            </div>
                        </div>
                        <div style={{ marginBottom: 12 }}>
                            <strong>Group by</strong>{' '}
                            <Select mode="multiple" aria-label="Group by" style={{ minWidth: 280 }} value={groupBy} onChange={setGroupBy}
                                options={allColumns.map((c) => ({ value: c, label: c }))} />
                        </div>
                        <NamedList label="Aggregates" testid="aggregates" items={aggregates} onChange={setAggregates}
                            placeholder="e.g. Sum([Net]), Count(), Avg([amount])" />
                        <Space wrap style={{ marginBottom: 12 }}>
                            <strong>Pivot</strong>
                            <Select aria-label="Pivot column" allowClear style={{ width: 160 }} value={pivot.column} placeholder="Column"
                                onChange={(column) => setPivot({ ...pivot, column })} options={allColumns.map((c) => ({ value: c, label: c }))} />
                            <Input aria-label="Pivot aggregate" style={{ width: 240, fontFamily: 'ui-monospace, Menlo, monospace' }}
                                placeholder="e.g. Sum([amount])" value={pivot.aggregate} onChange={(e) => setPivot({ ...pivot, aggregate: e.target.value })} />
                        </Space>
                        <Space wrap>
                            <strong>Sort</strong>
                            <Select aria-label="Sort column" allowClear style={{ width: 160 }} value={sort.column}
                                onChange={(column) => setSort({ ...sort, column })}
                                options={(groupBy.length || aggregates.length ? groupNames : allColumns).map((c) => ({ value: c, label: c }))} />
                            <Checkbox checked={sort.desc} onChange={(e) => setSort({ ...sort, desc: e.target.checked })}>descending</Checkbox>
                            <span>Rows <InputNumber aria-label="Rows" min={1} max={10000} value={limit} onChange={(v) => setLimit(Number(v) || 1000)} /></span>
                            <Button type="primary" onClick={() => void run()} loading={running} disabled={!connection || !table}>Run</Button>
                            <Button onClick={() => setSaving(true)} disabled={!table}>{current ? 'Save changes' : 'Save'}</Button>
                            <Button disabled={!result} onClick={() => {
                                if (!result) return;
                                const a = document.createElement('a');
                                a.href = URL.createObjectURL(new Blob([toCsv(result.columns, result.rows)], { type: 'text/csv' }));
                                a.download = `${current?.name || 'workbook'}.csv`;
                                a.click();
                            }}>Download CSV</Button>
                        </Space>
                    </Surface>
                    {error && <Alert style={{ marginTop: 12 }} type="error" message={error} showIcon />}
                    {result && (
                        <Surface style={{ marginTop: 12 }}>
                            <Muted>
                                {result.rows.length} row(s){result.truncated ? ' (more not shown)' : ''} in {result.elapsed_ms} ms
                                {result.masked_columns.length ? ` · masked for you: ${result.masked_columns.join(', ')}` : ''}
                            </Muted>
                            <Table size="small" rowKey="__row" scroll={{ x: true }} pagination={{ pageSize: 50, hideOnSinglePage: true }}
                                dataSource={result.rows.map((r, n) => ({ __row: n, ...Object.fromEntries(result.columns.map((c, i) => [c, r[i]])) }))}
                                columns={result.columns.map((c) => ({
                                    title: c, dataIndex: c,
                                    render: (v: unknown) => (v === null ? <Muted>null</Muted> : String(v)),
                                }))} />
                            <Collapse style={{ marginTop: 8 }}>
                                <Collapse.Panel key="sql" header="SQL"><pre style={{ whiteSpace: 'pre-wrap' }}>{result.sql}</pre></Collapse.Panel>
                            </Collapse>
                        </Surface>
                    )}
                </div>
            </div>
            <Modal title="Save workbook" open={saving} onOk={() => void save()} onCancel={() => setSaving(false)} okText="Save">
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input aria-label="Workbook name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <Checkbox checked={form.shared} onChange={(e) => setForm({ ...form, shared: e.target.checked })}>
                        Share with everyone (each still sees only what they may)
                    </Checkbox>
                </Space>
            </Modal>
        </Page>
    );
}
