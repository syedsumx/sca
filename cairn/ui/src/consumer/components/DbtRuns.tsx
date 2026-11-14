import { Select, Space, Table, Tag } from 'antd';
import React, { useMemo, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DbtRun } from '../api/types';
import { ErrorNotice } from './ErrorNotice';
import { Muted, Surface } from './tokens';
import { StatusBadge } from './StatusBadge';
import { When } from '../utils/time';

export const DBT_STATUS_COLOR: Record<string, string> = {
    success: 'green',
    pass: 'green',
    error: 'red',
    fail: 'red',
    'runtime error': 'red',
    warn: 'gold',
    skipped: 'default',
};
function duration(s: number | null | undefined): string {
    if (s == null) return '';
    if (s < 60) return `${s}s`;
    return `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** dbt runs recorded by `cairn dbt`: what ran where, how it went, node by node, and the project's docs. */
export function DbtRuns() {
    const { client } = useConsumerApi();
    const [project, setProject] = useState<string | undefined>();
    const q = useDheQuery<DbtRun[]>(`/pipelines/dbt/runs?${project ?? ''}`, (c, signal) => c.dbtRuns(project, { signal }));
    const runs = q.data ?? [];
    const projects = useMemo(() => Array.from(new Set(runs.map((r) => r.project))).sort(), [runs]);

    return (
        <>
            <Muted>
                dbt runs recorded in Cairn, newest first. Each run&apos;s models, tests and lineage are on the datasets it
                built.
            </Muted>
            <Space style={{ margin: '16px 0' }} wrap>
                <Select
                    allowClear
                    placeholder="All projects"
                    style={{ minWidth: 220 }}
                    value={project}
                    onChange={(v) => setProject(v)}
                    options={projects.map((p) => ({ value: p, label: p }))}
                    aria-label="Project"
                />
                {project && (
                    <a href={client.href(`/pipelines/dbt/docs/${encodeURIComponent(project)}/`)} target="_blank" rel="noreferrer">
                        {project}: dbt docs
                    </a>
                )}
            </Space>
            {q.error && <ErrorNotice error={q.error} />}
            <Surface>
                <Table<DbtRun>
                    rowKey="invocation"
                    loading={q.loading}
                    dataSource={runs}
                    pagination={{ pageSize: 25 }}
                    expandable={{
                        expandedRowRender: (r) => (
                            <Table
                                size="small"
                                rowKey="node"
                                pagination={false}
                                dataSource={r.detail}
                                columns={[
                                    { title: 'Node', dataIndex: 'node' },
                                    {
                                        title: 'Result',
                                        dataIndex: 'status',
                                        render: (s: string) => <StatusBadge state={s} />,
                                    },
                                    { title: 'Seconds', dataIndex: 'seconds' },
                                ]}
                            />
                        ),
                    }}
                    columns={[
                        { title: 'Project', dataIndex: 'project' },
                        { title: 'Command', dataIndex: 'command' },
                        { title: 'Environment', dataIndex: 'environment' },
                        {
                            title: 'Status',
                            dataIndex: 'status',
                            render: (s: string, r) => (
                                <span>
                                    <StatusBadge state={s} />
                                    {r.tests_failed ? <Muted>{` · ${r.tests_failed} test(s) failed`}</Muted> : ''}
                                </span>
                            ),
                        },
                        { title: 'Nodes', dataIndex: 'nodes' },
                        { title: 'Took', dataIndex: 'elapsed_s', render: (s: number | null) => duration(s) },
                        { title: 'By', dataIndex: 'actor', render: (a: string) => a.replace('urn:li:corpuser:', '') },
                        {
                            title: 'Finished',
                            dataIndex: 'finished_ms',
                            render: (ms: number) => <When value={ms} />,
                        },
                    ]}
                />
            </Surface>
        </>
    );
}
