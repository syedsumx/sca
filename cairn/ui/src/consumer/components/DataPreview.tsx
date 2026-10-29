import { Alert, Button, Empty, Input, Space, Table, Tag, Tooltip } from 'antd';
import React, { useState } from 'react';

import { CairnApiError } from '../../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { AskResult, DataResult } from '../api/types';
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

/** A question in plain words, answered with governed SQL that the caller can read and reuse. */
function AskBox({ urn, onUseSql }: { urn: string; onUseSql: (sql: string) => void }) {
    const { client } = useConsumerApi();
    const [question, setQuestion] = useState('');
    const [answer, setAnswer] = useState<AskResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);

    const ask = async () => {
        setBusy(true);
        setError(null);
        setAnswer(null);
        try {
            setAnswer(await client.askData(question, [urn]));
        } catch (e) {
            setError(
                e instanceof CairnApiError && e.status === 503
                    ? 'Asking questions needs an AI provider; an administrator can set one up.'
                    : (e as Error).message,
            );
        } finally {
            setBusy(false);
        }
    };

    return (
        <section aria-label="Ask">
            <Input.Search
                aria-label="Question"
                placeholder="Ask a question about this data, e.g. revenue by month this year"
                enterButton="Ask"
                value={question}
                loading={busy}
                onChange={(e) => setQuestion(e.target.value)}
                onSearch={() => question.trim().length >= 3 && void ask()}
            />
            {error && <Alert style={{ marginTop: 8 }} type="error" message={error} />}
            {answer && (
                <Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
                    {answer.explanation && <span>{answer.explanation}</span>}
                    {answer.definitions.length > 0 && (
                        <span>
                            <Muted>Uses approved definitions: </Muted>
                            {answer.definitions.map((d) => (
                                <Tag key={d.name} color="green">
                                    {d.name} v{d.version}
                                </Tag>
                            ))}
                        </span>
                    )}
                    {answer.sql && (
                        <pre aria-label="Generated SQL" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                            {answer.sql}
                        </pre>
                    )}
                    {answer.sql && (
                        <Button size="small" onClick={() => onUseSql(answer.sql as string)}>
                            Edit in the SQL editor
                        </Button>
                    )}
                    {answer.error && <Alert type="warning" message={answer.error} />}
                    {answer.result && <ResultTable result={answer.result} />}
                </Space>
            )}
        </section>
    );
}

/** First rows of the table, a question box, and a read-only SQL editor over the same connection. */
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
            <AskBox urn={urn} onUseSql={setSql} />
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
