import { Alert, Button, Descriptions, Space, Typography } from 'antd';
import React, { useEffect, useState } from 'react';

import { useConsumerApi } from '../api/hooks';
import type { DataResult, SqlPlan } from '../api/types';
import { bytes } from '../sql/resultStats';
import { describeError } from './ErrorNotice';
import { Muted } from './tokens';

/**
 * A statement's details (L163): how it ran (rows, time, what Trino processed and how long it queued, its query id,
 * the warehouse, the columns masked for the person) and, on request, the engine's plan of it as the person runs it:
 * their row filters and policies included, and nothing runs for it.
 */
export function QueryDetails({ sql, connection, result, auto = false }: { sql: string; connection?: string;
    result: DataResult | null; auto?: boolean }) {
    const { client } = useConsumerApi();
    const [plan, setPlan] = useState<SqlPlan | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    useEffect(() => {
        setPlan(null);
        setError(null);
    }, [sql, connection]);

    const explain = async () => {
        if (!connection) return;
        setLoading(true);
        setError(null);
        try {
            setPlan(await client.explainSql(sql, connection));
        } catch (e) {
            setError(describeError(e));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (auto) void explain();
        // once, when opened to show the plan
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [auto, sql, connection]);

    const s = result?.stats;
    return (
        <div data-testid="query-details">
            {result && (
                <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} bordered style={{ marginBottom: 12 }}>
                    <Descriptions.Item label="Rows">{result.rows.length}{result.truncated ? ' (more not read)' : ''}</Descriptions.Item>
                    <Descriptions.Item label="Time">{result.elapsed_ms} ms</Descriptions.Item>
                    {s?.queuedTimeMillis != null && <Descriptions.Item label="Queued">{s.queuedTimeMillis} ms</Descriptions.Item>}
                    {s?.processedRows != null && <Descriptions.Item label="Rows read">{s.processedRows.toLocaleString()}</Descriptions.Item>}
                    {s?.processedBytes != null && <Descriptions.Item label="Data read">{bytes(s.processedBytes)}</Descriptions.Item>}
                    {result.query_id && (
                        <Descriptions.Item label="Query id"><Typography.Text copyable code>{result.query_id}</Typography.Text></Descriptions.Item>
                    )}
                    {result.warehouse && <Descriptions.Item label="Warehouse">{result.warehouse}</Descriptions.Item>}
                    {result.tables.length > 0 && <Descriptions.Item label="Tables">{result.tables.join(', ')}</Descriptions.Item>}
                    <Descriptions.Item label="Masked for you">{result.masked_columns.join(', ') || 'none'}</Descriptions.Item>
                </Descriptions>
            )}
            <Space wrap>
                <Button onClick={() => void explain()} loading={loading} disabled={!connection || !sql.trim()}>Explain</Button>
                <Muted>The engine&apos;s plan of this statement as you would run it. Nothing runs.</Muted>
            </Space>
            {error && <Alert style={{ marginTop: 8 }} type="error" showIcon message={error} />}
            {plan && (
                <div style={{ marginTop: 8 }}>
                    <Muted>Plan from {plan.dialect} in {plan.elapsed_ms} ms</Muted>
                    <pre aria-label="Plan" style={{ maxHeight: 420, overflow: 'auto', fontSize: 12, padding: 8,
                        background: 'var(--cairn-sunken)', border: '1px solid var(--cairn-border)', borderRadius: 6 }}>
                        {plan.plan}
                    </pre>
                    <details>
                        <summary style={{ cursor: 'pointer' }}>The statement as you run it</summary>
                        <pre style={{ fontSize: 12, padding: 8, background: 'var(--cairn-sunken)', borderRadius: 6 }}>{plan.statement}</pre>
                    </details>
                </div>
            )}
        </div>
    );
}
