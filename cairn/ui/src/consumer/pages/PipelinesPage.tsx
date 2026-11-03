import { Alert, Select, Space, Table, Tabs, Tag } from 'antd';
import React, { useMemo, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DbtRun } from '../api/types';
import { Muted, Page, Surface } from '../components/tokens';
import { DerivedTab, MaintenanceTab, PipesTab, StreamsTab } from './LakehouseOps';

const STATUS_COLOR: Record<string, string> = {
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

/** Pipelines: dbt runs, pipes, streams, derived tables and table maintenance. */
export function PipelinesPage() {
    return (
        <Page>
            <h1 style={{ marginBottom: 0 }}>Pipelines</h1>
            <Tabs defaultActiveKey="dbt" destroyInactiveTabPane>
                <Tabs.TabPane tab="dbt runs" key="dbt"><DbtRuns /></Tabs.TabPane>
                <Tabs.TabPane tab="Pipes" key="pipes"><PipesTab /></Tabs.TabPane>
                <Tabs.TabPane tab="Streams" key="streams"><StreamsTab /></Tabs.TabPane>
                <Tabs.TabPane tab="Derived tables" key="derived"><DerivedTab /></Tabs.TabPane>
                <Tabs.TabPane tab="Maintenance" key="maintenance"><MaintenanceTab /></Tabs.TabPane>
            </Tabs>
        </Page>
    );
}

/** dbt runs recorded by `cairn dbt`: what ran where, how it went, node by node, and the project's docs. */
function DbtRuns() {
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
            {q.error && <Alert type="error" message={q.error.message} showIcon />}
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
                                        render: (s: string) => <Tag color={STATUS_COLOR[s] ?? 'default'}>{s}</Tag>,
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
                                <Tag color={STATUS_COLOR[s]}>
                                    {s}
                                    {r.tests_failed ? ` · ${r.tests_failed} test(s) failed` : ''}
                                </Tag>
                            ),
                        },
                        { title: 'Nodes', dataIndex: 'nodes' },
                        { title: 'Took', dataIndex: 'elapsed_s', render: (s: number | null) => duration(s) },
                        { title: 'By', dataIndex: 'actor', render: (a: string) => a.replace('urn:li:corpuser:', '') },
                        {
                            title: 'Finished',
                            dataIndex: 'finished_ms',
                            render: (ms: number) => new Date(ms).toLocaleString(),
                        },
                    ]}
                />
            </Surface>
        </>
    );
}
