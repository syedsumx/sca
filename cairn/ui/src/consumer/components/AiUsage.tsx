import { Alert, Table, Tag } from 'antd';
import React from 'react';

import { Link } from 'react-router-dom';

import { useDheQuery } from '../api/hooks';
import type { AIDatasetRead, AIReader } from '../api/types';
import { assetPath } from './Assets';
import { Muted } from './tokens';

const when = (ms: number) => new Date(ms).toLocaleString();
const CHANNEL = { mcp: 'MCP', assistant: 'Assistant', api: 'API key' } as const;
const datasetName = (urn: string) => urn.split(',')[1] ?? urn;

/** "Used by AI": the agents and AI tools that read this dataset in the last 30 days. */
export function UsedByAi({ urn }: { urn: string }) {
    const q = useDheQuery(`/ai/lineage/readers?urn=${urn}`, (c, signal) => c.aiReaders(urn, { signal }));
    if (q.error) return <Alert type="warning" showIcon message="AI usage is not available." />;
    return (
        <>
            <Muted>
                Agents, MCP sessions and the assistant that read this dataset in the last {q.data?.days ?? 30} days: its
                data through governed queries, or its metadata through tools.
            </Muted>
            <Table<AIReader>
                size="small"
                style={{ marginTop: 8 }}
                rowKey={(r) => `${r.agent ?? ''}|${r.actor ?? ''}|${r.channel}`}
                loading={q.loading}
                dataSource={q.data?.readers ?? []}
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                locale={{ emptyText: 'No AI has read this dataset lately.' }}
                columns={[
                    {
                        title: 'Used by',
                        render: (_, r) =>
                            r.agent ? (
                                <span>
                                    {r.agent_name ?? r.agent}
                                    {r.agent_owner && <Muted> · {r.agent_owner.split(':').pop()}</Muted>}
                                </span>
                            ) : (
                                <span>{r.actor ? r.actor.split(':').pop() : 'A person'}</span>
                            ),
                    },
                    { title: 'Through', render: (_, r) => <Tag>{CHANNEL[r.channel]}</Tag> },
                    {
                        title: 'AI system',
                        render: (_, r) =>
                            r.ai_system ? (
                                <span>
                                    {r.ai_system.name ?? r.ai_system.id}{' '}
                                    {r.ai_system.state && (
                                        <Tag color={r.ai_system.state === 'APPROVED' ? 'green' : 'orange'}>
                                            {r.ai_system.state.toLowerCase().replace('_', ' ')}
                                        </Tag>
                                    )}
                                </span>
                            ) : (
                                <Muted>not registered</Muted>
                            ),
                    },
                    { title: 'Data reads', dataIndex: 'data_reads' },
                    { title: 'Metadata reads', dataIndex: 'metadata_reads' },
                    { title: 'People', dataIndex: 'people' },
                    { title: 'Last', render: (_, r) => when(r.last_ms) },
                ]}
            />
        </>
    );
}

/** The datasets one agent read, most read first. */
export function DatasetsRead({ rows }: { rows: AIDatasetRead[] | null }) {
    return (
        <Table<AIDatasetRead>
            size="small"
            rowKey="dataset"
            loading={rows === null}
            dataSource={rows ?? []}
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            locale={{ emptyText: 'No datasets read yet.' }}
            columns={[
                {
                    title: 'Dataset',
                    render: (_, r) => <Link to={assetPath(r.dataset)}>{datasetName(r.dataset)}</Link>,
                },
                { title: 'Data reads', dataIndex: 'data_reads' },
                { title: 'Metadata reads', dataIndex: 'metadata_reads' },
                { title: 'Days used', dataIndex: 'days' },
                { title: 'Last', render: (_, r) => when(r.last_ms) },
            ]}
        />
    );
}
