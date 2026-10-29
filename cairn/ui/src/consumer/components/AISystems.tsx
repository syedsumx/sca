import { Alert, Button, Checkbox, Input, List, Radio, Select, Space, Statistic, Tag, Typography } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { AIAssessment, AISystem, AISystemInput, AISystemsReport, AITier, Purpose } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { download } from './BulkEdit';
import { Surface } from './tokens';

export const TIER_COLOR: Record<AITier, string> = {
    unacceptable: 'red',
    high: 'volcano',
    limited: 'gold',
    minimal: 'green',
};
const STATE_COLOR: Record<string, string> = {
    DRAFT: 'default',
    IN_REVIEW: 'blue',
    APPROVED: 'green',
    REJECTED: 'red',
    RETIRED: 'default',
};

const lines = (s: string) =>
    s
        .split(/\s+/)
        .map((x) => x.trim())
        .filter(Boolean);

function empty(): AISystemInput {
    return { name: '', purpose: '', models: [], datasets: [], vendor: null, data_purpose_id: null, answers: {} };
}

/** The risk assessment, rendered from the configured template. */
export function AssessmentFields({
    assessment,
    answers,
    onChange,
}: {
    assessment: AIAssessment;
    answers: Record<string, unknown>;
    onChange: (a: Record<string, unknown>) => void;
}) {
    const set = (id: string, v: unknown) => onChange({ ...answers, [id]: v });
    return (
        <fieldset aria-label={assessment.name} style={{ border: 0, padding: 0 }}>
            <Typography.Title level={5}>{assessment.name}</Typography.Title>
            {assessment.questions.map((q) => (
                <div key={q.id} style={{ marginBottom: 16 }}>
                    <div style={{ fontWeight: 500, marginBottom: 6 }}>{q.title}</div>
                    {q.type === 'multi' && (
                        <Checkbox.Group
                            aria-label={q.title}
                            value={(answers[q.id] as string[]) ?? []}
                            onChange={(v) => set(q.id, v)}
                            style={{ display: 'grid', gap: 4 }}
                            options={q.options.map((o) => ({ value: o.value, label: o.label }))}
                        />
                    )}
                    {q.type === 'boolean' && (
                        <Radio.Group
                            aria-label={q.title}
                            value={answers[q.id] === undefined ? undefined : answers[q.id] ? 'yes' : 'no'}
                            onChange={(e) => set(q.id, e.target.value === 'yes')}
                        >
                            <Radio value="yes">Yes</Radio>
                            <Radio value="no">No</Radio>
                        </Radio.Group>
                    )}
                    {q.type === 'text' && (
                        <Input.TextArea
                            aria-label={q.title}
                            rows={2}
                            value={(answers[q.id] as string) ?? ''}
                            onChange={(e) => set(q.id, e.target.value)}
                        />
                    )}
                </div>
            ))}
        </fieldset>
    );
}

function Editor({ system, onDone }: { system: AISystem | null; onDone: () => void }) {
    const { client } = useConsumerApi();
    const assessment = useDheQuery<AIAssessment>('/governance/ai-systems/assessment', (c, signal) =>
        c.aiAssessment({ signal }),
    );
    const purposes = useDheQuery<Purpose[]>('/governance/purposes', (c, signal) => c.listPurposes({ signal }));
    const [form, setForm] = useState<AISystemInput>(system ?? empty());
    const [error, setError] = useState<string | null>(null);
    const save = async () => {
        setError(null);
        try {
            await client.saveAISystem(form, system?.id);
            onDone();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };
    return (
        <Surface style={{ padding: 18, marginBottom: 16 }} aria-label="AI system editor">
            <Space direction="vertical" style={{ width: '100%' }}>
                <Input
                    aria-label="Name"
                    placeholder="Name"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
                <Input.TextArea
                    aria-label="Purpose"
                    placeholder="What it is for, in plain words"
                    rows={2}
                    value={form.purpose}
                    onChange={(e) => setForm({ ...form, purpose: e.target.value })}
                />
                <Input.TextArea
                    aria-label="Datasets"
                    placeholder="Datasets it is trained on or reads (urns, one per line)"
                    rows={2}
                    value={form.datasets.join('\n')}
                    onChange={(e) => setForm({ ...form, datasets: lines(e.target.value) })}
                />
                <Input.TextArea
                    aria-label="Models"
                    placeholder="Models it runs (urns, one per line)"
                    rows={1}
                    value={form.models.join('\n')}
                    onChange={(e) => setForm({ ...form, models: lines(e.target.value) })}
                />
                <Input
                    aria-label="Vendor"
                    placeholder="Vendor (for bought-in systems)"
                    value={form.vendor ?? ''}
                    onChange={(e) => setForm({ ...form, vendor: e.target.value || null })}
                />
                <Select
                    aria-label="Approved purpose for restricted data"
                    placeholder="Approved purpose for restricted data"
                    allowClear
                    value={form.data_purpose_id ?? undefined}
                    onChange={(v) => setForm({ ...form, data_purpose_id: v ?? null })}
                    options={(purposes.data ?? []).map((p) => ({ value: p.id, label: p.name }))}
                    style={{ width: '100%' }}
                />
                {assessment.data && (
                    <AssessmentFields
                        assessment={assessment.data}
                        answers={form.answers}
                        onChange={(answers) => setForm({ ...form, answers })}
                    />
                )}
                {error && <Alert type="error" showIcon message={error} />}
                <Space>
                    <Button type="primary" onClick={save} disabled={!form.name.trim() || !form.purpose.trim()}>
                        Save
                    </Button>
                    <Button onClick={onDone}>Cancel</Button>
                </Space>
            </Space>
        </Surface>
    );
}

function Report() {
    const { client } = useConsumerApi();
    const q = useDheQuery<AISystemsReport>('/governance/ai-systems/report', (c, signal) =>
        c.aiSystemsReport({ signal }),
    );
    if (!q.data) return null;
    const r = q.data;
    return (
        <Surface style={{ padding: 18, marginBottom: 16 }} aria-label="AI system inventory">
            <Space size={32} wrap>
                <Statistic title="AI systems" value={r.total} />
                {(['unacceptable', 'high', 'limited', 'minimal'] as AITier[]).map((t) => (
                    <Statistic key={t} title={`${t} risk`} value={r.by_tier[t]} />
                ))}
                <Statistic title="High risk, not approved" value={r.high_risk_not_approved.length} />
                <Button onClick={async () => download('ai-systems.csv', await client.aiSystemsCsv())}>
                    Download inventory
                </Button>
            </Space>
            {r.violations.length > 0 && (
                <Alert
                    type="error"
                    showIcon
                    style={{ marginTop: 12 }}
                    message={`${r.violations.length} use${r.violations.length > 1 ? 's' : ''} of restricted data without an approved purpose`}
                    description={r.violations.map((v) => (
                        <div key={`${v.system}${v.dataset}`}>
                            {v.name}: {v.dataset} ({v.reason})
                        </div>
                    ))}
                />
            )}
            {(r.undeclared_use ?? []).length > 0 && (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 12 }}
                    message="AI systems reading datasets they did not declare"
                    description={(r.undeclared_use ?? []).map((u) => (
                        <div key={u.system}>
                            {u.name}: {u.datasets.join(', ')}
                        </div>
                    ))}
                />
            )}
        </Surface>
    );
}

/** Governance → AI systems: the inventory, risk assessments and approvals. */
export function AISystemsAdmin() {
    const { client } = useConsumerApi();
    const session = useDheSession();
    const q = useDheQuery<AISystem[]>('/governance/ai-systems', (c, signal) => c.listAISystems({ signal }));
    const [editing, setEditing] = useState<AISystem | 'new' | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [notes, setNotes] = useState<Record<string, string>>({});
    const me = session.config?.user.urn;
    const approver = !!session.config?.is_admin;

    const act = async (fn: () => Promise<unknown>) => {
        setError(null);
        try {
            await fn();
            q.refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    return (
        <section aria-label="AI systems">
            {approver && <Report />}
            {editing ? (
                <Editor
                    system={editing === 'new' ? null : editing}
                    onDone={() => {
                        setEditing(null);
                        q.refresh();
                    }}
                />
            ) : (
                <Button type="primary" onClick={() => setEditing('new')} style={{ marginBottom: 12 }}>
                    Register an AI system
                </Button>
            )}
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <List
                dataSource={q.data ?? []}
                locale={{ emptyText: 'No AI systems registered yet.' }}
                renderItem={(s) => (
                    <List.Item
                        aria-label={`AI system ${s.name}`}
                        actions={[
                            <Button key="edit" size="small" onClick={() => setEditing(s)}>
                                Edit
                            </Button>,
                            ...(s.state === 'DRAFT' || s.state === 'REJECTED'
                                ? [
                                      <Button
                                          key="submit"
                                          size="small"
                                          onClick={() => act(() => client.submitAISystem(s.id))}
                                      >
                                          Submit for review
                                      </Button>,
                                  ]
                                : []),
                            ...(s.state === 'IN_REVIEW' && approver && s.owner !== me
                                ? [
                                      <Input
                                          key="note"
                                          size="small"
                                          aria-label={`Decision note for ${s.name}`}
                                          placeholder="Note"
                                          value={notes[s.id] ?? ''}
                                          onChange={(e) => setNotes({ ...notes, [s.id]: e.target.value })}
                                          style={{ width: 160 }}
                                      />,
                                      <Button
                                          key="approve"
                                          size="small"
                                          type="primary"
                                          onClick={() =>
                                              act(() => client.decideAISystem(s.id, true, notes[s.id] ?? ''))
                                          }
                                      >
                                          Approve
                                      </Button>,
                                      <Button
                                          key="reject"
                                          size="small"
                                          danger
                                          onClick={() =>
                                              act(() => client.decideAISystem(s.id, false, notes[s.id] ?? ''))
                                          }
                                      >
                                          Reject
                                      </Button>,
                                  ]
                                : []),
                        ]}
                    >
                        <List.Item.Meta
                            title={
                                <span>
                                    {s.name} <Tag color={TIER_COLOR[s.tier]}>{s.tier} risk</Tag>
                                    <Tag color={STATE_COLOR[s.state]}>{s.state.replace('_', ' ').toLowerCase()}</Tag>
                                    {s.violations.length > 0 && <Tag color="red">restricted data without purpose</Tag>}
                                    {(s.undeclared_datasets ?? []).length > 0 && (
                                        <Tag color="orange" title={(s.undeclared_datasets ?? []).join('\n')}>
                                            reads {(s.undeclared_datasets ?? []).length} undeclared dataset
                                            {(s.undeclared_datasets ?? []).length > 1 ? 's' : ''}
                                        </Tag>
                                    )}
                                    {s.missing.length > 0 && <Tag>assessment incomplete</Tag>}
                                </span>
                            }
                            description={s.purpose}
                        />
                    </List.Item>
                )}
            />
        </section>
    );
}
