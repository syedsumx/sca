import { Alert, Button, Descriptions, Input, Popconfirm, Space, Tag, Typography, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { LicenceState } from '../api/types';

const KEY = '/admin/licence';

/** What the banner says, or nothing when the licence needs no attention. */
export function bannerFor(st: LicenceState | undefined): { type: 'warning' | 'error'; text: string } | null {
    if (!st) return null;
    switch (st.status) {
        case 'missing':
            return { type: 'error', text: 'Cairn has no licence: you can look around, but nothing can be changed.' };
        case 'invalid':
            return {
                type: 'error',
                text: 'The licence is not valid: Cairn is read-only until a valid one is installed.',
            };
        case 'expired':
            return { type: 'error', text: 'The licence has expired: Cairn is read-only until it is renewed.' };
        case 'expiring':
            return {
                type: 'warning',
                text: `The licence expired ${Math.abs(st.days_left ?? 0)} days ago. Renew it before Cairn turns read-only.`,
            };
        default:
            if (st.over_seats)
                return {
                    type: 'warning',
                    text: 'More people use Cairn than the licence allows: the newest can read but not change anything.',
                };
            if (st.status === 'valid' && st.days_left !== null && st.days_left < 21)
                return { type: 'warning', text: `The licence expires in ${st.days_left} days.` };
            return null;
    }
}

export function LicenceBanner() {
    const q = useDheQuery<LicenceState>(KEY, (c, signal) => c.getLicence({ signal }), { pollMs: 300_000 });
    const banner = bannerFor(q.data);
    if (!banner) return null;
    return <Alert banner role="status" type={banner.type} message={banner.text} />;
}

const STATUS_COLOR: Record<string, string> = {
    evaluation: 'blue',
    valid: 'green',
    expiring: 'orange',
    expired: 'red',
    invalid: 'red',
    missing: 'red',
};

/** Administration: the installed licence, seats, and installing or removing a key. */
export function LicenceSettings() {
    const { client } = useConsumerApi();
    const q = useDheQuery<LicenceState>(KEY, (c, signal) => c.getLicence({ signal }));
    const [key, setKey] = useState('');
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const st = q.data;

    const run = async (fn: () => Promise<LicenceState>, done: string) => {
        setBusy(true);
        setError(null);
        try {
            await fn();
            setKey('');
            message.success(done);
            q.refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const lic = st?.licence;
    return (
        <section aria-label="Licence" style={{ maxWidth: 760 }}>
            {st && (
                <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
                    <Descriptions.Item label="Status">
                        <Tag color={STATUS_COLOR[st.status]}>{st.status}</Tag>
                        {st.read_only && <Tag color="red">read-only</Tag>}
                    </Descriptions.Item>
                    <Descriptions.Item label="Seats">
                        {st.seats_used ?? 0}
                        {st.seats_limit ? ` of ${st.seats_limit}` : ''} active in 30 days
                    </Descriptions.Item>
                    {lic && (
                        <>
                            <Descriptions.Item label="Customer">{lic.customer}</Descriptions.Item>
                            <Descriptions.Item label="Edition">
                                {lic.edition}
                                {lic.development && <Tag style={{ marginLeft: 8 }}>development</Tag>}
                            </Descriptions.Item>
                            <Descriptions.Item label="Expires">
                                {lic.expires_at} ({st.days_left} days)
                            </Descriptions.Item>
                            <Descriptions.Item label="Features">
                                {lic.features.length ? lic.features.join(', ') : 'core catalog, governance and quality'}
                            </Descriptions.Item>
                        </>
                    )}
                    {st.problem && (
                        <Descriptions.Item label="Problem" span={2}>
                            {st.problem}
                        </Descriptions.Item>
                    )}
                </Descriptions>
            )}
            {st?.status === 'evaluation' && (
                <Typography.Paragraph type="secondary">
                    No licence is installed: Cairn runs in evaluation mode with every feature.
                </Typography.Paragraph>
            )}
            <Input.TextArea
                aria-label="Licence key"
                rows={4}
                placeholder="CAIRN1.…"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                style={{ fontFamily: 'monospace' }}
            />
            {error && <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} />}
            <Space style={{ marginTop: 8 }}>
                <Button
                    type="primary"
                    disabled={!key.trim()}
                    loading={busy}
                    onClick={() => run(() => client.installLicence(key.trim()), 'Licence installed')}
                >
                    Install licence
                </Button>
                {lic && (
                    <Popconfirm
                        title="Remove the licence? Seats are counted afresh from the next one."
                        onConfirm={() => run(() => client.removeLicence(), 'Licence removed')}
                    >
                        <Button danger>Remove</Button>
                    </Popconfirm>
                )}
            </Space>
        </section>
    );
}
