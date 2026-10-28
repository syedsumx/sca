import { Alert, Button, Empty, Popconfirm, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { TagSyncOperation, TagSyncResult, TagSyncTarget } from '../api/types';
import { datasetName } from './BulkEdit';
import { Muted } from './tokens';

const short = (urn: string) => urn.replace(/^urn:li:(tag|glossaryTerm):/, '');

function PlanTable({ ops }: { ops: TagSyncOperation[] }) {
    return (
        <Table<TagSyncOperation>
            size="small"
            rowKey={(o) => `${o.op}|${o.urn}|${o.field}|${o.tag}`}
            dataSource={ops}
            pagination={ops.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
            columns={[
                {
                    title: '',
                    dataIndex: 'op',
                    width: 70,
                    render: (op: string) => <Tag color={op === 'SET' ? 'green' : 'red'}>{op}</Tag>,
                },
                { title: 'Table', render: (_, o) => datasetName(o.urn) },
                { title: 'Column', dataIndex: 'field', render: (f: string | null) => f ?? <Muted>table</Muted> },
                { title: 'Tag', render: (_, o) => `${o.tag} = ${o.value}` },
                {
                    title: 'Statement',
                    dataIndex: 'sql',
                    render: (sql: string, o) => (
                        <>
                            <code style={{ fontSize: 11, wordBreak: 'break-all' }}>{sql}</code>
                            {o.error && <div style={{ color: '#cf1322' }}>{o.error}</div>}
                        </>
                    ),
                },
            ]}
        />
    );
}

function TargetCard({ target, onDone }: { target: TagSyncTarget; onDone: () => void }) {
    const { client } = useConsumerApi();
    const [plan, setPlan] = useState<TagSyncResult | null>(null);
    const [busy, setBusy] = useState(false);
    const run = async (dryRun: boolean) => {
        setBusy(true);
        try {
            const out = await client.tagSync(target.id, dryRun);
            setPlan(out);
            if (!dryRun) {
                if (out.failed) message.warning(`${out.applied} applied, ${out.failed} failed`);
                else message.success(`${out.applied} tag change(s) applied`);
                onDone();
            }
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    const last = target.last_run;
    return (
        <section aria-label={`Tag sync ${target.id}`} style={{ marginBottom: 24 }}>
            <Space style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap' }}>
                <div>
                    <strong>{target.id}</strong> <Tag>{target.dialect}</Tag>
                    {target.dry_run && <Tag color="gold">plan only</Tag>}
                    <Muted style={{ display: 'block' }}>
                        {target.platform} datasets · {target.managed_tags} tag(s) managed by Cairn
                        {last &&
                            ` · last run ${new Date(last.at_ms).toLocaleString()}: ${last.applied} applied, ${last.planned} planned` +
                                (last.failed ? `, ${last.failed} failed` : '')}
                    </Muted>
                </div>
                <Space>
                    <Button onClick={() => void run(true)} loading={busy}>
                        Preview
                    </Button>
                    {!target.dry_run && (
                        <Popconfirm
                            title="Run these statements in the warehouse?"
                            okText="Sync"
                            onConfirm={() => void run(false)}
                        >
                            <Button type="primary" loading={busy}>
                                Sync now
                            </Button>
                        </Popconfirm>
                    )}
                </Space>
            </Space>
            <Muted style={{ display: 'block', margin: '8px 0' }}>
                {target.mappings.map((m) => `${short(m.label)} → ${m.tag} = ${m.value}`).join(' · ')}
            </Muted>
            {plan && (
                <>
                    {plan.conflicts.length > 0 && (
                        <Alert
                            type="warning"
                            style={{ marginBottom: 8 }}
                            message="Mapping conflicts"
                            description={plan.conflicts.join('; ')}
                        />
                    )}
                    {plan.operations.length ? (
                        <PlanTable ops={plan.operations} />
                    ) : (
                        <Alert
                            type="success"
                            message={`In sync: ${plan.tagged_objects} tagged table(s) and column(s)`}
                        />
                    )}
                </>
            )}
        </section>
    );
}

/** Governance studio: catalog classifications pushed to warehouse tags, per target. */
export function TagSyncAdmin() {
    const { cache } = useConsumerApi();
    const targets = useDheQuery<TagSyncTarget[]>('/governance/tag-sync', (c, signal) => c.tagSyncTargets({ signal }));
    if (targets.error) return <Alert type="error" message={targets.error.message} />;
    if (!targets.data) return null;
    if (!targets.data.length)
        return (
            <Empty
                description={
                    <>
                        No warehouse targets yet. Add a file to <code>config/tag-sync/</code> (see the example there) to
                        push classifications to Snowflake or Databricks tags.
                    </>
                }
            />
        );
    return (
        <>
            {targets.data.map((t) => (
                <TargetCard key={t.id} target={t} onDone={() => cache.invalidate('/governance/tag-sync')} />
            ))}
        </>
    );
}
