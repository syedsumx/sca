/**
 * Cairn Flows in the standalone demo (no backend): sample flows, a finished run, a run waiting for approval, and
 * runs you start that move step by step on the canvas. All data is fictional.
 */

type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any
type Handler = (body: Json, q: URLSearchParams, params: string[]) => unknown;

const HOUR = 3_600_000;
const STEP_MS = 1_600;

export function installFlowsDemo(on: (method: string, pattern: string, h: Handler) => void, fail: (status: number, msg: string) => never, me: string) {
    const now = Date.now();
    const nightly = {
        kind: 'flow', name: 'nightly-revenue', description: 'Refresh revenue, check it, branch on size, report and tell finance',
        params: [{ name: 'day', type: 'date', default: '{{ run.logical_date }}' }],
        triggers: [{ type: 'schedule', cron: '0 2 * * *', timezone: 'Europe/Berlin' }, { type: 'dataset', tables: ['sales.orders'] }],
        notify: { on: ['failure'], to: ['urn:li:corpuser:raj'] },
        steps: [
            { name: 'refresh', type: 'derived', table: 'revenue', reads: ['sales.orders'] },
            { name: 'no-negatives', type: 'check', after: ['refresh'], sql: 'SELECT count(*) AS bad FROM marts.revenue WHERE total < 0', connection: 'lakehouse', measure: 'bad', op: '==', value: 0 },
            { name: 'regions', type: 'sql', after: ['no-negatives'], sql: 'SELECT count(*) AS n FROM marts.revenue', connection: 'lakehouse' },
            { name: 'size', type: 'branch', after: ['regions'], condition: 'steps.regions.preview[0][0] > 5', then: ['full-report'], else: ['short-note'] },
            { name: 'full-report', type: 'sql', after: ['size'], sql: 'SELECT region, total FROM marts.revenue ORDER BY total DESC', connection: 'lakehouse' },
            { name: 'short-note', type: 'notify', after: ['size'], message: 'Only {{ steps.regions.preview[0][0] }} regions today' },
            { name: 'per-region', type: 'for_each', after: ['full-report'], items: '{{ steps.full-report.preview }}', concurrency: 4,
                step: { type: 'notify', message: '{{ item[0] }}: {{ item[1] }}' } },
            { name: 'tell-finance', type: 'notify', after: ['per-region', 'short-note'], when: 'none_failed', to: ['urn:li:corpuser:raj'], message: 'Revenue for {{ params.day }} is ready.' },
        ],
        layout: {
            refresh: { x: 0, y: 120 }, 'no-negatives': { x: 280, y: 120 }, regions: { x: 560, y: 120 }, size: { x: 840, y: 120 },
            'full-report': { x: 1120, y: 40 }, 'short-note': { x: 1120, y: 220 }, 'per-region': { x: 1400, y: 40 }, 'tell-finance': { x: 1680, y: 120 },
        },
    };
    const triage = {
        kind: 'flow', name: 'agent-triage', description: 'An agent triages failed checks; a person approves before anyone is told',
        triggers: [{ type: 'schedule', cron: '30 7 * * 1-5', timezone: 'UTC' }],
        steps: [
            { name: 'failures', type: 'sql', sql: 'SELECT dataset, monitor, message FROM ops.check_failures WHERE day = current_date', connection: 'lakehouse' },
            { name: 'triage', type: 'agent', after: ['failures'], app: 'triage', input: 'Triage these and propose owners: {{ steps.failures.preview }}' },
            { name: 'approve', type: 'approval', after: ['triage'], approvers: [me], message: '{{ steps.triage.output }}' },
            { name: 'tell', type: 'notify', after: ['approve'], message: '{{ steps.triage.output }}' },
        ],
    };
    const ingest = {
        kind: 'flow', name: 'ingest-orders', description: 'Load new order files, rebuild the clean table, run the dbt models',
        triggers: [{ type: 'dataset', tables: ['raw.orders'] }], concurrency: { policy: 'skip' },
        steps: [
            { name: 'load', type: 'pipe', pipe: 'orders-files' },
            { name: 'clean', type: 'derived', after: ['load'], table: 'orders_clean' },
            { name: 'models', type: 'dbt', after: ['clean'], command: 'build', select: 'tag:orders' },
            { name: 'spark-agg', type: 'spark', after: ['clean'], application: 's3a://jobs/agg.py' },
            { name: 'airflow-export', type: 'airflow', after: ['models', 'spark-agg'], dag_id: 'export_orders' },
        ],
    };
    const FLOWS: Json[] = [
        { def: nightly, id: 'fl_nightly', owner: me, role: 'owner', version: 3, enabled: true, next: now + 9 * HOUR, published: nightly },
        { def: triage, id: 'fl_triage', owner: me, role: 'owner', version: 1, enabled: true, next: now + 20 * HOUR, published: triage },
        { def: ingest, id: 'fl_ingest', owner: 'urn:li:corpuser:mei', role: 'editor', version: 2, enabled: true, next: null, published: ingest },
    ];
    const RUNS: Json[] = [];

    const outputs = (s: Json): Json => {
        if (s.type === 'sql') return s.name === 'regions' ? { rows: 1, columns: ['n'], preview: [[7]] } : { rows: 7, columns: ['region', 'total'], preview: [['EMEA', 412000], ['NA', 388000], ['APAC', 201000]] };
        if (s.type === 'check') return { rows: 1, value: 0, passed: true, detail: `${s.measure} is 0` };
        if (s.type === 'derived') return { table: s.table, kind: 'full', detail: 'ok' };
        if (s.type === 'branch') return { result: true, chosen: 'then' };
        if (s.type === 'agent') return { output: '3 failed checks: 2 belong to finance-data (stale feed), 1 to growth (schema change).' };
        if (s.type === 'for_each') return { items: 3, succeeded: 3 };
        if (s.type === 'notify') return { sent_to: s.to ?? [me] };
        return { status: 'succeeded' };
    };

    /** A run's steps as they would be `elapsed` ms after it started: one step after another, approvals wait. */
    const project = (run: Json): Json => {
        const def = run.def;
        const elapsed = Date.now() - run.created_ms;
        const status: Record<string, string> = {};
        const done: Record<string, number> = {};
        const order: Json[] = [];
        const seen = new Set<string>();
        const visit = (s: Json) => {
            if (seen.has(s.name)) return;
            seen.add(s.name);
            for (const a of s.after ?? []) visit(def.steps.find((x: Json) => x.name === a));
            order.push(s);
        };
        def.steps.forEach(visit);
        for (const s of order) {
            const deps = (s.after ?? []).map((a: string) => a);
            const notChosen = deps.some((d: string) => {
                const src = def.steps.find((x: Json) => x.name === d);
                return src?.type === 'branch' && (src.else ?? []).includes(s.name);
            });
            if (run.cancelled) {
                status[s.name] = status[s.name] ?? 'cancelled';
            }
            if (deps.some((d: string) => status[d] === 'failed' || (status[d] === 'skipped' && s.when !== 'none_failed'))) {
                status[s.name] = 'skipped';
                done[s.name] = done[deps[0]] ?? 0;
                continue;
            }
            if (notChosen) {
                status[s.name] = 'skipped';
                done[s.name] = Math.max(...deps.map((d: string) => done[d] ?? 0));
                continue;
            }
            if (deps.some((d: string) => !['ok', 'skipped'].includes(status[d]))) {
                status[s.name] = run.cancelled ? 'cancelled' : 'pending';
                continue;
            }
            const start = Math.max(0, ...deps.map((d: string) => done[d] ?? 0));
            if (s.type === 'approval' && !run.decisions[s.name]) {
                status[s.name] = elapsed >= start ? (run.cancelled ? 'cancelled' : 'waiting') : 'pending';
                continue;
            }
            if (s.type === 'approval' && run.decisions[s.name]?.decision === 'reject') {
                status[s.name] = 'failed';
                done[s.name] = start;
                continue;
            }
            const end = s.type === 'approval' ? Math.max(start, run.decisions[s.name].at - run.created_ms) : start + STEP_MS;
            done[s.name] = end;
            status[s.name] = run.cancelled && elapsed < end ? 'cancelled' : elapsed >= end ? 'ok' : elapsed >= start ? 'running' : 'pending';
        }
        const statuses = Object.values(status);
        const overall = run.cancelled ? 'cancelled' : statuses.includes('failed') ? 'failed'
            : statuses.every((x) => ['ok', 'skipped'].includes(x)) ? 'succeeded' : statuses.includes('waiting') && !statuses.includes('running') ? 'waiting' : 'running';
        const finished = ['succeeded', 'failed', 'cancelled'].includes(overall) ? run.created_ms + Math.max(0, ...Object.values(done)) : null;
        return {
            id: run.id, flow_id: run.flow.id, flow: def.name, version: run.version, status: overall, trigger: run.trigger, triggered_by: me, run_as: run.flow.owner,
            params: run.params, logical_date: new Date(run.created_ms).toISOString().slice(0, 10), detail: {}, created_ms: run.created_ms,
            started_ms: run.created_ms, finished_ms: finished, error: null, cancel_requested: !!run.cancelled, definition: def,
            steps: def.steps.map((s: Json) => ({
                key: s.name, step: s.name, type: s.type, status: status[s.name], attempt: ['pending', 'skipped'].includes(status[s.name]) ? 0 : 1,
                started_ms: status[s.name] === 'pending' ? null : run.created_ms + (done[s.name] ?? elapsed) - STEP_MS, finished_ms: status[s.name] === 'ok' ? run.created_ms + done[s.name] : null,
                next_ms: null, error: status[s.name] === 'skipped' ? (s.name === 'short-note' ? 'not chosen by size (size took then)' : 'did not run') : status[s.name] === 'failed' ? `rejected by ${run.decisions[s.name]?.by}` : null,
                output: status[s.name] === 'ok' ? (s.type === 'approval' ? { approved: true, by: run.decisions[s.name].by, comment: run.decisions[s.name].comment } : outputs(s)) : null,
                reused: false, decision: run.decisions[s.name] ?? null,
                ...(s.type === 'approval' ? { approvers: s.approvers ?? [], groups: s.groups ?? [] } : {}),
                ...(s.type === 'for_each' && status[s.name] === 'ok' ? { items: [0, 1, 2].map((i) => ({ key: `${s.name}[${i}]`, step: s.name, type: 'notify', status: 'ok', attempt: 1, started_ms: null, finished_ms: null, next_ms: null, error: null, output: null, reused: false, decision: null })) } : {}),
            })),
        };
    };

    const startRun = (f: Json, trigger: string, params: Json, ageMs = 0, decisions: Json = {}) => {
        const run = { id: `run_${Math.random().toString(16).slice(2, 10)}${Date.now().toString(16).slice(-4)}`, flow: f, def: f.published ?? f.def, version: f.version,
            trigger, params: params ?? {}, created_ms: Date.now() - ageMs, decisions, cancelled: false };
        RUNS.unshift(run);
        return run;
    };
    startRun(FLOWS[0], 'schedule', { day: new Date(now - 7 * HOUR).toISOString().slice(0, 10) }, 7 * HOUR);
    startRun(FLOWS[1], 'schedule', {}, 2 * HOUR);
    startRun(FLOWS[2], 'dataset', {}, 30 * 60_000);

    const byName = (name: string) => FLOWS.find((f) => f.def.name === name || f.id === name) ?? fail(404, 'no such flow');
    const summary = (f: Json) => {
        const last = RUNS.find((r) => r.flow === f);
        const lp = last ? project(last) : null;
        return { id: f.id, name: f.def.name, owner: f.owner, description: f.def.description ?? '', editors: [], viewers: [], enabled: f.enabled,
            published_version: f.version || null, draft_errors: 0, next_run_ms: f.next, updated_ms: now - HOUR, updated_by: f.owner,
            steps: f.def.steps.length, role: f.role, last_run: lp ? { id: lp.id, status: lp.status, created_ms: lp.created_ms, finished_ms: lp.finished_ms } : null };
    };
    const detail = (f: Json) => ({ ...summary(f), draft: f.def, errors: [], draft_yaml: null, published: f.published });

    on('GET', '/v1/flows', () => FLOWS.map(summary));
    on('GET', '/v1/flows/approvals', () => RUNS.filter((r) => !r.cancelled).flatMap((r) => project(r).steps
        .filter((s: Json) => s.type === 'approval' && s.status === 'waiting' && (s.approvers ?? []).includes(me))
        .map((s: Json) => ({ run: r.id, flow: r.def.name, step: s.key, message: 'The agent triaged 3 failed checks: send its summary?', since_ms: s.started_ms }))));
    on('POST', '/v1/flows', (b) => {
        if (FLOWS.some((f) => f.def.name === b.name)) fail(409, `a flow named ${b.name} exists`);
        const f = { def: { kind: 'flow', ...b }, id: `fl_${Date.now()}`, owner: me, role: 'owner', version: 0, enabled: true, next: null, published: null };
        FLOWS.push(f);
        return detail(f);
    });
    on('GET', '/v1/flows/runs/:id', (_b, _q, [id]) => project(RUNS.find((r) => r.id === id) ?? fail(404, 'no such run')));
    on('GET', '/v1/flows/runs/:id/steps/:id/log', (_b, _q, [id, key]) => {
        const p = project(RUNS.find((r) => r.id === id) ?? fail(404, 'no such run'));
        const s = p.steps.find((x: Json) => x.key === key);
        return { key, status: s?.status, log: s?.status === 'ok' ? `${new Date().toISOString().slice(11, 19)} ${key}: done as ${p.run_as.split(':').pop()}` : '', error: s?.error, output: s?.output };
    });
    on('POST', '/v1/flows/runs/:id/cancel', (_b, _q, [id]) => {
        const r = RUNS.find((x) => x.id === id) ?? fail(404, 'no such run');
        r.cancelled = true;
        return project(r);
    });
    on('POST', '/v1/flows/runs/:id/rerun', (_b, _q, [id]) => {
        const r = RUNS.find((x) => x.id === id) ?? fail(404, 'no such run');
        return project(startRun(r.flow, 'rerun', r.params));
    });
    on('POST', '/v1/flows/runs/:id/steps/:id/approve', (b, _q, [id, step]) => {
        const r = RUNS.find((x) => x.id === id) ?? fail(404, 'no such run');
        r.decisions[step] = { decision: 'approve', by: me, comment: b?.comment ?? '', at: Date.now() };
        return project(r);
    });
    on('POST', '/v1/flows/runs/:id/steps/:id/reject', (b, _q, [id, step]) => {
        const r = RUNS.find((x) => x.id === id) ?? fail(404, 'no such run');
        r.decisions[step] = { decision: 'reject', by: me, comment: b?.comment ?? '', at: Date.now() };
        return project(r);
    });
    on('GET', '/v1/flows/:id', (_b, _q, [name]) => detail(byName(decodeURIComponent(name))));
    on('PUT', '/v1/flows/:id', (b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        f.def = { ...b, name: f.def.name };
        return detail(f);
    });
    on('DELETE', '/v1/flows/:id', (_b, _q, [name]) => {
        FLOWS.splice(FLOWS.indexOf(byName(decodeURIComponent(name))), 1);
        return undefined;
    });
    on('POST', '/v1/flows/:id/publish', (_b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        f.version += 1;
        f.published = JSON.parse(JSON.stringify(f.def));
        return { flow: f.def.name, version: f.version };
    });
    on('POST', '/v1/flows/:id/enabled', (b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        f.enabled = !!b.enabled;
        return detail(f);
    });
    on('GET', '/v1/flows/:id/versions', (_b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        return Array.from({ length: f.version }, (_, i) => ({ version: f.version - i, published_by: f.owner, published_ms: now - (i + 1) * 24 * HOUR, note: null }));
    });
    on('POST', '/v1/flows/:id/runs', (b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        const draft = b?.version === 'draft';
        if (!f.version && !draft) fail(409, `${f.def.name} has no published version: publish it (or run the draft)`);
        const run = startRun(draft ? { ...f, published: f.def, version: 0 } : f, 'manual', b?.params ?? {});
        run.flow = f;
        return project(run);
    });
    on('GET', '/v1/flows/:id/runs', (_b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        return RUNS.filter((r) => r.flow === f).map((r) => {
            const p = project(r);
            return { id: p.id, version: p.version, status: p.status, trigger: p.trigger, triggered_by: p.triggered_by, created_ms: p.created_ms,
                started_ms: p.started_ms, finished_ms: p.finished_ms, logical_date: p.logical_date, error: null };
        });
    });
    on('POST', '/v1/flows/:id/backfill', (b, _q, [name]) => {
        const f = byName(decodeURIComponent(name));
        const days = Math.max(1, Math.round((Date.parse(b.end) - Date.parse(b.start)) / (24 * HOUR)) + 1);
        return { runs: Array.from({ length: Math.min(days, 366) }, () => startRun(f, 'backfill', {}).id) };
    });
}
