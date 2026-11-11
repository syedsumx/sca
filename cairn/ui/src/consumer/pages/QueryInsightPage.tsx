import { Alert, Button, Checkbox, Descriptions, Drawer, Input, Select, Space, Table, Tag } from 'antd';
import React, { useState } from 'react';

import { useDheQuery } from '../api/hooks';
import type { QueryProfile, QueryRecord } from '../api/types';
import { Muted, Page, Surface } from '../components/tokens';
import { TitleRow } from '../guide/PageHeader';
import { ErrorNotice } from '../components/ErrorNotice';
import { StatusBadge } from '../components/StatusBadge';

export const seconds = (ms: number) => (ms >= 60_000 ? `${(ms / 60_000).toFixed(1)} min` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`);

export function bytes(n: number): string {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

/** One query: its statistics, the tables it read, its SQL and why it was slow. */
function Profile({ id }: { id: string }) {
    const q = useDheQuery<QueryProfile>(`/v1/queries/${id}`, (c, signal) => c.queryProfile(id, { signal }));
    if (q.error) return <ErrorNotice error={q.error} />;
    if (!q.data) return <Muted>Loading…</Muted>;
    const p = q.data;
    return (
        <>
            {p.hints.length ? (
                <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
                    {p.hints.map((h) => (
                        <Alert key={h.kind} type={h.kind === 'failed' ? 'error' : 'warning'} showIcon message={h.text} />
                    ))}
                </Space>
            ) : (
                <Alert type="success" showIcon style={{ marginBottom: 16 }} message="Nothing to tune: this query looks healthy." />
            )}
            <Descriptions size="small" column={2} bordered>
                <Descriptions.Item label="State">
                    <StatusBadge state={p.state} />
                </Descriptions.Item>
                <Descriptions.Item label="By">{p.user}</Descriptions.Item>
                <Descriptions.Item label="Elapsed">{seconds(p.wall_ms)}</Descriptions.Item>
                <Descriptions.Item label="Queued">{seconds(p.queued_ms)}</Descriptions.Item>
                <Descriptions.Item label="CPU">{seconds(p.cpu_ms)}</Descriptions.Item>
                <Descriptions.Item label="Peak memory">{bytes(p.peak_memory_bytes)}</Descriptions.Item>
                <Descriptions.Item label="Read">
                    {bytes(p.input_bytes)} ({p.input_rows.toLocaleString()} rows)
                </Descriptions.Item>
                <Descriptions.Item label="Returned">{p.output_rows.toLocaleString()} rows</Descriptions.Item>
                <Descriptions.Item label="Source">{p.source || '—'}</Descriptions.Item>
                <Descriptions.Item label="Splits">{p.splits}</Descriptions.Item>
            </Descriptions>
            {p.error && <Alert style={{ marginTop: 16 }} type="error" message={p.error_code ?? 'Error'} description={p.error} />}
            <h3 style={{ marginTop: 16 }}>Tables</h3>
            {p.tables.length ? p.tables.map((t) => <Tag key={t}>{t}</Tag>) : <Muted>None recorded.</Muted>}
            <h3 style={{ marginTop: 16 }}>SQL</h3>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, background: 'rgba(127,127,127,0.08)', padding: 12 }}>{p.sql}</pre>
        </>
    );
}

/** Query insight: your queries on the lakehouse (everyone's for operators), the slowest first if you like, and
 * a profile of each with tuning hints. */
export function QueryInsightPage() {
    const [state, setState] = useState<string | undefined>();
    const [text, setText] = useState('');
    const [user, setUser] = useState('');
    const [slowest, setSlowest] = useState(false);
    const [filters, setFilters] = useState({ text: '', user: '' });
    const [open, setOpen] = useState<string | null>(null);
    const key = `/v1/queries?${state ?? ''}&${filters.text}&${filters.user}&${slowest}`;
    const list = useDheQuery<{ queries: QueryRecord[] }>(key, (c, signal) =>
        c.queries({ state, text: filters.text, user: filters.user, slowest, limit: 200 }, { signal }),
    );

    return (
        <Page>
            <TitleRow title="Query insight" />
            <Muted>Your queries on the lakehouse, with what they cost and how to make them faster. Operators see everyone's.</Muted>
            <Surface style={{ marginTop: 16 }}>
                <Space wrap style={{ marginBottom: 12 }}>
                    <Input.Search
                        aria-label="Search SQL"
                        placeholder="SQL contains…"
                        allowClear
                        value={text}
                        onChange={(e) => setText(e.target.value)}
                        onSearch={(v) => setFilters((f) => ({ ...f, text: v }))}
                        style={{ width: 260 }}
                    />
                    <Input.Search
                        aria-label="User"
                        placeholder="User (operators)"
                        allowClear
                        value={user}
                        onChange={(e) => setUser(e.target.value)}
                        onSearch={(v) => setFilters((f) => ({ ...f, user: v }))}
                        style={{ width: 200 }}
                    />
                    <Select
                        aria-label="State"
                        allowClear
                        placeholder="Any state"
                        style={{ width: 140 }}
                        value={state}
                        onChange={setState}
                        options={['FINISHED', 'FAILED'].map((s) => ({ value: s, label: s }))}
                    />
                    <Checkbox checked={slowest} onChange={(e) => setSlowest(e.target.checked)}>
                        Slowest first
                    </Checkbox>
                    <Button onClick={() => void list.refresh()}>Refresh</Button>
                </Space>
                {list.error && <ErrorNotice error={list.error} />}
                <Table<QueryRecord>
                    rowKey="query_id"
                    size="small"
                    loading={list.loading}
                    dataSource={list.data?.queries ?? []}
                    locale={{ emptyText: 'No queries yet.' }}
                    onRow={(r) => ({ onClick: () => setOpen(r.query_id), style: { cursor: 'pointer' } })}
                    columns={[
                        { title: 'When', dataIndex: 'create_ms', render: (v: number) => new Date(v).toLocaleString(), width: 170 },
                        {
                            title: 'State',
                            dataIndex: 'state',
                            width: 100,
                            render: (s: string) => <StatusBadge state={s} />,
                        },
                        { title: 'User', dataIndex: 'user', width: 140 },
                        {
                            title: 'SQL',
                            dataIndex: 'sql',
                            ellipsis: true,
                            render: (s: string) => <code style={{ fontSize: 12 }}>{s}</code>,
                        },
                        { title: 'Elapsed', dataIndex: 'wall_ms', width: 90, render: seconds },
                        { title: 'Read', dataIndex: 'input_bytes', width: 90, render: bytes },
                        { title: 'Rows', dataIndex: 'output_rows', width: 90, render: (n: number) => n.toLocaleString() },
                    ]}
                />
            </Surface>
            <Drawer title="Query profile" width={720} open={!!open} onClose={() => setOpen(null)} destroyOnClose>
                {open && <Profile id={open} />}
            </Drawer>
        </Page>
    );
}
