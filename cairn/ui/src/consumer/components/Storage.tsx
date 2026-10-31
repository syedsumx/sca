import {
    Alert,
    Button,
    Card,
    Form,
    InputNumber,
    Modal,
    Popconfirm,
    Space,
    Table,
    Tag,
    Typography,
    message,
} from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { StoreAuditEntry, StoreAuditVerify, StoreBucket } from '../api/types';
import { useT } from '../i18n';

const BUCKETS = '/storage/buckets';
const AUDIT = '/storage/audit';
const GiB = 1024 ** 3;

/** Bytes as a short human-readable size. */
export function formatBytes(n: number): string {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

function QuotaModal({ bucket, onClose }: { bucket: StoreBucket; onClose: (changed: boolean) => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [gib, setGib] = useState<number | null>(bucket.quota?.max_bytes ? bucket.quota.max_bytes / GiB : null);
    const [objects, setObjects] = useState<number | null>(bucket.quota?.max_objects ?? null);
    const [busy, setBusy] = useState(false);
    const save = async () => {
        setBusy(true);
        try {
            await client.setStorageQuota(bucket.name, {
                max_bytes: gib == null ? undefined : Math.round(gib * GiB),
                max_objects: objects == null ? undefined : objects,
            });
            message.success(t('storage.quotaSaved'));
            onClose(true);
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal
            open
            title={t('storage.quotaFor', { bucket: bucket.name })}
            okText={t('storage.save')}
            okButtonProps={{
                disabled: gib == null && objects == null,
                loading: busy,
            }}
            onOk={save}
            onCancel={() => onClose(false)}
        >
            <Form layout="vertical">
                <Form.Item label={t('storage.maxSize')}>
                    <InputNumber
                        aria-label={t('storage.maxSize')}
                        min={0}
                        value={gib}
                        onChange={setGib}
                        addonAfter="GiB"
                    />
                </Form.Item>
                <Form.Item label={t('storage.maxObjects')}>
                    <InputNumber aria-label={t('storage.maxObjects')} min={0} value={objects} onChange={setObjects} />
                </Form.Item>
            </Form>
        </Modal>
    );
}

function AuditTrail() {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<StoreAuditEntry[]>(AUDIT, (c, signal) => c.storageAudit({ signal }));
    const [check, setCheck] = useState<StoreAuditVerify | null>(null);
    const [busy, setBusy] = useState(false);
    const verify = async () => {
        setBusy(true);
        try {
            setCheck(await client.verifyStorageAudit());
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    const rows = [...(q.data ?? [])].reverse().slice(0, 100);
    return (
        <Card
            title={t('storage.audit')}
            extra={
                <Button onClick={verify} loading={busy}>
                    {t('storage.verify')}
                </Button>
            }
        >
            {check && (
                <Alert
                    style={{ marginBottom: 12 }}
                    type={check.valid ? 'success' : 'error'}
                    showIcon
                    message={
                        check.valid
                            ? t('storage.chainValid', { n: String(check.entries_checked) })
                            : t('storage.chainBroken', { reason: check.reason ?? '' })
                    }
                    description={check.head ? <Typography.Text code>{check.head}</Typography.Text> : undefined}
                />
            )}
            <Table<StoreAuditEntry>
                size="small"
                rowKey="seq"
                loading={q.loading}
                dataSource={rows}
                pagination={{ pageSize: 20 }}
                columns={[
                    { title: '#', dataIndex: 'seq', width: 70 },
                    {
                        title: t('storage.when'),
                        dataIndex: 'ts',
                        render: (v: string) => new Date(v).toLocaleString(),
                    },
                    {
                        title: t('storage.action'),
                        dataIndex: 'action',
                        render: (v: string) => <Tag>{v}</Tag>,
                    },
                    { title: t('storage.actor'), dataIndex: 'actor' },
                    { title: t('storage.subject'), dataIndex: 'subject' },
                ]}
            />
        </Card>
    );
}

/** Administration of Cairn Store: buckets with their usage, settings and quotas, and the store's audit trail. */
export function StorageSettings() {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<StoreBucket[]>(BUCKETS, (c, signal) => c.storageBuckets({ signal }));
    const [editing, setEditing] = useState<StoreBucket | null>(null);
    const removeQuota = async (b: StoreBucket) => {
        try {
            await client.deleteStorageQuota(b.name);
            message.success(t('storage.quotaRemoved'));
            q.refresh();
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    const pct = (b: StoreBucket) => (b.quota?.max_bytes ? Math.round((100 * b.usage.bytes) / b.quota.max_bytes) : null);
    return (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
            <Typography.Paragraph type="secondary">{t('storage.intro')}</Typography.Paragraph>
            {q.error && <Alert type="error" showIcon message={q.error.message} />}
            <Table<StoreBucket>
                rowKey="name"
                loading={q.loading}
                dataSource={q.data ?? []}
                pagination={false}
                columns={[
                    {
                        title: t('storage.bucket'),
                        dataIndex: 'name',
                        render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
                    },
                    {
                        title: t('storage.usage'),
                        render: (_: unknown, b) => (
                            <span>
                                {formatBytes(b.usage.bytes)} ·{' '}
                                {t('storage.objects', { n: b.usage.objects.toLocaleString() })}
                            </span>
                        ),
                    },
                    {
                        title: t('storage.quota'),
                        render: (_: unknown, b) =>
                            b.quota ? (
                                <span>
                                    {b.quota.max_bytes != null && `${formatBytes(b.quota.max_bytes)} (${pct(b)}%)`}
                                    {b.quota.max_bytes != null && b.quota.max_objects != null && ' · '}
                                    {b.quota.max_objects != null &&
                                        t('storage.objects', {
                                            n: b.quota.max_objects.toLocaleString(),
                                        })}
                                </span>
                            ) : (
                                <Typography.Text type="secondary">{t('storage.noQuota')}</Typography.Text>
                            ),
                    },
                    {
                        title: t('storage.settings'),
                        render: (_: unknown, b) => (
                            <Space size={4} wrap>
                                {b.versioning !== 'Off' && (
                                    <Tag color="blue">{t('storage.versioning', { state: b.versioning })}</Tag>
                                )}
                                {b.object_lock && <Tag color="purple">{t('storage.objectLock')}</Tag>}
                                {b.encryption_default && <Tag color="green">{t('storage.encrypted')}</Tag>}
                                {b.lifecycle && <Tag>{t('storage.lifecycle')}</Tag>}
                                {b.policy && <Tag>{t('storage.policy')}</Tag>}
                            </Space>
                        ),
                    },
                    {
                        title: '',
                        render: (_: unknown, b) => (
                            <Space>
                                <Button size="small" onClick={() => setEditing(b)}>
                                    {t('storage.setQuota')}
                                </Button>
                                {b.quota && (
                                    <Popconfirm
                                        title={t('storage.removeQuotaConfirm')}
                                        onConfirm={() => removeQuota(b)}
                                    >
                                        <Button size="small" danger>
                                            {t('storage.removeQuota')}
                                        </Button>
                                    </Popconfirm>
                                )}
                            </Space>
                        ),
                    },
                ]}
            />
            {editing && (
                <QuotaModal
                    bucket={editing}
                    onClose={(changed) => {
                        setEditing(null);
                        if (changed) q.refresh();
                    }}
                />
            )}
            <AuditTrail />
        </Space>
    );
}
