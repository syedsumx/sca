import { Alert, Button, Descriptions, Drawer, Form, Input, Modal, Popconfirm, Space, Table, Tag, Typography, message } from 'antd';
import React, { useCallback, useEffect, useState } from 'react';

/** One install as the fleet console lists it (cairn.fleet.server). */
export interface FleetInstall {
    id: string;
    name: string;
    customer: string;
    channel: string;
    licence_id: string | null;
    created_at: string;
    last_seen_at: string | null;
    version: string | null;
    attention: string[];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    last: any;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    history?: any[];
}

export interface FleetSummary {
    installs: number;
    healthy: number;
    attention: Record<string, number>;
    versions: Record<string, number>;
}

/** How the console reaches its API: the real service, or sample data in the browser. */
export interface FleetApi {
    summary(): Promise<FleetSummary>;
    installs(): Promise<FleetInstall[]>;
    install(id: string): Promise<FleetInstall>;
    register(body: { name: string; customer: string; channel?: string; licence_id?: string }): Promise<FleetInstall & { key: string }>;
    rotate(id: string): Promise<{ id: string; key: string }>;
    remove(id: string): Promise<void>;
}

export const ATTENTION: Record<string, { label: string; color: string; hint: string }> = {
    silent: { label: 'silent', color: 'red', hint: 'no check-in for longer than expected' },
    unhealthy: { label: 'unhealthy', color: 'red', hint: 'a component reports down or degraded' },
    update_failed: { label: 'update failed', color: 'volcano', hint: 'its last update was rolled back or failed' },
    licence_expired: { label: 'licence expired', color: 'volcano', hint: 'read-only until renewed' },
    security_update: { label: 'security update', color: 'orange', hint: 'a security release it has not taken' },
    over_seats: { label: 'over seats', color: 'gold', hint: 'more active people than the licence allows' },
    licence_expiring: { label: 'licence expiring', color: 'gold', hint: 'expires within 30 days' },
    update: { label: 'update', color: 'blue', hint: 'a newer release is offered' },
};

export function httpApi(base: string, token: string): FleetApi {
    const call = async <T,>(method: string, path: string, body?: unknown): Promise<T> => {
        const r = await fetch(`${base}${path}`, {
            method,
            headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
            body: body ? JSON.stringify(body) : undefined,
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `${r.status}`);
        return data as T;
    };
    return {
        summary: () => call('GET', '/fleet/v1/summary'),
        installs: async () => (await call<{ installs: FleetInstall[] }>('GET', '/fleet/v1/installs')).installs,
        install: (id) => call('GET', `/fleet/v1/installs/${encodeURIComponent(id)}`),
        register: (body) => call('POST', '/fleet/v1/installs', body),
        rotate: (id) => call('POST', `/fleet/v1/installs/${encodeURIComponent(id)}/rotate`),
        remove: async (id) => { await call('DELETE', `/fleet/v1/installs/${encodeURIComponent(id)}`); },
    };
}

function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
    return (
        <div style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 10, padding: '14px 18px', minWidth: 150 }}>
            <div style={{ fontSize: 12, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.4 }}>{label}</div>
            <div style={{ fontSize: 26, fontWeight: 600, color: tone ?? '#0f172a' }}>{value}</div>
        </div>
    );
}

/** The vendor's view of every customer install: what each runs and what needs attention, worst first. */
export function FleetConsole({ api, onSignOut }: { api: FleetApi; onSignOut?: () => void }) {
    const [installs, setInstalls] = useState<FleetInstall[] | null>(null);
    const [summary, setSummary] = useState<FleetSummary | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [open, setOpen] = useState<FleetInstall | null>(null);
    const [adding, setAdding] = useState(false);
    const [key, setKey] = useState<{ id: string; key: string } | null>(null);
    const [form] = Form.useForm();

    const load = useCallback(async () => {
        try {
            const [s, i] = await Promise.all([api.summary(), api.installs()]);
            setSummary(s);
            setInstalls(i);
            setError(null);
        } catch (e) {
            setError((e as Error).message);
        }
    }, [api]);
    useEffect(() => { void load(); }, [load]);

    const show = async (i: FleetInstall) => {
        try {
            setOpen(await api.install(i.id));
        } catch (e) {
            setError((e as Error).message);
        }
    };
    const worst = (summary && Object.entries(summary.attention)) || [];
    return (
        <section aria-label="Fleet" style={{ maxWidth: 1200, margin: '0 auto', padding: 24 }}>
            <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
                <div>
                    <Typography.Title level={3} style={{ margin: 0 }}>Fleet</Typography.Title>
                    <Typography.Text type="secondary">
                        Every customer install of Cairn, from what each one checks in: version, updates, licence, health.
                    </Typography.Text>
                </div>
                <Space>
                    <Button onClick={() => void load()}>Refresh</Button>
                    <Button type="primary" onClick={() => setAdding(true)}>Register an install</Button>
                    {onSignOut && <Button onClick={onSignOut}>Sign out</Button>}
                </Space>
            </Space>
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            {summary && (
                <Space wrap size={12} style={{ marginBottom: 16 }}>
                    <Stat label="Installs" value={summary.installs} />
                    <Stat label="Nothing to do" value={summary.healthy} tone="#15803d" />
                    {worst.map(([k, n]) => (
                        <Stat key={k} label={ATTENTION[k]?.label ?? k} value={n}
                            tone={['red', 'volcano'].includes(ATTENTION[k]?.color) ? '#b91c1c' : '#b45309'} />
                    ))}
                    <Stat label="Versions" value={<span style={{ fontSize: 15 }}>
                        {Object.entries(summary.versions).map(([v, n]) => `${v} × ${n}`).join(', ')}</span>} />
                </Space>
            )}
            <Table<FleetInstall> rowKey="id" loading={installs === null} dataSource={installs ?? []} pagination={false}
                onRow={(r) => ({ onClick: () => void show(r), style: { cursor: 'pointer' } })}
                columns={[
                    { title: 'Customer', dataIndex: 'customer' },
                    { title: 'Install', dataIndex: 'name' },
                    { title: 'Version', dataIndex: 'version', render: (v: string | null) => v ?? '—' },
                    { title: 'Channel', dataIndex: 'channel' },
                    { title: 'Last check-in', dataIndex: 'last_seen_at', render: (v: string | null) => v ?? 'never' },
                    { title: 'Needs attention', dataIndex: 'attention', render: (a: string[]) => a.length ? a.map((x) => (
                        <Tag key={x} color={ATTENTION[x]?.color} title={ATTENTION[x]?.hint}>{ATTENTION[x]?.label ?? x}</Tag>
                    )) : <Tag color="green">nothing</Tag> },
                ]} />
            <Drawer width={560} open={!!open} onClose={() => setOpen(null)} title={open ? `${open.customer} · ${open.name}` : ''}>
                {open && (
                    <>
                        <Descriptions size="small" column={1} bordered>
                            <Descriptions.Item label="Install id"><code>{open.id}</code></Descriptions.Item>
                            <Descriptions.Item label="Version">{open.version ?? '—'} ({open.channel})</Descriptions.Item>
                            <Descriptions.Item label="Last check-in">{open.last_seen_at ?? 'never'}</Descriptions.Item>
                            <Descriptions.Item label="Offered">
                                {open.last?.updates?.target ? `${open.last.updates.target} (${open.last.updates.severity})` : 'up to date'}
                            </Descriptions.Item>
                            <Descriptions.Item label="Last update">
                                {open.last?.updates?.last_plan?.status
                                    ? `${open.last.updates.last_plan.target}: ${open.last.updates.last_plan.status}` : '—'}
                            </Descriptions.Item>
                            <Descriptions.Item label="Licence">
                                {open.last?.licence?.status ?? '—'}
                                {open.last?.licence?.expires_at ? `, until ${open.last.licence.expires_at}` : ''}
                                {open.last?.licence?.seats_used !== undefined && open.last?.licence?.seats_used !== null
                                    ? `, ${open.last.licence.seats_used}${open.last.licence.seats_limit ? ` of ${open.last.licence.seats_limit}` : ''} seats` : ''}
                            </Descriptions.Item>
                            <Descriptions.Item label="Health">
                                {Object.entries(open.last?.health?.components ?? {}).map(([c, s]) => (
                                    <Tag key={c} color={s === 'ok' ? 'green' : 'red'}>{c}: {String(s)}</Tag>
                                ))}
                            </Descriptions.Item>
                        </Descriptions>
                        <Typography.Title level={5} style={{ marginTop: 16 }}>Check-ins</Typography.Title>
                        <ul style={{ paddingLeft: 18 }}>
                            {(open.history ?? []).slice(0, 20).map((h, n) => (
                                <li key={n}><code>{h.received_at}</code> {h.version}
                                    {h.updates?.target ? `, offered ${h.updates.target}` : ''}</li>
                            ))}
                        </ul>
                        <Space style={{ marginTop: 8 }}>
                            <Popconfirm title="Give this install a new check-in key? The current one stops working at once."
                                onConfirm={async () => { setKey(await api.rotate(open.id)); }}>
                                <Button>Rotate key</Button>
                            </Popconfirm>
                            <Popconfirm title="Remove this install and its check-ins?"
                                onConfirm={async () => { await api.remove(open.id); setOpen(null); message.success('Removed'); void load(); }}>
                                <Button danger>Remove</Button>
                            </Popconfirm>
                        </Space>
                    </>
                )}
            </Drawer>
            <Modal open={adding} title="Register an install" okText="Register" onCancel={() => setAdding(false)}
                onOk={async () => {
                    const v = await form.validateFields();
                    try {
                        const made = await api.register(v);
                        setAdding(false);
                        form.resetFields();
                        setKey({ id: made.id, key: made.key });
                        void load();
                    } catch (e) {
                        setError((e as Error).message);
                    }
                }}>
                <Form form={form} layout="vertical" initialValues={{ channel: 'stable' }}>
                    <Form.Item name="customer" label="Customer" rules={[{ required: true }]}><Input /></Form.Item>
                    <Form.Item name="name" label="Install name" rules={[{ required: true }]}><Input placeholder="acme-prod" /></Form.Item>
                    <Form.Item name="channel" label="Channel"><Input /></Form.Item>
                </Form>
            </Modal>
            <Modal open={!!key} title="The install's check-in key" footer={<Button onClick={() => setKey(null)}>Done</Button>}
                onCancel={() => setKey(null)}>
                <Typography.Paragraph>
                    Shown once. Give it to the customer to put in a Kubernetes Secret (Helm <code>cairn.fleet.keySecret</code>),
                    with install id <code>{key?.id}</code>.
                </Typography.Paragraph>
                <Input.TextArea aria-label="Check-in key" readOnly rows={2} value={key?.key} style={{ fontFamily: 'monospace' }} />
            </Modal>
        </section>
    );
}

/** The console's own sign-in: an operator token, kept for this tab only. */
export function SignIn({ onToken }: { onToken: (t: string) => void }) {
    const [t, setT] = useState('');
    return (
        <section aria-label="Sign in" style={{ maxWidth: 420, margin: '12vh auto', background: '#fff', padding: 28,
            border: '1px solid #e5e7eb', borderRadius: 12 }}>
            <Typography.Title level={4}>Cairn fleet console</Typography.Title>
            <Typography.Paragraph type="secondary">Sign in with your fleet operator token.</Typography.Paragraph>
            <Input.Password aria-label="Operator token" value={t} onChange={(e) => setT(e.target.value)} placeholder="cfo_…"
                onPressEnter={() => t.trim() && onToken(t.trim())} />
            <Button type="primary" block style={{ marginTop: 12 }} disabled={!t.trim()} onClick={() => onToken(t.trim())}>
                Sign in
            </Button>
        </section>
    );
}
