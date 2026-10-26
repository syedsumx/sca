import React from 'react';
import { Badge, Popover, Tag, Tooltip } from 'antd';
import styled from 'styled-components';

import { useAsync, useDheClient } from '../api/hooks';
import type { DatasetHealth } from '../api/types';
import { formatRelative, statusColor } from '../common/format';

const List = styled.ul`
    list-style: none;
    margin: 0;
    padding: 0;
    min-width: 260px;

    li {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 4px 0;
    }
`;

export interface DatasetHealthBadgeProps {
    datasetUrn: string;
    /** Supply health directly (e.g. from a parent that already fetched it) to skip the request. */
    health?: DatasetHealth;
    onClick?: () => void;
}

const LABELS: Record<string, string> = {
    PASS: 'Healthy',
    WARN: 'Warning',
    FAIL: 'Failing',
    ERROR: 'Error',
    TRAINING: 'Learning',
    UNKNOWN: 'No monitors',
};

export function DatasetHealthBadge({ datasetUrn, health: provided, onClick }: DatasetHealthBadgeProps) {
    const client = useDheClient();
    const { data, error, loading } = useAsync(
        (signal) => client.getDatasetHealth(datasetUrn, { signal }),
        [client, datasetUrn],
        { enabled: !provided },
    );
    const health = provided ?? data;

    if (loading && !health) return <Tag>Quality…</Tag>;
    if (error && !health) {
        return (
            <Tooltip title={error.message}>
                <Tag>Quality unavailable</Tag>
            </Tooltip>
        );
    }
    if (!health) return null;

    const content =
        health.monitors.length === 0 ? (
            <span>No data quality monitors configured.</span>
        ) : (
            <List aria-label="Monitor statuses">
                {health.monitors.map(({ monitor, last_evaluation: ev }) => (
                    <li key={monitor.id}>
                        <span>{monitor.name}</span>
                        <span>
                            <Tag color={statusColor(ev?.status)}>{ev?.status ?? 'PENDING'}</Tag>
                            <small>{formatRelative(ev?.ts)}</small>
                        </span>
                    </li>
                ))}
            </List>
        );

    return (
        <Popover title="Data quality" content={content} trigger={['hover', 'focus']}>
            <Badge count={health.open_incidents} size="small" title={`${health.open_incidents} open incidents`}>
                <Tag
                    color={statusColor(health.status)}
                    onClick={onClick}
                    tabIndex={0}
                    role={onClick ? 'button' : undefined}
                    onKeyDown={(e) => {
                        if (onClick && (e.key === 'Enter' || e.key === ' ')) onClick();
                    }}
                    style={{ cursor: onClick ? 'pointer' : undefined }}
                >
                    {LABELS[health.status] ?? health.status}
                </Tag>
            </Badge>
        </Popover>
    );
}

export default DatasetHealthBadge;
