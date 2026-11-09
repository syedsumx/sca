import { Alert, Button, Descriptions, Progress, Radio, Select, Space, Spin, Steps, Tag } from 'antd';
import React, { useEffect, useMemo, useRef, useState } from 'react';

import { useConsumerApi } from '../api/hooks';
import { Muted } from '../components/tokens';
import { CatalogTables, DiffPart, Estimate, OntologyRun, RunDiff, ontologyApi } from './api';
import { INITIAL, STEPS, WizardState, back, canNext, isFinal, next, percent, phaseLabel, resumable, scopeBody } from './wizard';

function DiffSection({ title, part }: { title: string; part: DiffPart }) {
    return (
        <section aria-label={`${title} changed`} style={{ marginTop: 8 }}>
            <strong>{title}</strong>{' '}
            <Muted>
                {part.counts.added} added · {part.counts.removed} removed · {part.counts.changed} changed · {part.counts.unchanged} unchanged
            </Muted>
            <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {part.added.slice(0, 20).map((x) => <li key={`a${x.id}`}><Tag color="green">added</Tag>{x.label}</li>)}
                {part.removed.slice(0, 20).map((x) => <li key={`r${x.id}`}><Tag color="red">removed</Tag>{x.label}</li>)}
                {part.changed.slice(0, 20).map((x) => (
                    <li key={`c${x.id}`}>
                        <Tag color="gold">changed</Tag>{x.label}: {Object.keys(x.changes).join(', ')}
                    </li>
                ))}
            </ul>
        </section>
    );
}

export function RunDiffView({ diff }: { diff: RunDiff }) {
    return (
        <div data-testid="run-diff">
            <Muted>
                Version {diff.version}
                {diff.against ? ` compared with run ${diff.against}` : ' (the first compiled ontology: everything is new)'}. Only what you may see.
            </Muted>
            <DiffSection title="Terms" part={diff.terms} />
            <DiffSection title="Relationships" part={diff.relationships} />
            <DiffSection title="Metrics" part={diff.metrics} />
        </div>
    );
}

/** Compile the ontology in four steps: what to compile (everything, schemas or tables), incremental or full, what it
 * will cost, then the run as it goes, with cancel and resume, and what changed since the last run. */
export function CompileWizard({ onFinished }: { onFinished?: () => void }) {
    const { client } = useConsumerApi();
    const api = useMemo(() => ontologyApi(client), [client]);
    const [s, setS] = useState<WizardState>(INITIAL);
    const [catalog, setCatalog] = useState<CatalogTables | null>(null);
    const [estimate, setEstimate] = useState<Estimate | null>(null);
    const [run, setRun] = useState<OntologyRun | null>(null);
    const [diff, setDiff] = useState<RunDiff | null>(null);
    const [imported, setImported] = useState<{ new: number; updated: number } | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const finished = useRef<string | null>(null);

    useEffect(() => {
        api.catalog().then(setCatalog).catch((e) => setError((e as Error).message));
    }, [api]);

    useEffect(() => {
        if (s.step !== 2) return;
        setEstimate(null);
        setError(null);
        api.estimate(s.mode, scopeBody(s)).then(setEstimate).catch((e) => setError((e as Error).message));
    }, [api, s]);

    // Follow the run until it ends; a completed run's proposals are imported and its diff shown.
    useEffect(() => {
        if (!run || isFinal(run)) return undefined;
        const t = window.setTimeout(() => {
            api.run(run.id).then(setRun).catch((e) => setError((e as Error).message));
        }, 800);
        return () => window.clearTimeout(t);
    }, [api, run]);
    useEffect(() => {
        if (!run || run.state !== 'completed' || finished.current === run.id) return;
        finished.current = run.id;
        (async () => {
            try {
                const out = await api.importRun(run.id);
                setImported({ new: out.new, updated: out.updated });
                setDiff(await api.diff(run.id));
                onFinished?.();
            } catch (e) {
                setError((e as Error).message);
            }
        })();
    }, [api, run, onFinished]);

    const start = async () => {
        setBusy(true);
        setError(null);
        try {
            const out = await api.compile(s.mode, scopeBody(s));
            setRun(out.run);
            setS(next(s));
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    const act = async (fn: () => Promise<unknown>) => {
        setBusy(true);
        setError(null);
        try {
            await fn();
            if (run) setRun(await api.run(run.id));
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const tableOptions = (catalog?.tables ?? []).map((t) => ({ value: t.id, label: `${t.schema ? `${t.schema}.` : ''}${t.name.split('.').pop()}` }));
    return (
        <div data-testid="compile-wizard">
            <Steps size="small" current={s.step} items={STEPS.map((title) => ({ title }))} style={{ marginBottom: 16 }} />
            {error && <Alert style={{ marginBottom: 8 }} type="error" showIcon message={error} />}
            {s.step === 0 && (
                <section aria-label="Scope">
                    <Radio.Group value={s.scope} onChange={(e) => setS({ ...s, scope: e.target.value })}>
                        <Space direction="vertical">
                            <Radio value="all">Every table ({catalog ? catalog.tables.length : '…'} you may see)</Radio>
                            <Radio value="schemas">Some schemas</Radio>
                            <Radio value="tables">Some tables</Radio>
                        </Space>
                    </Radio.Group>
                    {s.scope === 'schemas' && (
                        <Select mode="multiple" showSearch aria-label="Schemas" placeholder="Pick schemas" style={{ width: '100%', marginTop: 8 }}
                            value={s.schemas} onChange={(schemas) => setS({ ...s, schemas })}
                            options={(catalog?.schemas ?? []).map((x) => ({ value: x, label: x }))} />
                    )}
                    {s.scope === 'tables' && (
                        <Select mode="multiple" showSearch aria-label="Tables" placeholder="Pick tables (type to search)" style={{ width: '100%', marginTop: 8 }}
                            value={s.tables} onChange={(tables) => setS({ ...s, tables })} optionFilterProp="label" options={tableOptions} />
                    )}
                    <p><Muted>Tables outside the scope keep what the last run made of them.</Muted></p>
                </section>
            )}
            {s.step === 1 && (
                <section aria-label="Mode">
                    <Radio.Group value={s.mode} onChange={(e) => setS({ ...s, mode: e.target.value })}>
                        <Space direction="vertical">
                            <Radio value="incremental">Incremental: compile only the tables that changed since the last run</Radio>
                            <Radio value="full">Full: compile every table in scope again</Radio>
                        </Space>
                    </Radio.Group>
                </section>
            )}
            {s.step === 2 && (
                <section aria-label="Cost">
                    {!estimate && !error && <Spin />}
                    {estimate && (
                        <Descriptions size="small" column={1} bordered>
                            <Descriptions.Item label="Tables to compile">{estimate.to_process}</Descriptions.Item>
                            <Descriptions.Item label="Reused unchanged">{estimate.reused}</Descriptions.Item>
                            {estimate.scope !== null && <Descriptions.Item label="Outside the scope (kept)">{estimate.out_of_scope}</Descriptions.Item>}
                            <Descriptions.Item label="Model calls">
                                {estimate.model.chat_configured
                                    ? `up to ${estimate.model.chat_calls_max} (${estimate.model.chat_calls_per_table} a table) and ${estimate.model.embedding_calls_max} embedding calls`
                                    : 'none: no model is configured, so names and descriptions come from heuristics'}
                            </Descriptions.Item>
                        </Descriptions>
                    )}
                    {estimate?.model.chat_configured && <Muted style={{ fontSize: 12 }}>{estimate.model.note}. The calls go through the AI gateway under the compiler's identity and count against its quota.</Muted>}
                </section>
            )}
            {s.step === 3 && run && (
                <section aria-label="Run">
                    <Space direction="vertical" style={{ width: '100%' }}>
                        <div>
                            Run <code>{run.id}</code> (version {run.version}, {run.mode}): <strong data-testid="run-state">{phaseLabel(run)}</strong>
                        </div>
                        <Progress percent={percent(run)} status={run.state === 'failed' ? 'exception' : run.state === 'completed' ? 'success' : 'active'}
                            aria-label="Compilation progress" />
                        {run.error && <Alert type="error" showIcon message={run.error} />}
                        <Space>
                            {run.state === 'running' && <Button danger loading={busy} onClick={() => void act(() => api.cancel(run.id))}>Cancel</Button>}
                            {resumable(run) && <Button type="primary" loading={busy} onClick={() => void act(() => api.resume(run.id))}>Resume</Button>}
                            {isFinal(run) && <Button onClick={() => { setS(INITIAL); setRun(null); setDiff(null); setImported(null); }}>New run</Button>}
                        </Space>
                        {imported && <Alert type="success" showIcon message={`${imported.new} new proposals and ${imported.updated} updated in the review queue`} />}
                        {diff && <RunDiffView diff={diff} />}
                    </Space>
                </section>
            )}
            {s.step < 3 && (
                <Space style={{ marginTop: 16 }}>
                    {s.step > 0 && <Button onClick={() => setS(back(s))}>Back</Button>}
                    {s.step < 2 && <Button type="primary" disabled={!canNext(s)} onClick={() => setS(next(s))}>Next</Button>}
                    {s.step === 2 && <Button type="primary" disabled={!canNext(s, estimate)} loading={busy} onClick={() => void start()}>Run</Button>}
                </Space>
            )}
        </div>
    );
}
