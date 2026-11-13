import { Alert, Button, Drawer, Input, Space, Tag, Typography } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi } from '../api/hooks';
import type { SqlExplanation, SqlWritten } from '../api/types';
import { describeError } from './ErrorNotice';
import { Muted } from './tokens';

const codeStyle: React.CSSProperties = { fontSize: 12, padding: 8, background: 'var(--cairn-sunken)',
    border: '1px solid var(--cairn-border)', borderRadius: 6, whiteSpace: 'pre-wrap', margin: '8px 0' };

/**
 * The SQL editor's assistant (L163): write a statement from a question, or explain the statement at the cursor in
 * plain words. It is told the names and columns of the tables the person may see, never rows, and runs nothing: what
 * it writes goes into the editor for the person to read and run (as themselves).
 */
export function SqlAssistantDrawer({ open, onClose, connection, statement, onInsert, onOpenTab }: {
    open: boolean;
    onClose: () => void;
    connection?: string;
    /** The statement at the cursor (for Explain). */
    statement: string;
    onInsert: (sql: string) => void;
    onOpenTab: (sql: string, connection?: string) => void;
}) {
    const { client } = useConsumerApi();
    const [question, setQuestion] = useState('');
    const [written, setWritten] = useState<SqlWritten | null>(null);
    const [explained, setExplained] = useState<SqlExplanation | null>(null);
    const [busy, setBusy] = useState<'write' | 'explain' | null>(null);
    const [error, setError] = useState<string | null>(null);

    const write = async () => {
        if (!connection || question.trim().length < 3) return;
        setBusy('write');
        setError(null);
        try {
            setWritten(await client.assistWrite(question.trim(), connection));
        } catch (e) {
            setError(describeError(e));
        } finally {
            setBusy(null);
        }
    };
    const explain = async () => {
        if (!connection || !statement.trim()) return;
        setBusy('explain');
        setError(null);
        try {
            setExplained(await client.assistExplain(statement, connection));
        } catch (e) {
            setError(describeError(e));
        } finally {
            setBusy(null);
        }
    };

    return (
        <Drawer open={open} onClose={onClose} title="SQL assistant" width={480} destroyOnClose={false}>
            <Muted style={{ display: 'block', marginBottom: 12 }}>
                It sees the names and columns of the tables you may query, never their rows, and runs nothing: you read
                what it writes and run it yourself.
            </Muted>
            <Typography.Title level={5}>Write SQL</Typography.Title>
            <Input.TextArea aria-label="Question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)}
                placeholder="Revenue by region last month, paid orders only" />
            <Button style={{ marginTop: 8 }} type="primary" loading={busy === 'write'} onClick={() => void write()}
                disabled={!connection || question.trim().length < 3}>Write</Button>
            {written && (
                <div data-testid="assistant-written" style={{ marginTop: 8 }}>
                    {written.sql ? (
                        <>
                            <pre aria-label="Proposed SQL" style={codeStyle}>{written.sql}</pre>
                            {written.explanation && <Muted style={{ display: 'block' }}>{written.explanation}</Muted>}
                            {!written.same_connection && written.connection && (
                                <Alert style={{ marginTop: 8 }} type="info" showIcon
                                    message={`Written for ${written.connection}, where the tables that answer it are`} />
                            )}
                            <Space style={{ marginTop: 8 }} wrap>
                                {written.same_connection && <Button onClick={() => onInsert(written.sql!)}>Insert at cursor</Button>}
                                <Button onClick={() => onOpenTab(written.sql!, written.connection ?? connection)}>Open in a new tab</Button>
                            </Space>
                        </>
                    ) : (
                        <Alert type="warning" showIcon message={written.error ?? 'No statement came back'} />
                    )}
                </div>
            )}
            <Typography.Title level={5} style={{ marginTop: 24 }}>Explain in words</Typography.Title>
            <Muted style={{ display: 'block' }}>The statement at the cursor, in plain words.</Muted>
            <Button style={{ marginTop: 8 }} loading={busy === 'explain'} onClick={() => void explain()}
                disabled={!connection || !statement.trim()}>Explain the statement</Button>
            {explained && (
                <div data-testid="assistant-explained" style={{ marginTop: 8 }}>
                    <p>{explained.summary}</p>
                    <ol>{explained.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                    {explained.cautions.length > 0 && (
                        <Alert type="warning" showIcon message="Worth checking"
                            description={<ul style={{ margin: 0 }}>{explained.cautions.map((c, i) => <li key={i}>{c}</li>)}</ul>} />
                    )}
                    {explained.tables.length > 0 && (
                        <Space wrap style={{ marginTop: 8 }}>{explained.tables.map((t) => <Tag key={t}>{t}</Tag>)}</Space>
                    )}
                </div>
            )}
            {error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={error} />}
        </Drawer>
    );
}

/**
 * A failed statement's fix (L163): the assistant proposes a corrected statement from the error; the person sees it
 * beside what they wrote and replaces the statement with it if they want (undo brings theirs back).
 */
export function FixWithAssistant({ sql, error, connection, onReplace }: {
    sql: string;
    error: string;
    connection?: string;
    onReplace: (fixed: string) => void;
}) {
    const { client } = useConsumerApi();
    const [fix, setFix] = useState<{ sql: string; explanation: string } | null>(null);
    const [busy, setBusy] = useState(false);
    const [failed, setFailed] = useState<string | null>(null);
    const ask = async () => {
        if (!connection) return;
        setBusy(true);
        setFailed(null);
        try {
            setFix(await client.assistFix(sql, error, connection));
        } catch (e) {
            setFailed(describeError(e));
        } finally {
            setBusy(false);
        }
    };
    return (
        <div data-testid="assistant-fix" style={{ marginTop: 8 }}>
            {!fix && <Button size="small" loading={busy} onClick={() => void ask()} disabled={!connection}>Fix with AI</Button>}
            {failed && <Muted style={{ display: 'block' }}>Could not get a fix: {failed}</Muted>}
            {fix && (
                <>
                    <Muted style={{ display: 'block' }}>{fix.explanation}</Muted>
                    <pre aria-label="Proposed fix" style={codeStyle}>{fix.sql}</pre>
                    <Space>
                        <Button size="small" type="primary" onClick={() => onReplace(fix.sql)}>Replace the statement</Button>
                        <Button size="small" onClick={() => setFix(null)}>Dismiss</Button>
                    </Space>
                </>
            )}
        </div>
    );
}
