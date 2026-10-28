import { Alert, Button, Empty, Input, Space, Table, Tooltip } from 'antd';
import React, { useState } from 'react';

import { CairnApiError } from '../../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DataResult } from '../api/types';
import { Muted } from './tokens';

function ResultTable({ result }: { result: DataResult }) {
    const masked = new Set(result.masked_columns);
    return (
        <>
            <Table
                size="small"
                scroll={{ x: true }}
                rowKey={(_, i) => String(i)}
                dataSource={result.rows.map((r) => Object.fromEntries(r.map((v, i) => [String(i), v])))}
                pagination={{ pageSize: 25, hideOnSinglePage: true, showSizeChanger: false }}
                columns={result.columns.map((c, i) => ({
                    title: masked.has(c) ? (
                        <Tooltip title="Masked for you by a data policy">
                            <span>🔒 {c}</span>
                        </Tooltip>
                    ) : (
                        c
                    ),
                    dataIndex: String(i),
                    render: (v: unknown) => (v === null ? <Muted>null</Muted> : String(v)),
                }))}
            />
            <Muted style={{ fontSize: 12 }}>
                {result.rows.length} row(s){result.truncated ? ' (more not shown)' : ''} · {result.elapsed_ms} ms
                {result.masked_columns.length ? ` · ${result.masked_columns.length} column(s) masked` : ''}
            </Muted>
        </>
    );
}

/** First rows of the table, and a read-only SQL editor over the same connection. */
export function DataPreview({ urn, tableName }: { urn: string; tableName: string }) {
    const { client } = useConsumerApi();
    const preview = useDheQuery<DataResult>(`/data/preview?${urn}`, (c, signal) => c.previewData(urn, { signal }));
    const [sql, setSql] = useState(`SELECT *\nFROM ${tableName}\nLIMIT 50`);
    const [result, setResult] = useState<DataResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);

    const err = preview.error;
    if (err instanceof CairnApiError && err.status === 404)
        return <Empty description="No warehouse connection serves this table, so there is nothing to preview." />;
    if (err instanceof CairnApiError && err.status === 403)
        return (
            <Alert
                type="info"
                showIcon
                message="A data policy keeps this table's rows from you. Request access to see them."
            />
        );

    const run = async () => {
        setBusy(true);
        setError(null);
        try {
            setResult(await client.runQuery(sql, urn));
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <section aria-label="Preview">
                {err && <Alert type="error" message={err.message} />}
                {preview.data && <ResultTable result={preview.data} />}
            </section>
            <section aria-label="Query">
                <Input.TextArea
                    aria-label="SQL"
                    rows={5}
                    spellCheck={false}
                    value={sql}
                    onChange={(e) => setSql(e.target.value)}
                    style={{ fontFamily: 'monospace' }}
                />
                <Space style={{ margin: '8px 0' }}>
                    <Button type="primary" onClick={() => void run()} loading={busy}>
                        Run query
                    </Button>
                    <Muted>
                        SELECT only · up to 1,000 rows · 15 s · every query is logged · masked like the preview
                    </Muted>
                </Space>
                {error && <Alert type="error" message={error} />}
                {result && <ResultTable result={result} />}
            </section>
        </Space>
    );
}
