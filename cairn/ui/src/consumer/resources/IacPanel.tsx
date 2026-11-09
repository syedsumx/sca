import { Alert, Button, Input, Modal, Space, Table, Tabs, Tag, Typography } from 'antd';
import React, { useMemo, useState } from 'react';

import { useDheQuery } from '../api/hooks';
import { Muted } from '../components/tokens';
import { ChangesPanel } from './ChangesPanel';

/** One resource type of the registry (GET /v1/iac/schema). */
export interface IacKind {
    name: string;
    terraform_type: string;
    family: string;
    family_title: string;
    description: string;
    parents: string[];
    singleton: boolean;
    immutable: boolean;
    mine: boolean;
    docs: string;
    fields: { name: string; type: string; required: boolean; sensitive: boolean; write_only: boolean }[];
}

interface IacSchema {
    version: number;
    kinds: IacKind[];
}

/** The text of an export: Terraform (resource and import blocks) or a bundle (YAML), as the person asking. */
function ExportDialog({ title, query, onClose }: { title: string; query: Record<string, string>; onClose: () => void }) {
    const q = useDheQuery<string>(`iac-export:${JSON.stringify(query)}`, (c, signal) =>
        c.request<string>('GET', '/v1/iac/export', undefined, { query, signal }),
    );
    const [copied, setCopied] = useState(false);
    const text = typeof q.data === 'string' ? q.data : '';
    const copy = async () => {
        try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
        } catch {
            setCopied(false);
        }
    };
    return (
        <Modal open title={title} onCancel={onClose} width={760}
            footer={[
                <Button key="close" onClick={onClose}>Close</Button>,
                <Button key="copy" type="primary" disabled={!text} onClick={copy}>{copied ? 'Copied' : 'Copy'}</Button>,
            ]}>
            {q.error && <Alert type="error" showIcon message="Cairn refused" description={q.error.message} />}
            <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
                Secrets are variables here, never values: give them when you plan and apply.
            </Typography.Paragraph>
            <Input.TextArea aria-label="Exported code" value={q.loading && !text ? 'Exporting…' : text} readOnly
                autoSize={{ minRows: 8, maxRows: 24 }} style={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 12 }} />
        </Modal>
    );
}

/** "Copy as Terraform" and "Copy as YAML" for one resource (its registry type and Terraform id). */
export function CopyAsCode({ kind, id }: { kind: string; id: string }) {
    const [format, setFormat] = useState<'terraform' | 'yaml' | null>(null);
    return (
        <>
            <Space wrap>
                <Button onClick={() => setFormat('terraform')}>Copy as Terraform</Button>
                <Button onClick={() => setFormat('yaml')}>Copy as YAML</Button>
            </Space>
            {format && (
                <ExportDialog title={format === 'terraform' ? 'As Terraform' : 'As a bundle (YAML)'}
                    query={{ kind, id, format }} onClose={() => setFormat(null)} />
            )}
        </>
    );
}

/** Infrastructure as code: the resource types (and exports), and the change requests agents and people propose. */
export function IacPanel({ change }: { change?: string | null }) {
    return (
        <Tabs
            defaultActiveKey={change ? 'changes' : 'types'}
            items={[
                { key: 'types', label: 'Resource types', children: <IacTypes /> },
                { key: 'changes', label: 'Changes', children: <ChangesPanel initial={change} /> },
            ]}
        />
    );
}

/** Every resource type that can be kept as code, whether this workspace holds any, and an export of all of it. */
function IacTypes() {
    const schema = useDheQuery<IacSchema>('iac-schema', (c, signal) => c.request<IacSchema>('GET', '/v1/iac/schema', undefined, { signal }));
    const inventory = useDheQuery<{ kinds: Record<string, number | null> }>('iac-inventory', (c, signal) =>
        c.request('GET', '/v1/iac/inventory', undefined, { signal }),
    );
    const [all, setAll] = useState<'terraform' | 'yaml' | null>(null);
    const rows = useMemo(() => schema.data?.kinds ?? [], [schema.data]);
    const counts = inventory.data?.kinds ?? {};
    const docs = rows[0]?.docs ?? '/docs/administration.html#infrastructure-as-code';
    return (
        <section aria-label="Infrastructure as code">
            <Typography.Paragraph style={{ maxWidth: 820 }}>
                Every resource type below can be kept in git: as a bundle (<code>cairn bundle plan|apply</code>) or with
                Terraform (<code>cairn_&lt;type&gt;</code> resources and data sources). Export what this workspace holds
                to start from it; a plan of the export changes nothing. Counts are what you may read.{' '}
                <a href={docs} target="_blank" rel="noreferrer">How it works</a>
            </Typography.Paragraph>
            <Space style={{ marginBottom: 12 }} wrap>
                <Button onClick={() => setAll('terraform')}>Export as Terraform</Button>
                <Button onClick={() => setAll('yaml')}>Export as a bundle</Button>
            </Space>
            {schema.error && <Alert type="error" showIcon message="Could not load the registry" description={schema.error.message} />}
            <Table<IacKind>
                size="middle"
                loading={schema.loading && !schema.data}
                dataSource={rows}
                rowKey="name"
                pagination={rows.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
                scroll={{ x: 'max-content' }}
                columns={[
                    { title: 'Resource', dataIndex: 'terraform_type', key: 'tf', render: (v: string) => <code>{v}</code> },
                    { title: 'Bundle section', dataIndex: 'name', key: 'name', render: (v: string) => <code>{v}</code> },
                    { title: 'Area', dataIndex: 'family_title', key: 'family', render: (v: string) => <Tag>{v}</Tag> },
                    {
                        title: 'In this workspace',
                        key: 'count',
                        render: (_: unknown, k: IacKind) => {
                            if (!inventory.data) return <Muted>…</Muted>;
                            const n = counts[k.name];
                            if (n === null || n === undefined) return <Muted>off here</Muted>;
                            return n > 0 ? <Tag color="green">{n}</Tag> : <Muted>none</Muted>;
                        },
                    },
                    {
                        title: 'What it is',
                        dataIndex: 'description',
                        key: 'description',
                        render: (v: string, k: IacKind) => (
                            <span>
                                {v.split('. ')[0].replace(/\.$/, '')}.
                                {k.fields.some((f) => f.write_only) && <Tag style={{ marginLeft: 6 }}>write-only secret</Tag>}
                                {k.singleton && <Tag style={{ marginLeft: 6 }}>setting</Tag>}
                            </span>
                        ),
                    },
                    { title: '', key: 'docs', render: (_: unknown, k: IacKind) => <a href={k.docs} target="_blank" rel="noreferrer">Docs</a> },
                ]}
            />
            {all && (
                <ExportDialog title={all === 'terraform' ? 'This workspace as Terraform' : 'This workspace as a bundle'}
                    query={{ format: all }} onClose={() => setAll(null)} />
            )}
        </section>
    );
}
