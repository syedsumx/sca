import { Alert, Button, Input, List, Modal, Popconfirm, Space, Spin, Tag, Typography, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { Muted, SectionTitle } from './tokens';
import { useT } from '../i18n';
import { actorName } from '../utils/format';

// ---- what /v1/sre answers (cairn.sre) ------------------------------------------------------------------------------
export interface Evidence { id: string; tool: string; input: Record<string, unknown>; result: string }
export interface SreStep { id: string; tool: string; input: Record<string, unknown>; ok: boolean; result?: string; error?: string; ms: number }
export interface SreRun {
    id: string; kind: 'investigate' | 'ask' | 'diagnose' | 'postmortem'; status: 'running' | 'done' | 'refused' | 'failed';
    acts_for: string; asked_by: string | null; question: string | null; answer: string | null; steps: SreStep[];
    error: string | null; started_at: string; finished_at: string | null;
}
export interface SreAction {
    id: string; kind: 'change_request' | 'runbook_action'; action: string | null; params: Record<string, unknown>;
    change_id: string | null; title: string; rationale: string; evidence: Evidence[];
    status: 'pending' | 'executed' | 'applied' | 'rejected' | 'failed'; decided_by: string | null; error: string | null;
}
export interface Postmortem { markdown: string; status: 'draft' | 'approved'; edited_by: string | null; approved_by: string | null }
export interface SreIncident {
    runs: SreRun[]; actions: SreAction[]; postmortem: Postmortem | null;
    status: { ready: boolean; why: string | null; model: string | null; enabled: boolean };
}

const STATUS_COLOR: Record<string, string> = {
    pending: 'gold', executed: 'green', applied: 'green', rejected: 'default', failed: 'red',
    running: 'blue', done: 'green', refused: 'orange',
};

/** The evidence a note or a proposal cites: each tool call's input and (part of) its result. */
export function EvidenceList({ items }: { items: Evidence[] }) {
    const t = useT();
    if (!items?.length) return null;
    return (
        <details style={{ marginTop: 4 }}>
            <summary style={{ cursor: 'pointer' }}>{t('sre.evidence', { n: items.length })}</summary>
            <List size="small" dataSource={items} renderItem={(e) => (
                <List.Item>
                    <div style={{ width: '100%' }}>
                        <Space><Tag>{e.id}</Tag><code>{e.tool}</code><Muted>{JSON.stringify(e.input)}</Muted></Space>
                        <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto', fontSize: 12, margin: '4px 0 0' }}>{e.result}</pre>
                    </div>
                </List.Item>
            )} />
        </details>
    );
}

function Proposal({ a, onDone }: { a: SreAction; onDone: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [busy, setBusy] = useState(false);
    const decide = async (what: 'approve' | 'reject') => {
        setBusy(true);
        try {
            await client.request('POST', `/v1/sre/actions/${encodeURIComponent(a.id)}/${what}`, {});
            message.success(what === 'approve' ? t('sre.approved') : t('sre.rejected'));
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
            onDone();
        }
    };
    return (
        <List.Item>
            <div style={{ width: '100%' }} aria-label={t('sre.proposal', { title: a.title })} role="group">
                <Space wrap>
                    <Tag color={a.kind === 'change_request' ? 'geekblue' : 'purple'}>
                        {a.kind === 'change_request' ? t('sre.kind.change') : t('sre.kind.action')}</Tag>
                    <strong>{a.title}</strong>
                    <Tag color={STATUS_COLOR[a.status]}>{t(`sre.status.${a.status}` as never)}</Tag>
                    {a.decided_by && <Muted>{actorName(a.decided_by)}</Muted>}
                </Space>
                <p style={{ margin: '4px 0' }}>{a.rationale}</p>
                {a.change_id && <Muted>{t('sre.changeRequest', { id: a.change_id.slice(0, 12) })}</Muted>}
                {a.error && <Alert type="error" showIcon message={a.error} style={{ margin: '6px 0' }} />}
                <EvidenceList items={a.evidence} />
                {a.status === 'pending' && (
                    <Space style={{ marginTop: 6 }}>
                        <Popconfirm title={t('sre.approveConfirm')} okText={t('sre.approve')} onConfirm={() => decide('approve')}>
                            <Button type="primary" size="small" loading={busy} aria-label={t('sre.approveNamed', { title: a.title })}>
                                {t('sre.approve')}</Button>
                        </Popconfirm>
                        <Button size="small" disabled={busy} onClick={() => decide('reject')}
                            aria-label={t('sre.rejectNamed', { title: a.title })}>{t('sre.reject')}</Button>
                    </Space>
                )}
            </div>
        </List.Item>
    );
}

function PostmortemEditor({ id, pm, onDone }: { id: string; pm: Postmortem; onDone: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [text, setText] = useState(pm.markdown);
    const call = async (method: 'PUT' | 'POST', path: string, body?: unknown) => {
        try { await client.request(method, `/v1/sre/incidents/${encodeURIComponent(id)}/postmortem${path}`, body); onDone(); }
        catch (e) { notifyError(e); }
    };
    const draft = pm.status === 'draft';
    return (
        <section aria-label={t('sre.postmortem')}>
            <Space style={{ marginBottom: 6 }}>
                <Tag color={draft ? 'gold' : 'green'}>{draft ? t('sre.pm.draft_status') : t('sre.pm.approved')}</Tag>
                {pm.approved_by && <Muted>{actorName(pm.approved_by)}</Muted>}
            </Space>
            <Input.TextArea aria-label={t('sre.postmortem')} value={text} readOnly={!draft} autoSize={{ minRows: 6, maxRows: 20 }}
                onChange={(e) => setText(e.target.value)} style={{ fontFamily: 'monospace', fontSize: 12 }} />
            {draft && (
                <Space style={{ marginTop: 6 }}>
                    <Button onClick={() => call('PUT', '', { markdown: text })} disabled={text === pm.markdown}>{t('sre.pm.save')}</Button>
                    <Button type="primary" onClick={async () => { if (text !== pm.markdown) await call('PUT', '', { markdown: text }); await call('POST', '/approve'); }}>
                        {t('sre.pm.approve')}</Button>
                </Space>
            )}
        </section>
    );
}

/** The SRE agent on an incident: investigate, its proposals with Approve and Reject, a chat scoped to the incident,
 *  and the postmortem it drafts when the incident is resolved. */
export function SreIncidentPanel({ id, resolved, onChange }: { id: string; resolved: boolean; onChange: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<SreIncident>(`/v1/sre/incidents/${id}`, (c, signal) =>
        c.request('GET', `/v1/sre/incidents/${encodeURIComponent(id)}`, undefined, { signal }), { staleMs: 2_000, pollMs: 5_000 });
    const [question, setQuestion] = useState('');
    const [asking, setAsking] = useState(false);
    const refresh = () => { void q.refresh(); onChange(); };
    if (q.error) return <ErrorNotice error={q.error} area={t('sre.title')} />;
    const v = q.data;
    if (!v) return <Spin />;
    const running = v.runs.some((r) => r.status === 'running');
    const chat = v.runs.filter((r) => r.kind === 'ask').slice().reverse();
    const investigate = async () => {
        try { await client.request('POST', `/v1/sre/incidents/${encodeURIComponent(id)}/investigate`, {}); refresh(); }
        catch (e) { notifyError(e); }
    };
    const ask = async () => {
        setAsking(true);
        try {
            await client.request('POST', `/v1/sre/incidents/${encodeURIComponent(id)}/ask`, { question });
            setQuestion('');
            refresh();
        } catch (e) {
            notifyError(e);
        } finally {
            setAsking(false);
        }
    };
    const draftPm = async () => {
        try { await client.request('POST', `/v1/sre/incidents/${encodeURIComponent(id)}/postmortem/draft`, {}); refresh(); }
        catch (e) { notifyError(e); }
    };
    const last = v.runs.find((r) => r.kind !== 'ask');
    return (
        <section aria-label={t('sre.title')}>
            <SectionTitle>{t('sre.title')}</SectionTitle>
            {!v.status.ready && <Alert type="info" showIcon message={t('sre.notReady')} description={v.status.why} style={{ marginBottom: 8 }} />}
            <Space wrap style={{ marginBottom: 8 }}>
                <Button onClick={investigate} disabled={!v.status.enabled || running} loading={running}>{t('sre.investigate')}</Button>
                {last && <Tag color={STATUS_COLOR[last.status]}>{t(`sre.run.${last.status}` as never)}</Tag>}
                {last?.error && <Muted>{last.error}</Muted>}
            </Space>
            <Typography.Text strong>{t('sre.proposals')}</Typography.Text>
            {v.actions.length === 0 ? <Muted style={{ display: 'block' }}>{t('sre.noProposals')}</Muted> : (
                <List size="small" dataSource={v.actions} renderItem={(a) => <Proposal key={a.id} a={a} onDone={refresh} />} />
            )}
            <div style={{ marginTop: 12 }}>
                <Typography.Text strong>{t('sre.ask')}</Typography.Text>
                <List size="small" dataSource={chat} locale={{ emptyText: t('sre.askEmpty') }} renderItem={(r) => (
                    <List.Item>
                        <div>
                            <div><strong>{actorName(r.asked_by ?? r.acts_for)}:</strong> {r.question}</div>
                            <div>{r.status === 'running' ? <Spin size="small" /> : (r.answer || r.error)}</div>
                        </div>
                    </List.Item>
                )} />
                <Space.Compact style={{ width: '100%' }}>
                    <Input aria-label={t('sre.askPlaceholder')} placeholder={t('sre.askPlaceholder')} value={question}
                        onChange={(e) => setQuestion(e.target.value)} onPressEnter={() => question.trim() && ask()} />
                    <Button type="primary" disabled={!question.trim() || !v.status.ready} loading={asking} onClick={ask}>{t('sre.askButton')}</Button>
                </Space.Compact>
            </div>
            {(resolved || v.postmortem) && (
                <div style={{ marginTop: 12 }}>
                    <Typography.Text strong>{t('sre.postmortem')}</Typography.Text>
                    {v.postmortem ? <PostmortemEditor key={v.postmortem.markdown} id={id} pm={v.postmortem} onDone={refresh} /> : (
                        <div><Button onClick={draftPm}>{t('sre.pm.draft')}</Button></div>
                    )}
                </div>
            )}
        </section>
    );
}

/** Diagnose on a managed Postgres instance: the SRE agent's Postgres helper (why is it slow, is it healthy). */
export function DiagnoseButton({ instance }: { instance: string }) {
    const { client } = useConsumerApi();
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const [question, setQuestion] = useState('');
    const [out, setOut] = useState<{ report: Record<string, any>; answer: string | null; answer_unavailable: string | null } | null>(null);
    const run = async () => {
        setBusy(true);
        try {
            setOut(await client.request('POST', `/v1/sre/instances/${encodeURIComponent(instance)}/diagnose`, { question }));
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    const r = out?.report;
    return (
        <>
            <Button onClick={() => { setOpen(true); if (!out) void run(); }}>Diagnose</Button>
            <Modal open={open} title={`Diagnose ${instance}`} onCancel={() => setOpen(false)} footer={null} width={760}>
                <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
                    <Input aria-label="Ask about this instance" placeholder="Why is it slow? Is it healthy?" value={question}
                        onChange={(e) => setQuestion(e.target.value)} onPressEnter={run} />
                    <Button type="primary" loading={busy} onClick={run}>Diagnose</Button>
                </Space.Compact>
                {busy && !r && <Spin />}
                {r && (
                    <section aria-label={`Diagnosis of ${instance}`}>
                        <Space wrap style={{ marginBottom: 8 }}>
                            <Tag color={r.verdict === 'healthy' ? 'green' : r.verdict === 'down' ? 'red' : 'orange'}>{r.verdict}</Tag>
                            {r.connections && <Muted>{`${r.connections.total} of ${r.connections.max} connections`}</Muted>}
                        </Space>
                        {out?.answer && <Alert type="info" message={out.answer} style={{ marginBottom: 8 }} />}
                        {out?.answer_unavailable && question && <Alert type="warning" showIcon message={out.answer_unavailable} style={{ marginBottom: 8 }} />}
                        {(r.problems ?? []).map((p: string) => <Alert key={p} type="warning" showIcon message={p} style={{ marginBottom: 6 }} />)}
                        <SectionTitle>Findings</SectionTitle>
                        {(r.findings ?? []).length === 0 ? <Muted>Nothing found.</Muted> : (
                            <List size="small" dataSource={r.findings} renderItem={(f: any) => <List.Item><Tag>{f.kind}</Tag>{f.text}</List.Item>} />
                        )}
                        <SectionTitle>Suggested remediations</SectionTitle>
                        <Muted style={{ display: 'block', marginBottom: 6 }}>
                            Nothing is applied: a change is proposed as a change request someone approves; an operation is approved from an incident.
                        </Muted>
                        <List size="small" dataSource={r.suggestions ?? []} locale={{ emptyText: 'None' }} renderItem={(s: any) => (
                            <List.Item>
                                <div style={{ width: '100%' }}>
                                    <Space><Tag color={s.kind === 'change_request' ? 'geekblue' : 'purple'}>{s.kind === 'change_request' ? 'change request' : 'runbook action'}</Tag>
                                        <strong>{s.title}</strong></Space>
                                    <p style={{ margin: '4px 0' }}>{s.rationale}</p>
                                    {s.bundle_yaml && (
                                        <>
                                            <pre style={{ fontSize: 12, margin: 0 }}>{s.bundle_yaml}</pre>
                                            <Button size="small" onClick={async () => {
                                                try {
                                                    await client.request('POST', '/v1/iac/changes', { title: s.title, intent: s.rationale, bundle_yaml: s.bundle_yaml });
                                                    message.success('Proposed: it waits for someone to approve it');
                                                } catch (e) { notifyError(e); }
                                            }}>Propose as a change request</Button>
                                        </>
                                    )}
                                </div>
                            </List.Item>
                        )} />
                        {(r.slow_queries ?? []).length > 0 && (
                            <>
                                <SectionTitle>Slow statements</SectionTitle>
                                <List size="small" dataSource={r.slow_queries} renderItem={(s: any) => (
                                    <List.Item><code style={{ fontSize: 12 }}>{s.query}</code><Muted>{` ${s.mean_ms} ms × ${s.calls}`}</Muted></List.Item>
                                )} />
                            </>
                        )}
                        {(r.errors ?? []).length > 0 && <Muted>{`Not read: ${r.errors.map((e: any) => e.step).join(', ')}`}</Muted>}
                    </section>
                )}
            </Modal>
        </>
    );
}
