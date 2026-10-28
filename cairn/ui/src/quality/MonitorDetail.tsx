import React from 'react';
import { Button, Space, Table, Tag, Tooltip, message } from 'antd';

import { useAsync, useDheClient } from '../api/hooks';
import type { Evaluation, EvaluationFeedback, Monitor } from '../api/types';
import { formatNumber, formatTimestamp, statusColor } from '../common/format';
import { ErrorState, LoadingState } from '../common/States';
import { EvaluationChart } from './EvaluationChart';

export interface MonitorDetailProps {
    monitor: Monitor;
    limit?: number;
    /** Bumping this value forces a reload (e.g. after "Run now"). */
    refreshKey?: number;
}

const FEEDBACK_DONE: Record<EvaluationFeedback, string> = {
    EXPECTED: 'Marked as expected: kept in the baseline so the monitor learns',
    REAL: 'Confirmed as a real anomaly: kept out of the baseline',
    NEW_NORMAL: 'Baseline restarted from this value',
};
const FEEDBACK_TAG: Record<EvaluationFeedback, string> = { EXPECTED: 'Expected', REAL: 'Confirmed', NEW_NORMAL: 'New normal' };

/** Chart + evaluation history for one monitor, with "expected / real" anomaly feedback. */
export function MonitorDetail({ monitor, limit = 200, refreshKey = 0 }: MonitorDetailProps) {
    const client = useDheClient();
    const { data, error, loading, reload, setData } = useAsync(
        (signal) => client.listEvaluations(monitor.id, limit, { signal }),
        [client, monitor.id, limit, refreshKey],
    );

    const giveFeedback = async (ev: Evaluation, feedback: EvaluationFeedback) => {
        try {
            const updated = await client.sendFeedback(ev.id, feedback);
            setData((prev) => (prev ?? []).map((e) => (e.id === ev.id ? { ...e, ...updated } : e)));
            message.success(FEEDBACK_DONE[feedback]);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    if (loading && !data) return <LoadingState label="Loading evaluations…" />;
    if (error) return <ErrorState error={error} onRetry={reload} />;
    const evaluations = data ?? [];

    return (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <EvaluationChart evaluations={evaluations} title={`${monitor.name} history`} />
            <Table<Evaluation>
                size="small"
                rowKey="id"
                dataSource={[...evaluations].sort((a, b) => b.ts.localeCompare(a.ts))}
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                columns={[
                    { title: 'Time', dataIndex: 'ts', render: (v: string) => formatTimestamp(v), width: 190 },
                    {
                        title: 'Status',
                        dataIndex: 'status',
                        width: 100,
                        render: (s: Evaluation['status']) => <Tag color={statusColor(s)}>{s}</Tag>,
                    },
                    { title: 'Value', dataIndex: 'metric_value', render: (v: number | null) => formatNumber(v) },
                    {
                        title: 'Expected',
                        render: (_: unknown, ev) =>
                            ev.expected_low === null && ev.expected_high === null
                                ? '—'
                                : `${formatNumber(ev.expected_low)} – ${formatNumber(ev.expected_high)}`,
                    },
                    {
                        title: 'Score',
                        dataIndex: 'score',
                        render: (v: number | null) => (v === null ? '—' : v.toFixed(2)),
                        width: 80,
                    },
                    { title: 'Message', dataIndex: 'message', ellipsis: true },
                    {
                        title: 'Feedback',
                        width: 280,
                        render: (_: unknown, ev) => {
                            if (ev.status !== 'FAIL' && ev.status !== 'WARN') return null;
                            if (ev.feedback) {
                                return <Tag>{FEEDBACK_TAG[ev.feedback]}</Tag>;
                            }
                            return (
                                <Space size={4}>
                                    <Tooltip title="Not a real issue: keep it in the baseline so the monitor learns">
                                        <Button size="small" onClick={() => giveFeedback(ev, 'EXPECTED')}>
                                            Expected
                                        </Button>
                                    </Tooltip>
                                    <Tooltip title="An intended step change: restart the baseline from this value">
                                        <Button size="small" onClick={() => giveFeedback(ev, 'NEW_NORMAL')}>
                                            New normal
                                        </Button>
                                    </Tooltip>
                                    <Button size="small" danger onClick={() => giveFeedback(ev, 'REAL')}>
                                        Real
                                    </Button>
                                </Space>
                            );
                        },
                    },
                ]}
            />
        </Space>
    );
}

export default MonitorDetail;
