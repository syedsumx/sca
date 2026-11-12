import { Alert, Button, Descriptions, Empty, Popconfirm, Space, Switch, Table, Tag, Typography, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { UpdatePlan, UpdatesOverview, UpdatesState } from '../api/types';
import { describeError } from './ErrorNotice';
import { StatusBadge } from './StatusBadge';

const SEVERITY_COLOR: Record<string, string> = { normal: 'default', security: 'orange', critical: 'red' };
const DAY: Record<string, string> = {
    mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun',
};

/** "Sat, Sun 02:00 for 4 h (Europe/Berlin)", or "any time". */
export function describeWindows(o: UpdatesOverview | undefined): string {
    const p = o?.policy;
    if (!p) return '';
    if (!p.windows.length) return 'any time';
    const w = p.windows.map((x) => `${x.days.map((d) => DAY[d] ?? d).join(', ')} ${x.start} for ${x.hours} h`);
    return `${w.join('; ')} (${p.timezone})`;
}

/** What an operator is asked to do now, if anything. */
export function nextAction(u: UpdatesState | undefined, o: UpdatesOverview | undefined): string | null {
    const open = o?.plans.find((p) => ['pending_approval', 'scheduled', 'running'].includes(p.status));
    if (open?.status === 'pending_approval') return `The update to ${open.target} is waiting for approval.`;
    if (open?.status === 'scheduled')
        return o?.updater
            ? `The update to ${open.target} starts in the next maintenance window${o.next_window ? ` (${o.next_window})` : ''}.`
            : `The update to ${open.target} is approved. This install has no updater: apply it with your deployment tooling.`;
    if (open?.status === 'running') return `The update to ${open.target} is running.`;
    if (u?.target) return `Cairn ${u.target} is available.`;
    return null;
}

/** Administration: the release this install runs, what is newer, the update policy and the plans. */
export function UpdatesSettings() {
    const { client } = useConsumerApi();
    const updates = useDheQuery<UpdatesState>('/v1/system/updates', (c, signal) => c.getUpdates({ signal }));
    const overview = useDheQuery<UpdatesOverview>('/v1/system/updates/plans', (c, signal) =>
        c.getUpdatePlans({ signal }));
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const u = updates.data;
    const o = overview.data;

    const run = async (key: string, fn: () => Promise<unknown>, done: string) => {
        setBusy(key);
        setError(null);
        try {
            await fn();
            message.success(done);
            updates.refresh();
            overview.refresh();
        } catch (e) {
            setError(describeError(e));
        } finally {
            setBusy(null);
        }
    };

    const open = o?.plans.find((p) => ['pending_approval', 'scheduled', 'running'].includes(p.status));
    const action = nextAction(u, o);
    return (
        <section aria-label="Updates" style={{ maxWidth: 920 }}>
            {action && <Alert type={u?.severity === 'critical' && !open ? 'error' : 'info'} showIcon message={action}
                style={{ marginBottom: 12 }} />}
            {u?.error && (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                    message="The release feed did not verify: no update is offered until it does."
                    description={u.error} />
            )}
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            {u && (
                <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
                    <Descriptions.Item label="Running">{u.current}</Descriptions.Item>
                    <Descriptions.Item label="Channel">{u.channel}</Descriptions.Item>
                    <Descriptions.Item label="Newest">
                        {u.target ?? 'up to date'}
                        {u.severity && u.target && (
                            <Tag color={SEVERITY_COLOR[u.severity]} style={{ marginLeft: 8 }}>{u.severity}</Tag>
                        )}
                    </Descriptions.Item>
                    <Descriptions.Item label="Way up">
                        {u.path.length ? `${u.current} → ${u.path.join(' → ')}` : '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label="Last checked">{u.checked_at ?? 'never'}</Descriptions.Item>
                    <Descriptions.Item label="Maintenance windows">{describeWindows(o)}</Descriptions.Item>
                </Descriptions>
            )}
            {u?.blocked && <Alert type="warning" showIcon message={u.blocked} style={{ marginBottom: 12 }} />}
            {!!u?.breaking.length && (
                <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="Breaking changes on the way up"
                    description={
                        <ul style={{ margin: 0, paddingLeft: 18 }}>
                            {u.breaking.map((b) => (
                                <li key={`${b.version}-${b.id}`}>
                                    <strong>{b.version}</strong>: {b.text}{b.action ? ` (${b.action})` : ''}
                                </li>
                            ))}
                        </ul>
                    } />
            )}
            <Space wrap style={{ marginBottom: 16 }}>
                <Button loading={busy === 'check'} onClick={() => run('check', () => client.checkUpdates(), 'Checked')}>
                    Check now
                </Button>
                <Button type="primary" disabled={!u?.target || !!open} loading={busy === 'plan'}
                    onClick={() => run('plan', () => client.planUpdate({}), `Update to ${u?.target} planned`)}>
                    Plan update to {u?.target ?? 'the newest'}
                </Button>
                {o && (
                    <Space>
                        <Switch aria-label="Automatic updates" checked={o.policy.mode === 'auto'} loading={busy === 'mode'}
                            onChange={(on) => run('mode', () => client.setUpdatePolicy({ mode: on ? 'auto' : 'manual' }),
                                on ? 'Automatic updates on' : 'Automatic updates off')} />
                        <Typography.Text>
                            Automatic for {o.policy.auto_severities.join(' and ') || 'nothing'}
                        </Typography.Text>
                    </Space>
                )}
            </Space>
            {u?.available.length ? (
                <Table<(typeof u.available)[number]> size="small" rowKey="version" pagination={false}
                    style={{ marginBottom: 16 }} dataSource={u.available}
                    columns={[
                        { title: 'Release', dataIndex: 'version' },
                        { title: 'Severity', dataIndex: 'severity',
                            render: (s: string) => <Tag color={SEVERITY_COLOR[s]}>{s}</Tag> },
                        { title: 'Summary', dataIndex: 'summary' },
                        { title: 'Notes', dataIndex: 'notes_url',
                            render: (url: string) => url ? <a href={url} target="_blank" rel="noreferrer">notes</a> : null },
                    ]} />
            ) : u && !u.error ? (
                <Empty description={`Cairn ${u.current} is the newest release on ${u.channel}.`} />
            ) : null}
            <Typography.Title level={5}>Updates planned</Typography.Title>
            <Table<UpdatePlan> size="small" rowKey="id" pagination={false} dataSource={o?.plans ?? []}
                expandable={{ expandedRowRender: (p) => (
                    <ul style={{ margin: 0 }}>
                        {p.log.map((l, i) => <li key={i}><code>{l.t}</code> {l.step}: {l.msg}</li>)}
                    </ul>
                ) }}
                columns={[
                    { title: 'To', dataIndex: 'target', render: (t: string, p) => `${p.from} → ${t}` },
                    { title: 'Status', dataIndex: 'status', render: (s: string) => <StatusBadge state={s} /> },
                    { title: 'Planned by', dataIndex: 'requested_by',
                        render: (r: string, p) => (p.auto ? 'automatic' : r.replace('urn:li:corpuser:', '')) },
                    { title: '', key: 'act', render: (_: unknown, p) => (
                        <Space>
                            {p.status === 'pending_approval' && (
                                <Button size="small" type="primary" loading={busy === `a-${p.id}`}
                                    onClick={() => run(`a-${p.id}`, () => client.approveUpdate(p.id), 'Approved')}>
                                    Approve
                                </Button>
                            )}
                            {['pending_approval', 'scheduled'].includes(p.status) && (
                                <Popconfirm title="Cancel this update?"
                                    onConfirm={() => run(`c-${p.id}`, () => client.cancelUpdate(p.id), 'Cancelled')}>
                                    <Button size="small" danger>Cancel</Button>
                                </Popconfirm>
                            )}
                            {p.error && <Typography.Text type="danger">{p.error}</Typography.Text>}
                        </Space>
                    ) },
                ]} />
        </section>
    );
}
