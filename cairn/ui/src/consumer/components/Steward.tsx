import { Alert, Button, Empty, Input, Progress, Radio, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { StewardProposal } from '../api/types';
import { actorName } from '../utils/format';
import { assetPath } from './Assets';
import { Muted } from './tokens';

const KIND = { owner: 'Owner', term: 'Classification', description: 'Description' } as const;

function ProposedValue({ p, edit, onEdit }: { p: StewardProposal; edit?: unknown; onEdit: (v: unknown) => void }) {
    if (p.kind === 'description') {
        const text = (edit as string | undefined) ?? (p.value as { description: string }).description;
        return (
            <Input.TextArea
                aria-label={`Description for ${p.name}`}
                autoSize={{ minRows: 2, maxRows: 6 }}
                value={text}
                onChange={(e) => onEdit(e.target.value)}
            />
        );
    }
    if (p.kind === 'owner') {
        const v = (edit as string | undefined) ?? (p.value as string);
        return (
            <Input
                aria-label={`Owner for ${p.name}`}
                value={v}
                onChange={(e) => onEdit(e.target.value)}
                addonBefore={actorName(p.value as string)}
            />
        );
    }
    return <Tag color="purple">{(p.value as string).replace('urn:li:glossaryTerm:', '')}</Tag>;
}

/** Governance studio: review what the AI steward proposes, many at a time. */
export function StewardReview() {
    const { client, cache } = useConsumerApi();
    const [kind, setKind] = useState<string>('all');
    const key = `/ai/steward/proposals?${kind}`;
    const q = useDheQuery<StewardProposal[]>(key, (c, signal) =>
        c.stewardProposals(kind === 'all' ? undefined : kind, { signal }),
    );
    const [selected, setSelected] = useState<React.Key[]>([]);
    const [edits, setEdits] = useState<Record<string, unknown>>({});
    const [busy, setBusy] = useState(false);

    const refresh = () => {
        setSelected([]);
        setEdits({});
        cache.invalidate('/ai/steward/proposals');
    };
    const act = async (fn: () => Promise<string>) => {
        setBusy(true);
        try {
            message.success(await fn());
            refresh();
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    const run = () =>
        act(async () => {
            const r = await client.runSteward();
            return `${r.proposed} new proposal(s) for ${r.examined} dataset(s)${r.auto_accepted ? `; ${r.auto_accepted} applied automatically` : ''}`;
        });
    const decide = (action: 'accept' | 'reject') =>
        act(async () => {
            const ids = selected.map(String);
            const pick = Object.fromEntries(Object.entries(edits).filter(([id]) => ids.includes(id)));
            const r = await client.decideProposals(ids, action, pick);
            return `${r.decided} proposal(s) ${action === 'accept' ? 'accepted' : 'rejected'}`;
        });

    return (
        <>
            <Space style={{ marginBottom: 12, flexWrap: 'wrap' }}>
                <Button onClick={() => void run()} loading={busy}>
                    Find gaps now
                </Button>
                <Radio.Group
                    aria-label="Proposal kind"
                    optionType="button"
                    value={kind}
                    onChange={(e) => {
                        setKind(e.target.value);
                        setSelected([]);
                    }}
                    options={[
                        { label: 'All', value: 'all' },
                        ...Object.entries(KIND).map(([value, label]) => ({ value, label })),
                    ]}
                />
                <Button type="primary" disabled={!selected.length} loading={busy} onClick={() => void decide('accept')}>
                    Accept {selected.length || ''}
                </Button>
                <Button danger disabled={!selected.length} loading={busy} onClick={() => void decide('reject')}>
                    Reject {selected.length || ''}
                </Button>
                <Muted>
                    Datasets without a description, owner or classification, most used first. Edit before accepting.
                </Muted>
            </Space>
            {q.error && <Alert type="error" message={q.error.message} />}
            {q.data && !q.data.length && (
                <Empty description="Nothing to review. Find gaps to ask the steward for proposals." />
            )}
            {q.data && q.data.length > 0 && (
                <Table<StewardProposal>
                    size="small"
                    rowKey="id"
                    dataSource={q.data}
                    rowSelection={{ selectedRowKeys: selected, onChange: setSelected }}
                    pagination={{ pageSize: 25, showSizeChanger: false, hideOnSinglePage: true }}
                    columns={[
                        { title: 'Dataset', render: (_, p) => <Link to={assetPath(p.urn)}>{p.name}</Link> },
                        { title: 'Column', dataIndex: 'field', render: (f: string) => f || <Muted>—</Muted> },
                        { title: 'Proposal', render: (_, p) => <Tag>{KIND[p.kind]}</Tag> },
                        {
                            title: 'Value',
                            width: 360,
                            render: (_, p) => (
                                <ProposedValue
                                    p={p}
                                    edit={edits[p.id]}
                                    onEdit={(v) => setEdits({ ...edits, [p.id]: v })}
                                />
                            ),
                        },
                        {
                            title: 'Confidence',
                            width: 120,
                            render: (_, p) => (
                                <Progress
                                    percent={Math.round(p.confidence * 100)}
                                    size="small"
                                    format={(v) => `${v}%`}
                                />
                            ),
                        },
                        {
                            title: 'Why',
                            render: (_, p) => (
                                <ul style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
                                    {p.evidence.map((e) => (
                                        <li key={e}>{e}</li>
                                    ))}
                                </ul>
                            ),
                        },
                    ]}
                />
            )}
        </>
    );
}
