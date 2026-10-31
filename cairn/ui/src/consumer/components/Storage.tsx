import {
    Alert,
    Button,
    Card,
    Checkbox,
    Form,
    Input,
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
import type { StoreAuditEntry, StoreAuditVerify, StoreBucket, StoreKey } from '../api/types';
import { useT } from '../i18n';

const BUCKETS = '/storage/buckets';
const AUDIT = '/storage/audit';
const KEYS = '/storage/keys';
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
            onClose(true); // unmounts this dialog
        } catch (e) {
            message.error((e as Error).message);
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

/** Shows a new or rotated key's secret, once. */
function SecretModal({ k, onClose }: { k: StoreKey; onClose: () => void }) {
    const t = useT();
    return (
        <Modal
            open
            title={t('storage.secretTitle', { key: k.access_key })}
            onOk={onClose}
            onCancel={onClose}
            okText={t('storage.close')}
            cancelButtonProps={{ style: { display: 'none' } }}
        >
            <Alert type="warning" showIcon message={t('storage.secretOnce')} style={{ marginBottom: 12 }} />
            <Typography.Paragraph copyable={{ text: k.secret_key }} code>
                {k.secret_key}
            </Typography.Paragraph>
        </Modal>
    );
}

function CreateKeyModal({ onClose }: { onClose: (created: StoreKey | null) => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [name, setName] = useState('');
    const [buckets, setBuckets] = useState('');
    const [description, setDescription] = useState('');
    const [readOnly, setReadOnly] = useState(false);
    const [admin, setAdmin] = useState(false);
    const [busy, setBusy] = useState(false);
    const create = async () => {
        setBusy(true);
        try {
            const k = await client.createStorageKey({
                access_key: name.trim() || undefined,
                buckets: buckets
                    .split(',')
                    .map((b) => b.trim())
                    .filter(Boolean),
                description: description.trim() || undefined,
                read_only: readOnly,
                admin,
            });
            onClose(k); // unmounts this dialog
        } catch (e) {
            message.error((e as Error).message);
            setBusy(false);
        }
    };
    return (
        <Modal
            open
            title={t('storage.createKey')}
            okText={t('storage.create')}
            okButtonProps={{ loading: busy }}
            onOk={create}
            onCancel={() => onClose(null)}
        >
            <Form layout="vertical">
                <Form.Item label={t('storage.keyName')}>
                    <Input aria-label={t('storage.keyName')} value={name} onChange={(e) => setName(e.target.value)} />
                </Form.Item>
                <Form.Item label={t('storage.description')}>
                    <Input
                        aria-label={t('storage.description')}
                        value={description}
                        onChange={(e) => setDescription(e.target.value)}
                    />
                </Form.Item>
                <Form.Item label={t('storage.bucketsField')}>
                    <Input
                        aria-label={t('storage.bucketsField')}
                        value={buckets}
                        onChange={(e) => setBuckets(e.target.value)}
                    />
                </Form.Item>
                <Space>
                    <Checkbox checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)}>
                        {t('storage.readOnly')}
                    </Checkbox>
                    <Checkbox checked={admin} onChange={(e) => setAdmin(e.target.checked)}>
                        {t('storage.admin')}
                    </Checkbox>
                </Space>
            </Form>
        </Modal>
    );
}

function AccessKeys() {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<StoreKey[]>(KEYS, (c, signal) => c.storageKeys({ signal }));
    const [creating, setCreating] = useState(false);
    const [secret, setSecret] = useState<StoreKey | null>(null);
    const rotate = async (k: StoreKey) => {
        try {
            setSecret(await client.rotateStorageKey(k.access_key));
            q.refresh();
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    const remove = async (k: StoreKey) => {
        try {
            await client.deleteStorageKey(k.access_key);
            message.success(t('storage.keyDeleted'));
            q.refresh();
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    return (
        <Card
            title={t('storage.keys')}
            extra={
                <Button type="primary" onClick={() => setCreating(true)}>
                    {t('storage.createKey')}
                </Button>
            }
        >
            <Typography.Paragraph type="secondary">{t('storage.keysIntro')}</Typography.Paragraph>
            {q.error && <Alert type="error" showIcon message={q.error.message} />}
            <Table<StoreKey>
                size="small"
                rowKey="access_key"
                loading={q.loading}
                dataSource={q.data ?? []}
                pagination={false}
                columns={[
                    {
                        title: t('storage.key'),
                        dataIndex: 'access_key',
                        render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
                    },
                    {
                        title: t('storage.source'),
                        dataIndex: 'source',
                        render: (v: string) => (
                            <Tag>{v === 'api' ? t('storage.sourceApi') : t('storage.sourceConfig')}</Tag>
                        ),
                    },
                    {
                        title: t('storage.scope'),
                        render: (_: unknown, k) => (
                            <Space size={4} wrap>
                                {k.admin && <Tag color="red">{t('storage.admin')}</Tag>}
                                {k.read_only && <Tag color="blue">{t('storage.readOnly')}</Tag>}
                                {k.buckets.length ? (
                                    k.buckets.map((b) => <Tag key={b}>{b}</Tag>)
                                ) : (
                                    <Tag>{t('storage.allBuckets')}</Tag>
                                )}
                                {k.policy && <Tag>{t('storage.policy')}</Tag>}
                            </Space>
                        ),
                    },
                    { title: t('storage.description'), dataIndex: 'description' },
                    {
                        title: t('storage.created'),
                        dataIndex: 'created',
                        render: (v?: string | null) => (v ? new Date(v).toLocaleString() : ''),
                    },
                    {
                        title: '',
                        render: (_: unknown, k) =>
                            k.source === 'api' && (
                                <Space>
                                    <Popconfirm title={t('storage.rotateConfirm')} onConfirm={() => rotate(k)}>
                                        <Button size="small">{t('storage.rotate')}</Button>
                                    </Popconfirm>
                                    <Popconfirm title={t('storage.deleteKeyConfirm')} onConfirm={() => remove(k)}>
                                        <Button size="small" danger>
                                            {t('storage.deleteKey')}
                                        </Button>
                                    </Popconfirm>
                                </Space>
                            ),
                    },
                ]}
            />
            {creating && (
                <CreateKeyModal
                    onClose={(k) => {
                        setCreating(false);
                        if (k) {
                            setSecret(k);
                            q.refresh();
                        }
                    }}
                />
            )}
            {secret && <SecretModal k={secret} onClose={() => setSecret(null)} />}
        </Card>
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
            <AccessKeys />
            <AuditTrail />
        </Space>
    );
}
