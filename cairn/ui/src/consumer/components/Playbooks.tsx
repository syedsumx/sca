import { Alert, Button, Card, Empty, Input, Space, Switch, Table, Tag, Timeline } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { Playbook, PlaybookRun } from '../api/types';
import { Muted } from './tokens';

const TRIGGER: Record<string, string> = {
    'quality.incident.raised': 'when a quality incident is raised',
    'schema.changed': 'when a schema changes',
    'access.requested': 'when access is requested',
    'dataset.created': 'when a dataset appears',
};
const COLOR = { ok: 'green', skipped: 'gray', failed: 'red' } as const;
const STATE = { DONE: 'green', PARTIAL: 'orange', FAILED: 'red' } as const;

function Steps({ run }: { run: PlaybookRun }) {
    return (
        <Timeline>
            {run.steps.map((s) => (
                <Timeline.Item key={s.name} color={COLOR[s.status]}>
                    <b>{s.name}</b> <Tag color={COLOR[s.status]}>{s.status}</Tag>
                    <Muted>{s.summary}</Muted>
                </Timeline.Item>
            ))}
        </Timeline>
    );
}

function PlaybookCard({ p, onChange }: { p: Playbook; onChange: () => void }) {
    const { client } = useConsumerApi();
    const [urn, setUrn] = useState('');
    const [tried, setTried] = useState<PlaybookRun | null>(null);
    const [error, setError] = useState<string | null>(null);
    return (
        <Card
            size="small"
            title={p.name}
            extra={
                <Switch
                    aria-label={`Turn on ${p.name}`}
                    checked={p.enabled}
                    onChange={async (v) => {
                        await client.setPlaybookEnabled(p.id, v);
                        onChange();
                    }}
                />
            }
        >
            <p style={{ marginTop: 0 }}>{p.description}</p>
            <Muted>
                Runs {TRIGGER[p.trigger] ?? p.trigger}: {p.steps.join(' → ')}
            </Muted>
            <Space.Compact style={{ width: '100%', marginTop: 12 }}>
                <Input
                    aria-label={`Try ${p.name} on`}
                    placeholder="Try it on an asset: urn:li:…"
                    value={urn}
                    onChange={(e) => setUrn(e.target.value)}
                />
                <Button
                    disabled={!urn.startsWith('urn:li:')}
                    onClick={async () => {
                        setError(null);
                        try {
                            setTried(await client.runPlaybook(p.id, urn));
                            onChange();
                        } catch (e) {
                            setError(e instanceof Error ? e.message : String(e));
                        }
                    }}
                >
                    Run now
                </Button>
            </Space.Compact>
            {error && <Alert type="error" message={error} style={{ marginTop: 8 }} />}
            {tried && (
                <div style={{ marginTop: 12 }}>
                    <Steps run={tried} />
                </div>
            )}
        </Card>
    );
}

/** Governance studio → Playbooks: multi-step jobs Cairn runs on events; nothing applies without review. */
export function PlaybooksAdmin() {
    const list = useDheQuery<Playbook[]>('/governance/playbooks', (c, signal) => c.playbooks({ signal }));
    const runs = useDheQuery<PlaybookRun[]>('/governance/playbooks/runs', (c, signal) =>
        c.playbookRuns(undefined, { signal }),
    );
    const refresh = () => {
        list.refresh();
        runs.refresh();
    };
    const names = Object.fromEntries((list.data ?? []).map((p) => [p.id, p.name]));
    return (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
            <Muted>
                Playbooks read, notify, start discussions and queue suggestions for review. They never change metadata
                themselves, and what they post only names assets every recipient may see.
            </Muted>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
                {(list.data ?? []).map((p) => (
                    <PlaybookCard key={p.id} p={p} onChange={refresh} />
                ))}
            </div>
            <section aria-label="Playbook runs">
                <h4>Recent runs</h4>
                {(runs.data ?? []).length === 0 ? (
                    <Empty description="No runs yet." />
                ) : (
                    <Table
                        size="small"
                        rowKey="id"
                        dataSource={runs.data}
                        pagination={{ pageSize: 10, hideOnSinglePage: true }}
                        expandable={{ expandedRowRender: (r) => <Steps run={r} /> }}
                        columns={[
                            { title: 'Playbook', render: (_, r) => names[r.playbook] ?? r.playbook },
                            {
                                title: 'On',
                                render: (_, r) => (r.subject.includes(',') ? r.subject.split(',')[1] : r.subject),
                            },
                            {
                                title: 'State',
                                render: (_, r) => <Tag color={STATE[r.state]}>{r.state.toLowerCase()}</Tag>,
                            },
                            { title: 'When', render: (_, r) => new Date(r.created_ms).toLocaleString() },
                        ]}
                    />
                )}
            </section>
        </Space>
    );
}
