import { Alert, Button, Checkbox, Empty, Input, InputNumber, Modal, Select, Space, Spin, Tag } from 'antd';
import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi } from '../api/hooks';
import { Muted, tokens } from '../components/tokens';
import { BatchResult, Proposal, ontologyApi, termHref } from './api';
import { QueueFilter, aboveThreshold, filterQueue, keyIntent, queueReducer, targets } from './review';

const List = styled.div`
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    background: ${tokens.color.surface};
    max-height: 560px;
    overflow: auto;
    &:focus-visible {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: 2px;
    }
`;

const Row = styled.div<{ $cursor: boolean }>`
    display: grid;
    grid-template-columns: 28px 90px 1fr 70px;
    gap: 8px;
    align-items: start;
    padding: 8px 12px;
    border-bottom: 1px solid ${tokens.color.border};
    background: ${(p) => (p.$cursor ? tokens.color.primarySoft : 'transparent')};
    border-left: 3px solid ${(p) => (p.$cursor ? tokens.color.primary : 'transparent')};
    .summary {
        color: ${tokens.color.textMuted};
        font-size: 12px;
        white-space: pre-wrap;
        max-height: 3.6em;
        overflow: hidden;
    }
`;

/** The proposals waiting for review: filter by kind and confidence, select (x or the checkbox), accept (a) or reject
 * (r) the selection or the row under the cursor (j/k), with one comment; or accept everything above a confidence
 * after seeing the list. Each decision is the reviewer's own, under their privileges. */
export function ReviewQueue({ onChanged }: { onChanged?: () => void }) {
    const { client } = useConsumerApi();
    const api = useMemo(() => ontologyApi(client), [client]);
    const [items, setItems] = useState<Proposal[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [filter, setFilter] = useState<QueueFilter>({ kind: '', minConfidence: 0 });
    const [state, dispatch] = useReducer(queueReducer, { cursor: 0, selected: [] });
    const [comment, setComment] = useState('');
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<BatchResult | null>(null);
    const [threshold, setThreshold] = useState(0.9);
    const [preview, setPreview] = useState<Proposal[] | null>(null);
    const listRef = useRef<HTMLDivElement | null>(null);

    const load = useCallback(async () => {
        try {
            setItems((await api.proposals()).proposals);
            setError(null);
        } catch (e) {
            setError((e as Error).message);
        }
    }, [api]);
    useEffect(() => {
        void load();
    }, [load]);

    const shown = useMemo(() => filterQueue(items ?? [], filter), [items, filter]);

    const decide = async (ids: string[], action: 'accept' | 'reject') => {
        if (!ids.length) return;
        setBusy(true);
        setResult(null);
        try {
            const out = await api.decideMany(ids, action, comment);
            setResult(out);
            const done = out.results.filter((r) => r.ok).map((r) => r.id);
            setItems((cur) => (cur ?? []).filter((p) => !done.includes(p.id)));
            dispatch({ type: 'removed', ids: done, count: shown.length - done.length });
            if (done.length) onChanged?.();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
            listRef.current?.focus();
        }
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if ((e.target as HTMLElement).tagName === 'INPUT' && e.key !== 'Escape') return;
        const intent = keyIntent(e.key, { ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey });
        if (!intent) return;
        e.preventDefault();
        if ('move' in intent) dispatch({ type: 'move', by: intent.move, count: shown.length });
        else if ('toggle' in intent) {
            const p = shown[state.cursor];
            if (p) dispatch({ type: 'toggle', id: p.id });
        } else if ('decide' in intent) void decide(targets(shown, state), intent.decide);
        else if ('open' in intent) {
            const p = shown[state.cursor];
            if (p?.kind === 'term' && p.element_id) window.location.hash = `#${termHref(p.element_id)}`;
        }
    };

    useEffect(() => {
        listRef.current?.querySelector('[data-cursor="true"]')?.scrollIntoView?.({ block: 'nearest' });
    }, [state.cursor]);

    if (error && !items) return <Alert type="warning" showIcon message="The review queue is not available" description={error} />;
    if (!items) return <Spin />;
    const chosen = targets(shown, state);
    return (
        <div>
            <Space wrap style={{ marginBottom: 8 }}>
                <Select
                    aria-label="Kind"
                    value={filter.kind}
                    style={{ width: 160 }}
                    onChange={(kind) => setFilter((f) => ({ ...f, kind }))}
                    options={[{ value: '', label: 'Every kind' }, { value: 'term', label: 'Terms' },
                        { value: 'relationship', label: 'Relationships' }, { value: 'metric', label: 'Metrics' }]}
                />
                <label>
                    Confidence at least{' '}
                    <InputNumber aria-label="Minimum confidence" min={0} max={1} step={0.05} value={filter.minConfidence}
                        onChange={(v) => setFilter((f) => ({ ...f, minConfidence: Number(v ?? 0) }))} />
                </label>
                <Input aria-label="Review comment" placeholder="Comment for these decisions" value={comment}
                    onChange={(e) => setComment(e.target.value)} style={{ width: 260 }} />
            </Space>
            <Space wrap style={{ marginBottom: 8 }}>
                <Checkbox
                    checked={shown.length > 0 && state.selected.length === shown.length}
                    indeterminate={state.selected.length > 0 && state.selected.length < shown.length}
                    onChange={() => dispatch({ type: 'selectAll', ids: shown.map((p) => p.id) })}
                >
                    Select all ({shown.length})
                </Checkbox>
                <Button type="primary" disabled={!chosen.length} loading={busy} onClick={() => void decide(chosen, 'accept')}>
                    Accept {state.selected.length ? `${state.selected.length} selected` : 'current'}
                </Button>
                <Button danger disabled={!chosen.length} loading={busy} onClick={() => void decide(chosen, 'reject')}>
                    Reject {state.selected.length ? `${state.selected.length} selected` : 'current'}
                </Button>
                <span>
                    <InputNumber aria-label="Accept threshold" min={0} max={1} step={0.05} value={threshold}
                        onChange={(v) => setThreshold(Number(v ?? 0.9))} style={{ width: 80 }} />{' '}
                    <Button onClick={() => setPreview(aboveThreshold(shown, threshold))}>Accept all above {threshold}…</Button>
                </span>
            </Space>
            <Muted style={{ display: 'block', fontSize: 12, marginBottom: 6 }}>
                Keys (in the list): j/k move · x select · a accept · r reject · o open the term. Decisions apply to the selection, or the
                highlighted row when nothing is selected.
            </Muted>
            {result && (
                <Alert
                    style={{ marginBottom: 8 }}
                    type={result.refused ? 'warning' : 'success'}
                    showIcon
                    message={`${result.action === 'accept' ? 'Accepted' : 'Rejected'} ${result.done}${result.refused ? `; ${result.refused} refused` : ''}`}
                    description={result.refused ? result.results.filter((r) => !r.ok).map((r) => <div key={r.id}>{r.id}: {r.error}</div>) : undefined}
                />
            )}
            {error && items && <Alert style={{ marginBottom: 8 }} type="error" showIcon message={error} />}
            {shown.length === 0 ? (
                <Empty description="Nothing waiting for review" />
            ) : (
                <List ref={listRef} tabIndex={0} role="region" aria-label="Proposals to review (j and k move, x selects, a accepts, r rejects)"
                    onKeyDown={onKeyDown} data-testid="review-queue">
                    <span className="cairn-sr-only" aria-live="polite" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>
                        {shown[state.cursor] ? `${state.cursor + 1} of ${shown.length}: ${shown[state.cursor].title}` : ''}
                    </span>
                    {shown.map((p, i) => (
                        <Row
                            key={p.id}
                            id={`proposal-${p.id}`}
                            aria-current={i === state.cursor ? 'true' : undefined}
                            $cursor={i === state.cursor}
                            data-cursor={i === state.cursor}
                            data-proposal={p.id}
                            onClick={() => dispatch({ type: 'move', by: i - state.cursor, count: shown.length })}
                        >
                            <Checkbox
                                aria-label={`Select ${p.title}`}
                                checked={state.selected.includes(p.id)}
                                onChange={() => dispatch({ type: 'toggle', id: p.id })}
                                tabIndex={-1}
                            />
                            <Tag>{p.kind}</Tag>
                            <div>
                                {p.kind === 'term' && p.element_id ? <Link to={termHref(p.element_id)} tabIndex={-1}>{p.title}</Link> : p.title}
                                <div className="summary">{p.summary_md.split('\n')[0].slice(0, 240)}</div>
                            </div>
                            <span title="confidence">{Math.round(p.confidence * 100)}%</span>
                        </Row>
                    ))}
                </List>
            )}
            <Modal
                title={`Accept ${preview?.length ?? 0} proposals at confidence ${threshold} or more?`}
                open={preview !== null}
                okText={`Accept ${preview?.length ?? 0}`}
                okButtonProps={{ disabled: !preview?.length }}
                confirmLoading={busy}
                onCancel={() => setPreview(null)}
                onOk={async () => {
                    const ids = (preview ?? []).map((p) => p.id);
                    setPreview(null);
                    await decide(ids, 'accept');
                }}
            >
                {preview?.length ? (
                    <ul data-testid="threshold-preview" style={{ maxHeight: 320, overflow: 'auto' }}>
                        {preview.map((p) => <li key={p.id}>{p.title} ({Math.round(p.confidence * 100)}%)</li>)}
                    </ul>
                ) : (
                    <Muted>No proposal shown is at that confidence.</Muted>
                )}
                <Muted>Each is accepted as you, with the comment above; any you may not accept is refused and listed.</Muted>
            </Modal>
        </div>
    );
}
