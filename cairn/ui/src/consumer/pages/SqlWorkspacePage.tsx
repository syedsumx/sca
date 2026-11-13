import { Alert, Button, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, Tooltip, message } from 'antd';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { SqlLanguage } from 'sql-formatter';

import { BiAssetButtons } from '../components/BiAdmin';
import { QueryDetails } from '../components/QueryDetails';
import { ResultGrid } from '../components/ResultGrid';
import { FixWithAssistant, SqlAssistantDrawer } from '../components/SqlAssistant';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { BiFolder, ChartSpec, DataResult, SavedQuery, SchemaTable, SqlRun, SystemTablesInfo } from '../api/types';
import { ResultChart, toCsv } from '../components/ResultChart';
import { CHART_TYPES } from '../api/types';
import { SqlEditor, type SqlEditorHandle } from '../components/SqlEditor';
import { WorkspaceFiles } from '../components/WorkspaceFiles';
import { useDheSession } from '../context/CairnProvider';
import { closeTab, type EditorTab, isDirty, maySave, openSaved, refreshed, remember, restore, saved as savedTab, untitled } from '../sql/workspaceTabs';
import { statementTitle, toRun } from '../sql/statements';
import { type PagedProgress, runPaged, StatementStopped } from '../sql/pagedRun';
import { bytes, type ColumnFilters } from '../sql/resultStats';
import { Muted, Page, Surface } from '../components/tokens';
import styled from 'styled-components';
import { TitleRow } from '../guide/PageHeader';
import { describeError, notifyError } from '../components/ErrorNotice';
import { When } from '../utils/time';
import { StatusBadge } from '../components/StatusBadge';

/** The schema browser beside the editor; on a narrow screen it stacks above it, and the editor keeps to the viewport. */
const SqlGrid = styled.div`
    display: grid;
    grid-template-columns: minmax(220px, 1fr) 3fr;
    gap: 16px;
    margin-top: 16px;
    > * {
        min-width: 0;
    }
    .sql-main textarea {
        max-width: 100%;
    }
    @media (max-width: 900px) {
        grid-template-columns: minmax(0, 1fr);
        .sql-schema {
            max-height: 260px;
            overflow: auto;
        }
    }
`;

const ago = (ms: number) => <When value={ms} />;

/** One statement of a run and how it went (a result tab). */
export interface StatementRun {
    key: string;
    text: string;
    from: number;
    to: number;
    status: 'queued' | 'running' | 'succeeded' | 'failed' | 'stopped' | 'skipped';
    result?: DataResult;
    error?: string;
    /** Read page by page on Trino: how far it has got. */
    progress?: PagedProgress;
}

const RUN_STATE: Record<StatementRun['status'], { state: string; label: string }> = {
    queued: { state: 'waiting', label: 'Waiting' },
    running: { state: 'running', label: 'Running' },
    succeeded: { state: 'succeeded', label: 'Done' },
    failed: { state: 'failed', label: 'Failed' },
    stopped: { state: 'stopped', label: 'Stopped' },
    skipped: { state: 'skipped', label: 'Not run' },
};

/** The formatter's dialect for a connection's platform (standard SQL when it has none of its own). */
export function sqlDialect(platform: string | undefined): SqlLanguage {
    const known: Record<string, SqlLanguage> = {
        trino: 'trino', presto: 'trino', postgres: 'postgresql', postgresql: 'postgresql', mysql: 'mysql',
        mariadb: 'mariadb', snowflake: 'snowflake', bigquery: 'bigquery', redshift: 'redshift', sqlite: 'sqlite',
        databricks: 'spark', spark: 'spark', mssql: 'transactsql', duckdb: 'duckdb', clickhouse: 'clickhouse',
    };
    return known[(platform ?? '').toLowerCase()] ?? 'sql';
}

/** The SQL workspace: browse what you may query, run read-only SQL as yourself, chart and download the
 * result, keep a history, and save queries for yourself or everyone. */
export function SqlWorkspacePage() {
    const { client } = useConsumerApi();
    const conns = useDheQuery('/data/sources', (c, signal) => c.dataConnections({ signal }));
    const session = useDheSession();
    const me = session.config?.user?.urn;
    const [limit, setLimit] = useState(200);
    // Trino connections run page by page (L163): rows as they come, a real cancel, and a warehouse to run on.
    const targets = useDheQuery('/v1/statements/targets', (c, signal) => c.statementTargets({ signal }).catch(() => null));
    const [warehouse, setWarehouse] = useState<string | undefined>(undefined);
    const [filters, setFilters] = useState<ColumnFilters>({});
    const [explaining, setExplaining] = useState<string | null>(null);   // the statement whose plan is shown
    const [assisting, setAssisting] = useState<string | null>(null);     // the assistant, with the statement at the cursor
    const [running, setRunning] = useState(false);
    const stopper = useRef<AbortController | null>(null);
    const editor = useRef<SqlEditorHandle>(null);
    const [saving, setSaving] = useState(false);
    const [form, setForm] = useState({ name: '', description: '', shared: false, folder: undefined as string | undefined });
    const [tick, setTick] = useState(0);
    const [side, setSide] = useState<'files' | 'objects'>('files');

    // The open files (L163): tabs, each a saved query or an untitled draft, with its own results.
    const [tabs, setTabs] = useState<EditorTab[]>([]);
    const [activeTab, setActiveTab] = useState<string | undefined>();
    const [results, setResults] = useState<Record<string, { runs: StatementRun[]; active?: string }>>({});
    const tab = tabs.find((t) => t.key === activeTab);
    const patchTab = useCallback((key: string | undefined, patch: Partial<EditorTab>) => {
        if (!key) return;
        setTabs((ts) => ts.map((t) => (t.key === key ? { ...t, ...patch } : t)));
    }, []);
    const sql = tab?.sql ?? '';
    const setSql = useCallback((v: string) => patchTab(activeTab, { sql: v }), [activeTab, patchTab]);
    const defaultConnection = conns.data?.[0]?.connection;
    const connection = tab?.connection ?? defaultConnection;
    const paged = !!connection && !!targets.data?.connections.includes(connection);
    const maxRows = paged ? 100_000 : 1000;
    const setConnection = (c: string) => patchTab(activeTab, { connection: c });
    const chart: ChartSpec = tab?.chart?.type ? tab.chart : { type: 'table' };
    const setChart = (c: ChartSpec) => patchTab(activeTab, { chart: c });
    const runs = (activeTab && results[activeTab]?.runs) || [];
    const activeRun = activeTab ? results[activeTab]?.active : undefined;
    const setActiveRun = (k: string) => activeTab && setResults((r) => ({ ...r, [activeTab]: { ...r[activeTab], active: k } }));

    const schema = useDheQuery<SchemaTable[]>(`/sql/schema?${connection ?? ''}`, (c, signal) =>
        connection ? c.sqlSchema(connection, { signal }) : Promise.resolve([]),
    );
    // Cairn's system tables (cairn_system.*): listed when they are set up; read like any table.
    const system = useDheQuery<SystemTablesInfo | null>('/v1/system-tables', (c, signal) =>
        c.systemTables({ signal }).catch(() => null),
    );
    const history = useDheQuery<SqlRun[]>(`/sql/history#${tick}`, (c, signal) => c.sqlHistory({ signal }));
    const saved = useDheQuery<SavedQuery[]>(`/sql/saved#${tick}`, (c, signal) => c.savedQueries({ signal }));
    const folders = useDheQuery<BiFolder[]>(`/sql/folders#${tick}`, (c, signal) => c.biFolders({ signal }).catch(() => []));

    // The tabs open last time on this device, once the saved queries are known; else one untitled tab.
    const restored = useRef(false);
    useEffect(() => {
        // Kept per person: wait until who is signed in is known, or the tabs would be read from (and written to) nobody's.
        if (restored.current || !saved.data || (session.loading && !me)) return;
        restored.current = true;
        const back = restore(me, saved.data);
        if (back.tabs.length) {
            setTabs(back.tabs);
            setActiveTab(back.active);
        } else {
            const first = untitled([]);
            setTabs([first]);
            setActiveTab(first.key);
        }
    }, [saved.data, me, session.loading]);
    useEffect(() => {
        if (restored.current) remember(me, tabs, activeTab);
    }, [tabs, activeTab, me]);
    // Renamed, moved, deleted or reshared since it was opened: the tabs follow Cairn (a later save keeps the new name).
    const listed = useRef<Set<string>>(new Set());
    useEffect(() => {
        if (!saved.data) return;
        const before = listed.current;
        listed.current = new Set(saved.data.map((q) => q.id));
        if (restored.current) setTabs((ts) => refreshed(ts, saved.data!, before));
    }, [saved.data]);

    // Autosave: a saved query the person may change is saved a moment after they stop typing; its versions coalesce.
    const savingNow = useRef<Record<string, boolean>>({});
    useEffect(() => {
        const t = tab;
        if (!t || !t.savedId || !isDirty(t) || !maySave(t) || !t.connection || !t.sql.trim()) return undefined;
        const timer = window.setTimeout(async () => {
            if (savingNow.current[t.key]) return;
            savingNow.current[t.key] = true;
            patchTab(t.key, { saving: true, error: undefined });
            try {
                const q = await client.saveQuery({ name: t.name, description: t.description ?? '', sql: t.sql,
                    connection: t.connection, shared: t.shared, chart: t.chart, autosave: true }, t.savedId);
                setTabs((ts) => savedTab(ts, t.key, q, t.sql));
            } catch (e) {
                patchTab(t.key, { saving: false, error: describeError(e) });
            } finally {
                savingNow.current[t.key] = false;
            }
        }, 1500);
        return () => window.clearTimeout(timer);
    }, [tab, client, patchTab]);

    const newTab = (text = '', conn?: string) => {
        const t = untitled(tabs, text, conn);
        setTabs((ts) => [...ts, t]);
        setActiveTab(t.key);
    };
    const openFile = (q: SavedQuery) => {
        const out = openSaved(tabs, q);
        setTabs(out.tabs);
        setActiveTab(out.key);
    };
    const close = (key: string) => {
        const t = tabs.find((x) => x.key === key);
        const go = () => {
            const out = closeTab(tabs, key, activeTab);
            if (!out.tabs.length) {
                const first = untitled([]);
                setTabs([first]);
                setActiveTab(first.key);
            } else {
                setTabs(out.tabs);
                setActiveTab(out.active);
            }
            setResults((r) => {
                const { [key]: _gone, ...rest } = r;
                return rest;
            });
        };
        if (t && !t.savedId && isDirty(t)) {
            Modal.confirm({ title: `Close ${t.name}?`, content: 'It has not been saved: its text is lost.', okText: 'Close',
                okButtonProps: { danger: true }, onOk: go });
        } else go();
    };
    const current = tab?.savedId ? (saved.data ?? []).find((q) => q.id === tab.savedId) ?? null : null;

    // The statement at the cursor, the selection's statements, or all; one after another, each its own result, as
    // the person; the first that fails stops the rest (and is selected in the editor).
    const run = useCallback(async (mode: 'current' | 'all' = 'current') => {
        if (!connection || running) return;
        const picked = toRun(sql, editor.current?.selection() ?? { from: 0, to: 0 }, mode);
        if (!picked.length) return;
        const stamp = Date.now();
        const tabKey = activeTab as string;
        const list: StatementRun[] = picked.map((st, i) => ({ key: `${stamp}-${i}`, ...st, status: 'queued' }));
        const show = (active: string) => setResults((r) => ({ ...r, [tabKey]: { runs: [...list], active } }));
        const put = (i: number, patch: Partial<StatementRun>) => {
            list[i] = { ...list[i], ...patch };
            setResults((r) => ({ ...r, [tabKey]: { runs: [...list], active: r[tabKey]?.active ?? list[i].key } }));
        };
        show(list[0].key);
        setRunning(true);
        const ctl = new AbortController();
        stopper.current = ctl;
        try {
            for (let i = 0; i < list.length; i += 1) {
                if (ctl.signal.aborted) {
                    for (let j = i; j < list.length; j += 1) put(j, { status: 'stopped' });
                    break;
                }
                put(i, { status: 'running' });
                show(list[i].key);
                try {
                    const result = paged
                        ? await runPaged(client, { sql: list[i].text, connection, warehouse, limit }, ctl.signal,
                            (progress) => put(i, { progress }))
                        : await client.runSql(list[i].text, connection, limit, { signal: ctl.signal });
                    put(i, { status: 'succeeded', result });
                } catch (e) {
                    if (ctl.signal.aborted || e instanceof StatementStopped) {
                        for (let j = i; j < list.length; j += 1) put(j, { status: 'stopped' });
                        break;
                    }
                    put(i, { status: 'failed', error: describeError(e) });
                    for (let j = i + 1; j < list.length; j += 1) put(j, { status: 'skipped' });
                    if (list.length > 1 || mode === 'all') editor.current?.select(list[i].from, list[i].to);
                    break;
                }
            }
        } finally {
            stopper.current = null;
            setRunning(false);
            setTick((t) => t + 1);
        }
    }, [client, connection, sql, limit, running, activeTab, paged, warehouse]);

    const stop = () => stopper.current?.abort();

    const platform = conns.data?.find((c) => c.connection === connection)?.platform;
    // The formatter (every dialect's grammar) is loaded the first time someone formats, not with the page.
    const tidy = async () => {
        try {
            const { format } = await import('sql-formatter');
            setSql(format(sql, { language: sqlDialect(platform), keywordCase: 'upper', tabWidth: 2 }));
        } catch (e) {
            message.warning(`Could not format it: ${describeError(e)}`);
        }
    };

    const shown = runs.find((r) => r.key === activeRun) ?? runs[runs.length - 1];
    useEffect(() => setFilters({}), [shown?.key]);           // another result: its own filters
    const result = shown?.result ?? null;

    const download = () => {
        if (!result) return;
        const blob = new Blob([toCsv(result.columns, result.rows)], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${current?.name || 'query'}.csv`;
        a.click();
        URL.revokeObjectURL(a.href);
    };

    // Save: a saved query is saved as it is (a version of its own); an untitled tab asks for a name and a folder.
    const saveNow = async () => {
        if (!tab) return;
        if (!tab.savedId) {
            setForm({ name: '', description: '', shared: false, folder: undefined });
            setSaving(true);
            return;
        }
        if (!maySave(tab) || !connection) {
            message.warning('Only its owner and editors change this query: save a copy as a new file');
            return;
        }
        try {
            const q = await client.saveQuery({ name: tab.name, description: tab.description ?? '', sql: tab.sql, connection,
                shared: tab.shared, chart }, tab.savedId);
            setTabs((ts) => savedTab(ts, tab.key, q, tab.sql));
            setTick((t) => t + 1);
            message.success('Saved');
        } catch (e) {
            notifyError(e);
        }
    };

    const save = async () => {
        if (!connection || !tab) return;
        try {
            const q = await client.saveQuery({ name: form.name, description: form.description, shared: form.shared, sql, connection,
                chart }, undefined);
            if (form.folder) await client.fileBi(form.folder, 'query', q.id).catch(notifyError);
            setTabs((ts) => savedTab(ts, tab.key, q, sql));
            setSaving(false);
            setTick((t) => t + 1);
            message.success(form.shared ? 'Saved and shared' : 'Saved');
        } catch (e) {
            notifyError(e);
        }
    };

    const numeric = useMemo(
        () => (result ? result.columns.filter((_, i) => result.rows.some((r) => typeof r[i] === 'number')) : []),
        [result],
    );

    return (
        <Page>
            <TitleRow title="SQL" />
            <Muted>Read-only SQL as you: every table is checked and masked for you, as on its page.</Muted>
            <SqlGrid>
                <Surface className="sql-schema">
                    <Tabs size="small" activeKey={side} onChange={(k) => setSide(k as 'files' | 'objects')}
                        items={[{ key: 'files', label: 'Files' }, { key: 'objects', label: 'Objects' }]} />
                    {side === 'files' ? (
                        <WorkspaceFiles me={me} saved={saved.data ?? []} current={tab?.savedId} onOpen={openFile}
                            onNew={() => newTab()} onChanged={() => setTick((t) => t + 1)} />
                    ) : (<>
                    {schema.data && !schema.data.length && <Muted>No tables you may query here.</Muted>}
                    <Collapse>
                        {(schema.data ?? []).map((t) => (
                            <Collapse.Panel
                                key={t.urn}
                                header={
                                    <span
                                        onDoubleClick={() => editor.current?.insert(t.name)}
                                        title="Double-click to insert at the cursor">
                                        {t.name}
                                    </span>
                                }>
                                <ul style={{ margin: 0, paddingLeft: 16 }}>
                                    {t.columns.map((c) => (
                                        <li key={c.name}>
                                            {c.name} <Muted>{c.type}</Muted>
                                        </li>
                                    ))}
                                </ul>
                            </Collapse.Panel>
                        ))}
                    </Collapse>
                    {system.data && system.data.tables.length > 0 && (
                        <div style={{ marginTop: 12 }} data-testid="system-tables">
                            <strong>System tables</strong>
                            <Muted style={{ display: 'block', fontSize: 12 }}>
                                Cairn&apos;s own data, fresh within {Math.round(system.data.freshness_seconds / 60)} min.
                                Run on a Trino connection.
                            </Muted>
                            <Collapse>
                                {system.data.tables.map((t) => (
                                    <Collapse.Panel
                                        key={t.name}
                                        header={
                                            <span
                                                onDoubleClick={() => editor.current?.insert(t.name)}
                                                title={`${t.description} Double-click to insert at the cursor.`}>
                                                {t.name}
                                            </span>
                                        }>
                                        <Muted style={{ display: 'block', marginBottom: 4 }}>
                                            {t.description}
                                            {t.row_filtered ? ' You see your own rows unless you are an operator.' : ''}
                                        </Muted>
                                        <ul style={{ margin: 0, paddingLeft: 16 }}>
                                            {t.columns.map((c) => (
                                                <li key={c.name} title={c.description}>
                                                    {c.name} <Muted>{c.type}</Muted>
                                                    {t.masked_columns.includes(c.name) && <Tag style={{ marginLeft: 4 }}>masked</Tag>}
                                                </li>
                                            ))}
                                        </ul>
                                    </Collapse.Panel>
                                ))}
                            </Collapse>
                        </div>
                    )}
                    </>)}
                </Surface>
                <div className="sql-main">
                    <Surface>
                        {/* The + sits beside the tabs, not in the tab list: a tab list holds only tabs. */}
                        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 4 }}>
                            <div style={{ flex: 1, minWidth: 0 }}>
                                <Tabs
                                    type="editable-card"
                                    size="small"
                                    activeKey={activeTab}
                                    onChange={setActiveTab}
                                    hideAdd
                                    onEdit={(key, action) => { if (action === 'remove') close(String(key)); }}
                                    items={tabs.map((t) => ({
                                        key: t.key,
                                        closable: true,
                                        label: (
                                            <span title={t.error ? `Not saved: ${t.error}` : t.saving ? 'Saving…' : isDirty(t)
                                                ? (t.savedId ? 'Unsaved changes: saved in a moment' : 'Not saved yet') : 'Saved'}>
                                                {t.name}
                                                {(isDirty(t) || t.saving) && <span aria-label="unsaved" style={{ marginLeft: 6, color: 'var(--cairn-accent)' }}>●</span>}
                                                {t.error && <span aria-label="not saved" style={{ marginLeft: 6, color: 'var(--cairn-error-mark)' }}>!</span>}
                                            </span>
                                        ),
                                    }))}
                                />
                            </div>
                            <Button size="small" type="text" aria-label="New tab" title="New tab" onClick={() => newTab()}
                                style={{ marginTop: 4 }}>+</Button>
                        </div>
                        <Space style={{ marginBottom: 8 }} wrap>
                            <Select
                                size="small"
                                style={{ minWidth: 220 }}
                                aria-label="Connection"
                                value={connection}
                                onChange={setConnection}
                                options={(conns.data ?? []).map((c) => ({ value: c.connection, label: `${c.connection} (${c.platform})` }))}
                            />
                            {paged && (targets.data?.warehouses.length ?? 0) > 0 && (
                                <Select
                                    size="small"
                                    style={{ minWidth: 180 }}
                                    aria-label="Warehouse"
                                    value={warehouse ?? ''}
                                    onChange={(v) => setWarehouse(v || undefined)}
                                    options={[{ value: '', label: `${connection}'s own` },
                                        ...(targets.data?.warehouses ?? []).map((w) => ({ value: w, label: `warehouse ${w}` }))]}
                                />
                            )}
                            {paged && <Tooltip title="Read page by page on Trino: Stop cancels the query there">
                                <Tag>Trino</Tag></Tooltip>}
                            {current?.shared && <Tag color="blue">shared</Tag>}
                            {tab?.savedId && !maySave(tab) && <Tag>read-only: you may run it, not change it</Tag>}
                            {tab?.error && <Muted>Not saved: {tab.error}</Muted>}
                        </Space>
                        <SqlEditor
                            key={activeTab}
                            ref={editor}
                            onSave={() => void saveNow()}
                            value={sql}
                            onChange={setSql}
                            tables={schema.data ?? []}
                            onRun={(mode) => void run(mode)}
                            minLines={10}
                            maxLines={28}
                            placeholder="SELECT region, count(*) AS orders FROM sales.orders GROUP BY region"
                        />
                        <Space style={{ marginTop: 8 }} wrap>
                            <Tooltip title="The statement at the cursor, or the selection (Ctrl/⌘+Enter)">
                                <Button type="primary" onClick={() => void run('current')} loading={running}
                                    disabled={!connection || !sql.trim()}>
                                    Run
                                </Button>
                            </Tooltip>
                            <Tooltip title="Every statement, one after another (Ctrl/⌘+Shift+Enter)">
                                <Button onClick={() => void run('all')} disabled={!connection || !sql.trim() || running}>
                                    Run all
                                </Button>
                            </Tooltip>
                            {running && <Button danger onClick={stop}>Stop</Button>}
                            <span>
                                Rows <InputNumber aria-label="Rows" min={1} max={maxRows} value={Math.min(limit, maxRows)}
                                    onChange={(v) => setLimit(Math.min(Number(v) || 200, maxRows))} />
                            </span>
                            <Button onClick={() => void tidy()} disabled={!sql.trim()}>Format</Button>
                            <Tooltip title="Write SQL from a question, or explain the statement at the cursor in words">
                                <Button onClick={() => setAssisting(toRun(sql, editor.current?.selection() ?? { from: 0, to: 0 }, 'current')[0]?.text ?? '')}
                                    disabled={!connection}>Assistant</Button>
                            </Tooltip>
                            <Tooltip title="The plan of the statement at the cursor, as you would run it">
                                <Button onClick={() => setExplaining(toRun(sql, editor.current?.selection() ?? { from: 0, to: 0 }, 'current')[0]?.text ?? null)}
                                    disabled={!connection || !sql.trim()}>Explain</Button>
                            </Tooltip>
                            <Tooltip title="Ctrl/⌘+S. A saved file is also saved as you type.">
                                <Button onClick={() => void saveNow()} disabled={!sql.trim()}>Save</Button>
                            </Tooltip>
                            <Button onClick={download} disabled={!result}>
                                Download CSV
                            </Button>
                        </Space>
                        <Muted style={{ display: 'block', marginTop: 6 }}>
                            Ctrl/⌘+Enter runs the statement at the cursor or the selection · Ctrl/⌘+Shift+Enter runs all ·
                            Ctrl/⌘+S saves · Ctrl+Space suggests · Ctrl/⌘+F finds and replaces
                        </Muted>
                    </Surface>
                    {runs.length > 1 && (
                        <div role="group" aria-label="Statements">
                        <Tabs
                            style={{ marginTop: 12 }}
                            activeKey={shown?.key}
                            onChange={setActiveRun}
                            items={runs.map((r, i) => ({
                                key: r.key,
                                label: (
                                    <span title={r.text}>
                                        <StatusBadge state={RUN_STATE[r.status].state} label={`${i + 1}`} plain />{' '}
                                        {statementTitle(r.text, 32)}
                                    </span>
                                ),
                            }))}
                        />
                        </div>
                    )}
                    {shown?.status === 'running' && shown.progress && (
                        <Muted style={{ display: 'block', marginTop: 12 }} role="status">
                            Running{shown.progress.state ? ` (${shown.progress.state.toLowerCase()})` : ''}: {shown.progress.rows} row(s)
                            {shown.progress.stats.processedBytes ? ` · ${bytes(shown.progress.stats.processedBytes)} read` : ''}
                            {shown.progress.stats.elapsedTimeMillis != null ? ` · ${shown.progress.stats.elapsedTimeMillis} ms` : ''}
                        </Muted>
                    )}
                    {shown?.error && (
                        <Alert style={{ marginTop: 12 }} type="error" message={shown.error} showIcon
                            description={shown.status === 'failed' ? (
                                <FixWithAssistant key={shown.key} sql={shown.text} error={shown.error} connection={connection}
                                    onReplace={(fixed) => {
                                        // The statement where it ran, if it is still there as it was; else at the cursor.
                                        if (sql.slice(shown.from, shown.to) === shown.text) editor.current?.replace(shown.from, shown.to, fixed);
                                        else {
                                            message.info('The statement changed since it ran: the fix is put at the cursor');
                                            editor.current?.insert(fixed);
                                        }
                                    }} />
                            ) : undefined} />
                    )}
                    {shown && !shown.result && !shown.error && shown.status !== 'running' && (
                        <Muted style={{ display: 'block', marginTop: 12 }}>
                            {shown.status === 'stopped' ? 'Stopped before it finished.' : shown.status === 'skipped'
                                ? 'Not run: a statement before it failed.' : 'Waiting for the statements before it.'}
                        </Muted>
                    )}
                    {result && (
                        <Surface style={{ marginTop: 12 }}>
                            <Muted>
                                {result.rows.length} row(s){result.truncated ? ' (more not shown)' : ''} in {result.elapsed_ms} ms
                                {result.masked_columns.length ? ` · masked for you: ${result.masked_columns.join(', ')}` : ''}
                            </Muted>
                            <Tabs
                                items={[
                                    {
                                        key: 'table',
                                        label: 'Table',
                                        children: <ResultGrid result={result} filters={filters} onFilters={setFilters} />,
                                    },
                                    {
                                        key: 'details',
                                        label: 'Details',
                                        children: <QueryDetails sql={shown?.text ?? ''} connection={connection} result={result} />,
                                    },
                                    {
                                        key: 'chart',
                                        label: 'Chart',
                                        children: (
                                            <>
                                                <Space wrap style={{ marginBottom: 8 }}>
                                                    <Select aria-label="Chart type" value={chart.type} style={{ width: 100 }}
                                                        onChange={(type) => setChart({ ...chart, type })}
                                                        options={CHART_TYPES.map((v) => ({ value: v, label: v.replace('_', ' ') }))} />
                                                    <Select aria-label="x" placeholder="x" value={chart.x} style={{ width: 160 }}
                                                        onChange={(x) => setChart({ ...chart, x })}
                                                        options={result.columns.map((c) => ({ value: c, label: c }))} />
                                                    <Select aria-label="y" placeholder={chart.type === 'map' ? 'size by' : 'y'} mode="multiple"
                                                        value={chart.y} style={{ minWidth: 200 }} onChange={(y) => setChart({ ...chart, y })}
                                                        options={numeric.map((c) => ({ value: c, label: c }))} />
                                                    {chart.type === 'heatmap' && (
                                                        <Select aria-label="series" placeholder="down the side" value={chart.series} style={{ width: 160 }}
                                                            onChange={(series) => setChart({ ...chart, series })}
                                                            options={result.columns.map((c) => ({ value: c, label: c }))} />
                                                    )}
                                                    {chart.type === 'map' && (['lat', 'lon'] as const).map((k) => (
                                                        <Select key={k} aria-label={k === 'lat' ? 'latitude' : 'longitude'} style={{ width: 140 }}
                                                            placeholder={k === 'lat' ? 'latitude' : 'longitude'} value={chart[k]}
                                                            onChange={(v) => setChart({ ...chart, [k]: v })}
                                                            options={numeric.map((c) => ({ value: c, label: c }))} />
                                                    ))}
                                                </Space>
                                                <ResultChart columns={result.columns} rows={result.rows} chart={chart} />
                                            </>
                                        ),
                                    },
                                ]}
                            />
                        </Surface>
                    )}
                    <Surface style={{ marginTop: 12 }}>
                        <Tabs
                            items={[
                                {
                                    key: 'history',
                                    label: 'History',
                                    children: (
                                        <Table<SqlRun>
                                            size="small"
                                            rowKey="id"
                                            pagination={{ pageSize: 10, hideOnSinglePage: true }}
                                            dataSource={history.data ?? []}
                                            columns={[
                                                {
                                                    title: 'Query',
                                                    render: (_, h) => (
                                                        <a onClick={() => newTab(h.sql, h.connection)} title="Open in a new tab" style={{ fontFamily: 'monospace' }}>
                                                            {h.sql.slice(0, 80)}
                                                        </a>
                                                    ),
                                                },
                                                {
                                                    title: 'Result',
                                                    render: (_, h) =>
                                                        h.status === 'ok' ? <StatusBadge state="succeeded" label={`${h.rows} row(s)`} plain />
                                                            : h.status === 'running' ? <StatusBadge state="running" label="Running" plain />
                                                                : h.status === 'cancelled' ? <StatusBadge state="stopped" label="Stopped" plain />
                                                                    : <StatusBadge state="failed" title={h.error ?? ''} plain />,
                                                },
                                                { title: 'When', render: (_, h) => ago(h.at_ms) },
                                            ]}
                                        />
                                    ),
                                },
                            ]}
                        />
                    </Surface>
                </div>
            </SqlGrid>
            <SqlAssistantDrawer open={assisting !== null} onClose={() => setAssisting(null)} connection={connection}
                statement={assisting ?? ''} onInsert={(text) => { editor.current?.insert(text); setAssisting(null); }}
                onOpenTab={(text, conn) => { newTab(text, conn); setAssisting(null); }} />
            <Modal open={explaining !== null} title="Explain" footer={null} width={900} onCancel={() => setExplaining(null)} destroyOnClose>
                {explaining !== null && <QueryDetails sql={explaining} connection={connection} result={null} auto />}
            </Modal>
            <Modal title="Save query" open={saving} onOk={() => void save()} onCancel={() => setSaving(false)}
                okText="Save" okButtonProps={{ disabled: !form.name.trim() }}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input aria-label="Name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <Input.TextArea aria-label="Description" placeholder="What it answers" value={form.description}
                        onChange={(e) => setForm({ ...form, description: e.target.value })} />
                    <Select aria-label="Folder" allowClear placeholder="Folder (optional)" style={{ width: '100%' }} value={form.folder}
                        onChange={(folder) => setForm({ ...form, folder })}
                        options={(folders.data ?? []).filter((f) => f.owner === me).map((f) => ({ value: f.id, label: f.name }))} />
                    <Checkbox checked={form.shared} onChange={(e) => setForm({ ...form, shared: e.target.checked })}>
                        Share with everyone (each person runs it as themselves)
                    </Checkbox>
                </Space>
            </Modal>
        </Page>
    );
}
