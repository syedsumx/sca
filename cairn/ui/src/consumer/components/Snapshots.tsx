import { Alert, Button, Empty, Input, Modal, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { SnapshotRows, TableSnapshot } from '../api/types';
import { Muted } from './tokens';

/** A lakehouse table's snapshots: look at the table as it was, and restore an earlier snapshot (L59).
 * Everything runs on Trino as the person: they see their own masks and row filters, and a restore needs
 * MANAGE_DATASET on the table. */
export function TableSnapshots({ table }: { table: string }) {
    const { client } = useConsumerApi();
    const q = useDheQuery<TableSnapshot[]>(`snapshots:${table}`, (c, signal) => c.tableSnapshots(table, { signal }), {
        staleMs: 10_000,
    });
    const [viewing, setViewing] = useState<{ snap: TableSnapshot; rows?: SnapshotRows; error?: string } | null>(null);
    const [restoring, setRestoring] = useState<TableSnapshot | null>(null);
    const [reason, setReason] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const view = async (snap: TableSnapshot) => {
        setViewing({ snap });
        try {
            setViewing({ snap, rows: await client.snapshotRows(table, snap.id) });
        } catch (e) {
            setViewing({ snap, error: (e as Error).message });
        }
    };
    const restore = async () => {
        if (!restoring) return;
        setBusy(true);
        setError(null);
        try {
            const r = await client.restoreSnapshot(table, restoring.id, reason.trim());
            message.success(`Restored ${table} to snapshot ${r.to_id} (${r.records} rows)`);
            setRestoring(null);
            setReason('');
            await q.refresh();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    if (q.error) return <Alert type="error" showIcon message="Could not list the table's snapshots" description={q.error.message} />;
    const snaps = [...(q.data ?? [])].reverse();          // newest first
    if (!q.loading && !snaps.length) return <Empty description="No snapshots yet" />;
    return (
        <>
            <Table<TableSnapshot>
                size="small"
                loading={q.loading && !q.data}
                rowKey="id"
                dataSource={snaps}
                pagination={{ pageSize: 20, hideOnSinglePage: true }}
                columns={[
                    { title: 'Committed', dataIndex: 'committed_at', render: (v: string) => new Date(v).toLocaleString() },
                    { title: 'Operation', dataIndex: 'operation' },
                    { title: 'Rows', dataIndex: 'records', render: (v: number | null) => (v === null ? <Muted>?</Muted> : v) },
                    { title: 'Added', dataIndex: 'added' },
                    { title: 'Deleted', dataIndex: 'deleted' },
                    { title: 'Snapshot', dataIndex: 'id', render: (v: string, s) => (
                        <Space>
                            <Muted style={{ fontFamily: 'monospace', fontSize: 12 }}>{v}</Muted>
                            {s.current && <Tag color="green">current</Tag>}
                        </Space>
                    ) },
                    { title: '', key: 'act', render: (_: unknown, s) => (
                        <Space>
                            <Button size="small" onClick={() => view(s)}>View rows</Button>
                            {!s.current && (
                                <Button size="small" onClick={() => { setRestoring(s); setError(null); }}>Restore</Button>
                            )}
                        </Space>
                    ) },
                ]}
            />
            <Modal
                open={!!viewing}
                title={viewing ? `${table} at ${new Date(viewing.snap.committed_at).toLocaleString()}` : ''}
                onCancel={() => setViewing(null)}
                footer={null}
                width={900}
            >
                {viewing?.error && <Alert type="error" showIcon message={viewing.error} />}
                {viewing?.rows && (
                    <>
                        <Table
                            size="small"
                            scroll={{ x: true }}
                            rowKey={(_, i) => String(i)}
                            pagination={false}
                            dataSource={viewing.rows.rows.map((r) => Object.fromEntries(r.map((v, i) => [String(i), v])))}
                            columns={viewing.rows.columns.map((c, i) => ({
                                title: c, dataIndex: String(i),
                                render: (v: unknown) => (v === null ? <Muted>null</Muted> : String(v)),
                            }))}
                        />
                        <Muted style={{ fontSize: 12 }}>
                            {viewing.rows.rows.length} row(s){viewing.rows.truncated ? ' (more not shown)' : ''}, as you see them
                        </Muted>
                    </>
                )}
            </Modal>
            <Modal
                open={!!restoring}
                title={`Restore ${table}`}
                okText="Restore"
                okButtonProps={{ disabled: !reason.trim(), loading: busy, danger: true }}
                onOk={restore}
                onCancel={() => setRestoring(null)}
            >
                {restoring && (
                    <p>
                        The table goes back to how it was at {new Date(restoring.committed_at).toLocaleString()}
                        {restoring.records !== null ? ` (${restoring.records} rows)` : ''}. Later snapshots stay in the history, so
                        this can be undone by restoring the current one.
                    </p>
                )}
                <Input.TextArea
                    aria-label="Reason"
                    placeholder="Why (kept in the audit log)"
                    value={reason}
                    maxLength={500}
                    onChange={(e) => setReason(e.target.value)}
                />
                {error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} />}
            </Modal>
        </>
    );
}
