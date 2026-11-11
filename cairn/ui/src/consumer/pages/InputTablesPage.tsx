import { Alert, Button, Checkbox, Empty, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Tag, message } from 'antd';
import React, { useEffect, useMemo, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { InputColumnType, InputTable } from '../api/types';
import { Muted, Page, Surface } from '../components/tokens';
import { ErrorNotice, describeError } from '../components/ErrorNotice';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

const TYPES: InputColumnType[] = ['text', 'number', 'integer', 'boolean', 'date', 'timestamp'];
const SYSTEM = ['_row_id', '_updated_by', '_updated_at'];

type Row = Record<string, unknown> & { _row_id: string; _updated_at?: string };
type Draft = Record<string, unknown>;

/** One cell, edited in the type of its column (the server checks the type again). */
function Cell({ kind, value, onChange, label }: { kind: InputColumnType; value: unknown; onChange: (v: unknown) => void; label: string }) {
    if (kind === 'boolean') {
        return <Checkbox aria-label={label} checked={value === true || value === 'true'} onChange={(e) => onChange(e.target.checked)} />;
    }
    if (kind === 'number' || kind === 'integer') {
        return (
            <InputNumber aria-label={label} value={value === null || value === undefined || value === '' ? null : Number(value)}
                precision={kind === 'integer' ? 0 : undefined} style={{ width: 130 }}
                onChange={(v) => onChange(v === null || v === undefined ? null : v)} />
        );
    }
    if (kind === 'date') {
        return <Input type="date" aria-label={label} value={value ? String(value).slice(0, 10) : ''} style={{ width: 160 }}
            onChange={(e) => onChange(e.target.value || null)} />;
    }
    return <Input aria-label={label} value={value === null || value === undefined ? '' : String(value)}
        placeholder={kind === 'timestamp' ? '2026-11-04T10:00:00Z' : undefined} onChange={(e) => onChange(e.target.value)} />;
}

/** Input tables: lakehouse tables you type into. Every change runs on Trino as you, so you can only add or edit
 * rows where your policies let you write; what others see of the table is masked and filtered as for any table. */
export function InputTablesPage() {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const tables = useDheQuery<InputTable[]>(`/sql/input-tables#${tick}`, (c, signal) => c.inputTables({ signal }));
    const conns = useDheQuery('/data/sources', (c, signal) => c.dataConnections({ signal }));
    const [selected, setSelected] = useState<string | undefined>();
    const [rowsTick, setRowsTick] = useState(0);
    const [edits, setEdits] = useState<Record<string, Draft>>({});
    const [drafts, setDrafts] = useState<Draft[]>([]);
    const [checked, setChecked] = useState<string[]>([]);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [creating, setCreating] = useState(false);
    const [form, setForm] = useState<{ connection?: string; table: string; description: string; columns: { name: string; type: InputColumnType }[] }>(
        { table: '', description: '', columns: [{ name: '', type: 'text' }] });

    useEffect(() => {
        if (!selected && tables.data?.length) setSelected(tables.data[0].id);
    }, [tables.data, selected]);
    useEffect(() => {
        if (!form.connection && conns.data?.length) setForm((f) => ({ ...f, connection: conns.data![0].connection }));
    }, [conns.data, form.connection]);
    const rows = useDheQuery(`/sql/input-tables/${selected ?? ''}/rows#${rowsTick}`, (c, signal) =>
        selected ? c.inputRows(selected, { signal }) : Promise.resolve(null));
    const table = (tables.data ?? []).find((t) => t.id === selected);
    const data: Row[] = useMemo(() => {
        const r = rows.data;
        if (!r) return [];
        return r.rows.map((x) => Object.fromEntries(r.columns.map((c, i) => [c, x[i]])) as Row);
    }, [rows.data]);
    const masked = new Set(rows.data?.masked_columns ?? []);

    const reload = () => {
        setEdits({});
        setDrafts([]);
        setChecked([]);
        setRowsTick((t) => t + 1);
    };
    const act = async (fn: () => Promise<unknown>, done: string) => {
        setBusy(true);
        setError(null);
        try {
            await fn();
            message.success(done);
            reload();
        } catch (e) {
            setError(describeError(e));
        } finally {
            setBusy(false);
        }
    };

    const saveRow = (row: Row) => act(() => client.changeInputRow(table!.id, row._row_id, edits[row._row_id], row._updated_at), 'Row saved');
    const saveDrafts = () => act(() => client.addInputRows(table!.id, drafts), `${drafts.length} row${drafts.length === 1 ? '' : 's'} added`);
    const removeChecked = () => act(() => client.deleteInputRows(table!.id, checked), 'Rows deleted');

    const columns = table ? [
        ...table.columns.map((c) => ({
            title: <span>{c.name} <Muted>{c.type}</Muted>{masked.has(c.name) && <Tag color="orange" style={{ marginLeft: 4 }}>masked</Tag>}</span>,
            key: c.name,
            render: (_: unknown, row: Row) => {
                const value = c.name in (edits[row._row_id] ?? {}) ? edits[row._row_id][c.name] : row[c.name];
                return masked.has(c.name) ? String(row[c.name] ?? '') : (
                    <Cell kind={c.type} value={value} label={`${c.name} of row ${data.indexOf(row) + 1}`}
                        onChange={(v) => setEdits((e) => ({ ...e, [row._row_id]: { ...(e[row._row_id] ?? {}), [c.name]: v } }))} />
                );
            },
        })),
        { title: 'Changed by', key: '_updated_by', render: (_: unknown, row: Row) => <Muted>{String(row._updated_by ?? '').replace('urn:li:corpuser:', '')}</Muted> },
        {
            title: '', key: 'save',
            render: (_: unknown, row: Row) => edits[row._row_id] && (
                <Button size="small" type="primary" loading={busy} aria-label={`Save row ${data.indexOf(row) + 1}`} onClick={() => saveRow(row)}>Save</Button>
            ),
        },
    ] : [];

    const create = async () => {
        setBusy(true);
        setError(null);
        try {
            const t = await client.createInputTable({ connection: form.connection ?? '', table: form.table, description: form.description,
                columns: form.columns.filter((c) => c.name) });
            message.success(`${t.table_name} created`);
            setCreating(false);
            setSelected(t.id);
            setTick((x) => x + 1);
        } catch (e) {
            setError(describeError(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Page>
            <PageHeader title="Input tables" help={pageHelp('/sql/input-tables')}
                description="Tables you type into, in the lakehouse. Every change runs as you, under your policies, and is audited." />
            <Surface>
                <Space wrap style={{ marginBottom: 12 }}>
                    <Select aria-label="Input table" style={{ width: 280 }} value={selected} placeholder="Choose an input table"
                        options={(tables.data ?? []).map((t) => ({ value: t.id, label: t.table_name }))}
                        onChange={(v) => { setSelected(v); setEdits({}); setDrafts([]); setChecked([]); }} />
                    <Button onClick={() => { setError(null); setCreating(true); }}>New input table</Button>
                    {table && (
                        <Popconfirm title="Stop listing this as an input table? Its data stays in the lakehouse."
                            onConfirm={() => act(async () => { await client.forgetInputTable(table.id); setSelected(undefined); setTick((x) => x + 1); }, 'Removed')}>
                            <Button danger>Remove from input tables</Button>
                        </Popconfirm>
                    )}
                </Space>
                {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }}
                    action={/changed this row/.test(error) ? <Button size="small" onClick={reload}>Read again</Button> : undefined} />}
                {!table && !tables.loading && <Empty description="No input tables yet" />}
                {table && (
                    <>
                        {table.description && <p><Muted>{table.description}</Muted></p>}
                        {rows.error && <ErrorNotice error={rows.error} style={{ marginBottom: 12 }} />}
                        <Table<Row> rowKey="_row_id" size="small" pagination={{ pageSize: 50 }} dataSource={data} columns={columns}
                            loading={rows.loading} scroll={{ x: true }}
                            rowSelection={{ selectedRowKeys: checked, onChange: (keys) => setChecked(keys as string[]),
                                getCheckboxProps: (r: Row) => ({ 'aria-label': `Select row ${r._row_id}` } as never) }} />
                        {drafts.length > 0 && (
                            <div data-testid="new-rows" style={{ marginTop: 12 }}>
                                <strong>New rows</strong>
                                {drafts.map((d, i) => (
                                    <Space key={i} wrap style={{ display: 'flex', marginTop: 6 }}>
                                        {table.columns.map((c) => (
                                            <Cell key={c.name} kind={c.type} value={d[c.name]} label={`New ${c.name} ${i + 1}`}
                                                onChange={(v) => setDrafts(drafts.map((x, j) => (j === i ? { ...x, [c.name]: v } : x)))} />
                                        ))}
                                        <Button aria-label={`Remove new row ${i + 1}`} onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}>×</Button>
                                    </Space>
                                ))}
                            </div>
                        )}
                        <Space style={{ marginTop: 12 }}>
                            <Button onClick={() => setDrafts([...drafts, {}])}>Add row</Button>
                            {drafts.length > 0 && <Button type="primary" loading={busy} onClick={saveDrafts}>Save new rows</Button>}
                            {checked.length > 0 && (
                                <Popconfirm title={`Delete ${checked.length} row${checked.length === 1 ? '' : 's'}?`} onConfirm={removeChecked}>
                                    <Button danger>Delete selected</Button>
                                </Popconfirm>
                            )}
                            <Button onClick={reload}>Refresh</Button>
                        </Space>
                    </>
                )}
            </Surface>
            <Modal title="New input table" open={creating} onCancel={() => setCreating(false)} onOk={create} okText="Create"
                confirmLoading={busy} width={640}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Select aria-label="Connection" value={form.connection} style={{ width: '100%' }}
                        options={(conns.data ?? []).map((c) => ({ value: c.connection, label: c.connection }))}
                        onChange={(v) => setForm({ ...form, connection: v })} />
                    <Input aria-label="Table" placeholder="schema.table" value={form.table} onChange={(e) => setForm({ ...form, table: e.target.value })} />
                    <Input aria-label="Description" placeholder="What is typed in here" value={form.description}
                        onChange={(e) => setForm({ ...form, description: e.target.value })} />
                    <strong>Columns</strong>
                    {form.columns.map((c, i) => (
                        <Space key={i}>
                            <Input aria-label={`Column name ${i + 1}`} value={c.name} placeholder="name" style={{ width: 220 }}
                                onChange={(e) => setForm({ ...form, columns: form.columns.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)) })} />
                            <Select aria-label={`Column type ${i + 1}`} value={c.type} style={{ width: 140 }} options={TYPES.map((t) => ({ value: t, label: t }))}
                                onChange={(v) => setForm({ ...form, columns: form.columns.map((x, j) => (j === i ? { ...x, type: v } : x)) })} />
                            <Button aria-label={`Remove column ${i + 1}`} onClick={() => setForm({ ...form, columns: form.columns.filter((_, j) => j !== i) })}>×</Button>
                        </Space>
                    ))}
                    <Button size="small" onClick={() => setForm({ ...form, columns: [...form.columns, { name: '', type: 'text' }] })}>Add column</Button>
                    <Muted>Cairn adds {SYSTEM.join(', ')} to every input table.</Muted>
                    {error && <Alert type="error" showIcon message={error} />}
                </Space>
            </Modal>
        </Page>
    );
}
