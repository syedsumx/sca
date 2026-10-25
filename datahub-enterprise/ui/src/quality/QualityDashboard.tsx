import React, { useMemo, useState } from 'react';
import { Button, Card, Col, Input, Popconfirm, Row, Select, Space, Statistic, Table, Tag, Tooltip, message } from 'antd';

import { useDheClient, usePolling } from '../api/hooks';
import type { EvaluationStatus, Monitor, MonitorType } from '../api/types';
import { datasetNameFromUrn, formatRelative, statusColor } from '../common/format';
import { EmptyState, ErrorState, LoadingState, PageContainer, PageHeader } from '../common/States';
import { MonitorDetail } from './MonitorDetail';
import { MONITOR_TYPE_LABELS, MonitorEditor } from './MonitorEditor';

export interface QualityDashboardProps {
    /** Restrict the dashboard to monitors of one dataset (used by QualityTab). */
    datasetUrn?: string;
    pollIntervalMs?: number;
    /** Hide the page header/stats when embedded inside an entity tab. */
    embedded?: boolean;
}

type StatusFilter = EvaluationStatus | 'ALL' | 'PAUSED';

export function summarize(monitors: Monitor[]) {
    const counts: Record<string, number> = { total: monitors.length, PASS: 0, FAIL: 0, WARN: 0, TRAINING: 0, ERROR: 0 };
    monitors.forEach((m) => {
        const s = m.last_evaluation?.status;
        if (s) counts[s] = (counts[s] ?? 0) + 1;
    });
    return counts;
}

export function QualityDashboard({ datasetUrn, pollIntervalMs = 30000, embedded = false }: QualityDashboardProps) {
    const client = useDheClient();
    const { data, error, loading, reload, setData } = usePolling(
        (signal) => client.listMonitors({ signal }),
        pollIntervalMs,
        [client],
    );
    const [editorOpen, setEditorOpen] = useState(false);
    const [editing, setEditing] = useState<Monitor | null>(null);
    const [search, setSearch] = useState('');
    const [typeFilter, setTypeFilter] = useState<MonitorType | 'ALL'>('ALL');
    const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL');
    const [runningId, setRunningId] = useState<string | null>(null);
    const [refreshKeys, setRefreshKeys] = useState<Record<string, number>>({});

    const monitors = useMemo(() => {
        const q = search.trim().toLowerCase();
        return (data ?? [])
            .filter((m) => !datasetUrn || m.dataset_urn === datasetUrn)
            .filter((m) => typeFilter === 'ALL' || m.type === typeFilter)
            .filter((m) => {
                if (statusFilter === 'ALL') return true;
                if (statusFilter === 'PAUSED') return !m.enabled || m.status === 'PAUSED';
                return m.last_evaluation?.status === statusFilter;
            })
            .filter((m) => !q || m.name.toLowerCase().includes(q) || m.dataset_urn.toLowerCase().includes(q));
    }, [data, datasetUrn, typeFilter, statusFilter, search]);

    const stats = useMemo(
        () => summarize((data ?? []).filter((m) => !datasetUrn || m.dataset_urn === datasetUrn)),
        [data, datasetUrn],
    );

    const runNow = async (m: Monitor) => {
        setRunningId(m.id);
        try {
            const ev = await client.runMonitor(m.id);
            setData((prev) => (prev ?? []).map((x) => (x.id === m.id ? { ...x, last_evaluation: ev } : x)));
            setRefreshKeys((k) => ({ ...k, [m.id]: (k[m.id] ?? 0) + 1 }));
            message.info(`${m.name}: ${ev.status}${ev.message ? ` — ${ev.message}` : ''}`);
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setRunningId(null);
        }
    };

    const remove = async (m: Monitor) => {
        try {
            await client.deleteMonitor(m.id);
            setData((prev) => (prev ?? []).filter((x) => x.id !== m.id));
            message.success(`Deleted ${m.name}`);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    const toggle = async (m: Monitor) => {
        try {
            const updated = await client.updateMonitor(m.id, {
                name: m.name,
                dataset_urn: m.dataset_urn,
                type: m.type,
                schedule: m.schedule,
                config: m.config,
                sensitivity: m.sensitivity,
                severity: m.severity,
                enabled: !m.enabled,
            });
            setData((prev) => (prev ?? []).map((x) => (x.id === m.id ? updated : x)));
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    const body = () => {
        if (loading && !data) return <LoadingState label="Loading monitors…" />;
        if (error && !data) return <ErrorState error={error} onRetry={reload} />;
        if ((data ?? []).length === 0 || (datasetUrn && stats.total === 0)) {
            return (
                <EmptyState description="No data quality monitors yet">
                    <Button type="primary" onClick={() => setEditorOpen(true)}>
                        Create a monitor
                    </Button>
                </EmptyState>
            );
        }
        return (
            <Table<Monitor>
                rowKey="id"
                dataSource={monitors}
                pagination={{ pageSize: 20, hideOnSinglePage: true }}
                expandable={{
                    expandedRowRender: (m) => <MonitorDetail monitor={m} refreshKey={refreshKeys[m.id] ?? 0} />,
                }}
                locale={{ emptyText: 'No monitors match the filters' }}
                columns={[
                    {
                        title: 'Monitor',
                        dataIndex: 'name',
                        render: (name: string, m) => (
                            <Space direction="vertical" size={0}>
                                <strong>{name}</strong>
                                {!datasetUrn && (
                                    <Tooltip title={m.dataset_urn}>
                                        <small>{datasetNameFromUrn(m.dataset_urn)}</small>
                                    </Tooltip>
                                )}
                            </Space>
                        ),
                    },
                    { title: 'Type', dataIndex: 'type', render: (t: MonitorType) => MONITOR_TYPE_LABELS[t] ?? t, width: 150 },
                    {
                        title: 'Status',
                        width: 130,
                        render: (_: unknown, m) => {
                            if (!m.enabled || m.status === 'PAUSED') return <Tag>PAUSED</Tag>;
                            const s = m.last_evaluation?.status ?? (m.status === 'TRAINING' ? 'TRAINING' : undefined);
                            return <Tag color={statusColor(s)}>{s ?? 'PENDING'}</Tag>;
                        },
                    },
                    { title: 'Severity', dataIndex: 'severity', width: 100 },
                    {
                        title: 'Last run',
                        width: 120,
                        render: (_: unknown, m) => (
                            <Tooltip title={m.last_evaluation?.message}>{formatRelative(m.last_evaluation?.ts)}</Tooltip>
                        ),
                    },
                    {
                        title: 'Actions',
                        width: 260,
                        render: (_: unknown, m) => (
                            <Space size={4} wrap>
                                <Button size="small" loading={runningId === m.id} onClick={() => runNow(m)}>
                                    Run now
                                </Button>
                                <Button
                                    size="small"
                                    onClick={() => {
                                        setEditing(m);
                                        setEditorOpen(true);
                                    }}
                                >
                                    Edit
                                </Button>
                                <Button size="small" onClick={() => toggle(m)}>
                                    {m.enabled ? 'Pause' : 'Resume'}
                                </Button>
                                <Popconfirm
                                    title="Delete this monitor? Its assertion will be removed from DataHub."
                                    onConfirm={() => remove(m)}
                                >
                                    <Button size="small" danger>
                                        Delete
                                    </Button>
                                </Popconfirm>
                            </Space>
                        ),
                    },
                ]}
            />
        );
    };

    return (
        <PageContainer style={embedded ? { padding: 0 } : undefined}>
            {!embedded && (
                <PageHeader>
                    <h1>Data quality</h1>
                    <Button
                        type="primary"
                        onClick={() => {
                            setEditing(null);
                            setEditorOpen(true);
                        }}
                    >
                        New monitor
                    </Button>
                </PageHeader>
            )}
            {!embedded && (
                <Row gutter={16}>
                    {(
                        [
                            ['Monitors', stats.total, undefined],
                            ['Passing', stats.PASS, '#52c41a'],
                            ['Failing', stats.FAIL, '#f5222d'],
                            ['Warning', stats.WARN, '#faad14'],
                            ['Learning', stats.TRAINING, '#1890ff'],
                        ] as Array<[string, number, string | undefined]>
                    ).map(([title, value, color]) => (
                        <Col key={title} xs={12} md={4}>
                            <Card size="small">
                                <Statistic title={title} value={value} valueStyle={color ? { color } : undefined} />
                            </Card>
                        </Col>
                    ))}
                </Row>
            )}
            <Space wrap>
                <Input.Search
                    allowClear
                    placeholder="Search monitors"
                    aria-label="Search monitors"
                    onChange={(e) => setSearch(e.target.value)}
                    style={{ width: 260 }}
                />
                <Select
                    aria-label="Filter by type"
                    value={typeFilter}
                    onChange={setTypeFilter}
                    style={{ width: 180 }}
                    options={[
                        { value: 'ALL', label: 'All types' },
                        ...(Object.keys(MONITOR_TYPE_LABELS) as MonitorType[]).map((t) => ({
                            value: t,
                            label: MONITOR_TYPE_LABELS[t],
                        })),
                    ]}
                />
                <Select
                    aria-label="Filter by status"
                    value={statusFilter}
                    onChange={setStatusFilter}
                    style={{ width: 160 }}
                    options={['ALL', 'PASS', 'FAIL', 'WARN', 'TRAINING', 'ERROR', 'PAUSED'].map((s) => ({
                        value: s,
                        label: s === 'ALL' ? 'All statuses' : s,
                    }))}
                />
                {embedded && (
                    <Button
                        type="primary"
                        onClick={() => {
                            setEditing(null);
                            setEditorOpen(true);
                        }}
                    >
                        New monitor
                    </Button>
                )}
            </Space>
            {body()}
            <MonitorEditor
                open={editorOpen}
                monitor={editing}
                defaultDatasetUrn={datasetUrn}
                onClose={() => setEditorOpen(false)}
                onSaved={(saved) => {
                    setEditorOpen(false);
                    setData((prev) => {
                        const list = prev ?? [];
                        return list.some((m) => m.id === saved.id)
                            ? list.map((m) => (m.id === saved.id ? saved : m))
                            : [saved, ...list];
                    });
                    message.success(`Saved ${saved.name}`);
                }}
            />
        </PageContainer>
    );
}

export default QualityDashboard;
