/**
 * Airflow both ways and infrastructure-as-code change requests in the standalone demo (no backend): Airflow DAGs that
 * are yours (one running, one convertible, one that is not), and change requests proposed by agents, with plans to
 * approve and apply, and "Ask an agent" drafting a flow. All data is fictional.
 */

type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any
type Handler = (body: Json, q: URLSearchParams, params: string[]) => unknown;

const MIN = 60_000;

export function installAirflowIacDemo(on: (method: string, pattern: string, h: Handler) => void,
    fail: (status: number, msg: string) => never, me: string, addFlow: (def: Json) => void) {
    const now = Date.now();
    const iso = (ms: number) => new Date(ms).toISOString();
    // ---- Airflow's DAGs ------------------------------------------------------------------------------------------
    const DAGS: Record<string, Json> = {
        orders_daily: {
            summary: { dag_id: 'orders_daily', description: 'Orders, loaded and modelled every night', owners: ['raj'], tags: ['team:sales'],
                is_paused: false, schedule: '0 1 * * *', schedule_text: 'At 01:00', has_import_errors: false, cairn_flow: null },
            steps: [
                { name: 'start', type: 'wait', after: [], operator: 'EmptyOperator', trigger_rule: 'all_success' },
                { name: 'load', type: 'sql', after: ['start'], operator: 'CairnTrinoOperator', trigger_rule: 'all_success', retries: 2 },
                { name: 'models', type: 'dbt', after: ['load'], operator: 'CairnDbtOperator', trigger_rule: 'all_success' },
                { name: 'fresh', type: 'check', after: ['models'], operator: 'CairnCheckOperator', trigger_rule: 'all_success' },
            ],
            started: now - 3 * MIN,
            conversion: { convertible: true, blockers: [], notes: [], flow: {} },
            flow: { kind: 'flow', name: 'orders-daily', description: 'Converted from the Airflow DAG orders_daily',
                triggers: [{ type: 'schedule', cron: '0 1 * * *', timezone: 'UTC' }],
                steps: [
                    { name: 'load', type: 'sql', sql: 'INSERT INTO marts.orders_daily SELECT * FROM raw.orders', connection: 'lakehouse', retries: 2 },
                    { name: 'models', type: 'dbt', after: ['load'], command: 'build', select: 'tag:orders', project_dir: '/dbt' },
                    { name: 'fresh', type: 'check', after: ['models'], sql: 'SELECT count(*) AS n FROM marts.orders_daily', connection: 'lakehouse', measure: 'n', op: '>', value: 0 },
                ] },
        },
        finance_close: {
            summary: { dag_id: 'finance_close', description: 'Month-end close with a Python reconciliation', owners: ['finance'], tags: ['team:finance'],
                is_paused: false, schedule: '0 6 1 * *', schedule_text: 'At 06:00 on day 1 of the month', has_import_errors: false, cairn_flow: null },
            steps: [
                { name: 'extract', type: 'sql', after: [], operator: 'CairnTrinoOperator', trigger_rule: 'all_success' },
                { name: 'reconcile', type: 'python', after: ['extract'], operator: 'PythonOperator', trigger_rule: 'all_success' },
                { name: 'publish', type: 'sql', after: ['reconcile'], operator: 'CairnTrinoOperator', trigger_rule: 'all_success' },
            ],
            started: now - 26 * 60 * MIN,
            conversion: { convertible: false, notes: [], flow: null, blockers: [
                { task: 'reconcile', operator: 'PythonOperator', reason: "PythonOperator has no flow step: only cairn-airflow-provider's operators (Trino, check, dbt, Spark), TriggerDagRunOperator and EmptyOperator convert" },
            ] },
        },
        'cairn__nightly-revenue': {
            summary: { dag_id: 'cairn__nightly-revenue', description: 'Refresh revenue, check it, branch on size, report and tell finance',
                owners: ['ana'], tags: ['cairn', 'cairn-flow:nightly-revenue', 'cairn-flow-version:4'], is_paused: false, schedule: null,
                schedule_text: 'Never, external triggers only', has_import_errors: false, cairn_flow: 'nightly-revenue' },
            steps: [
                { name: 'refresh', type: 'derived', after: [], operator: 'CairnFlowStepOperator', trigger_rule: 'all_success' },
                { name: 'no-negatives', type: 'check', after: ['refresh'], operator: 'CairnFlowStepOperator', trigger_rule: 'all_success' },
                { name: 'size', type: 'branch', after: ['no-negatives'], operator: 'CairnFlowBranchOperator', trigger_rule: 'all_success' },
                { name: 'full-report', type: 'sql', after: ['size'], operator: 'CairnFlowStepOperator', trigger_rule: 'all_success' },
                { name: 'short-note', type: 'notify', after: ['size'], operator: 'CairnFlowStepOperator', trigger_rule: 'all_success' },
            ],
            started: now - 50 * MIN,
            conversion: { convertible: false, notes: [], flow: null, blockers: [
                { task: 'refresh', operator: 'CairnFlowStepOperator', reason: 'generated from the Cairn flow nightly-revenue: open the flow in Cairn instead' },
            ] },
        },
    };
    // A run that moves on while you watch: every task takes 20 seconds.
    const states = (d: Json): Record<string, string> => {
        const out: Record<string, string> = {};
        const done = Math.floor((Date.now() - d.started) / 20_000);
        d.steps.forEach((s: Json, i: number) => {
            if (d.summary.dag_id === 'cairn__nightly-revenue' && s.name === 'short-note') out[s.name] = 'skipped';
            else out[s.name] = i < done ? 'success' : i === done ? 'running' : 'scheduled';
        });
        return out;
    };
    const runState = (d: Json) => (Object.values(states(d)).every((s) => s === 'success' || s === 'skipped') ? 'success' : 'running');
    const STATUS: Record<string, string> = { success: 'ok', running: 'running', scheduled: 'pending', skipped: 'skipped', failed: 'failed' };
    const byId = (id: string) => DAGS[decodeURIComponent(id)] ?? fail(404, 'no such DAG');
    on('GET', '/v1/airflow/dags', () => Object.values(DAGS).map((d) => ({ ...d.summary,
        last_run: { dag_run_id: `scheduled__${iso(d.started)}`, state: runState(d), start_date: iso(d.started), end_date: null, run_type: 'scheduled' } })));
    on('GET', '/v1/airflow/dags/:id', (_b, _q, [id]) => {
        const d = byId(id);
        const st = states(d);
        const run = `scheduled__${iso(d.started)}`;
        return { ...d.summary, timezone: 'UTC', file: `${d.summary.dag_id}.py`, steps: d.steps,
            runs: [{ dag_run_id: run, state: runState(d), run_type: 'scheduled', start_date: iso(d.started), end_date: null }],
            run, run_state: runState(d), states: st, statuses: Object.fromEntries(Object.entries(st).map(([k, v]) => [k, STATUS[v] ?? 'running'])),
            conversion: d.conversion };
    });
    on('POST', '/v1/airflow/dags/:id/convert', (b, _q, [id]) => {
        const d = byId(id);
        if (!d.conversion.convertible) fail(409, `${d.summary.dag_id} cannot be converted: ${d.conversion.blockers.map((x: Json) => `${x.task}: ${x.reason}`).join('; ')}`);
        const def = { ...d.flow, name: b?.name || d.flow.name };
        addFlow(def);
        return { flow: def.name, notes: [], draft: def };
    });
    on('GET', '/v1/flows/:id/airflow', (_b, _q, [name]) => `"""${decodeURIComponent(name)}: generated by Cairn (demo)"""\n\nfrom airflow.sdk import DAG\n`);

    // ---- change requests --------------------------------------------------------------------------------------------
    const flowSpec = {
        triggers: [{ type: 'schedule', cron: '30 1 * * *', timezone: 'Europe/Berlin' }],
        steps: [
            { name: 'refresh', type: 'derived', table: 'orders_daily' },
            { name: 'check', type: 'check', after: ['refresh'], sql: 'SELECT count(*) AS n FROM marts.orders_daily', connection: 'lakehouse', measure: 'n', op: '>', value: 0 },
            { name: 'email', type: 'notify', after: ['check'], to: ['urn:li:corpuser:raj'], message: 'Orders are refreshed: {{ steps.check.value }} rows' },
        ],
    };
    const plan = (items: Json[]) => ({ changes: items, digest: 'demo', counts: {
        create: items.filter((c) => c.action === 'create').length, update: items.filter((c) => c.action === 'update').length,
        replace: 0, delete: items.filter((c) => c.action === 'delete').length, unchanged: 0 } });
    const CHANGES: Json[] = [
        { id: 'chg_revenue', title: 'Revenue report for sales', status: 'proposed', author: me, agent: 'agt_reporting', sponsor: me,
            intent: 'A saved query and a nightly flow that sends the revenue report to sales', allow_delete: false, kinds: ['flows', 'queries'],
            approved_by: null, approved_ms: null, comment: null, applied_by: null, applied_ms: null, error: null, created_ms: now - 40 * MIN,
            updated_ms: now - 40 * MIN, commit: null, may_approve: false, may_apply: false, variables: [], result: null,
            plan: plan([
                { action: 'create', kind: 'queries', key: 'revenue-by-region', terraform_type: 'cairn_saved_query', fields: [
                    { field: 'name', before: null, after: 'revenue-by-region' }, { field: 'connection', before: null, after: 'lakehouse' },
                    { field: 'sql', before: null, after: 'SELECT region, sum(amount) AS total FROM marts.revenue GROUP BY region' }] },
                { action: 'create', kind: 'flows', key: 'nightly-orders', terraform_type: 'cairn_flow', fields: [
                    { field: 'name', before: null, after: 'nightly-orders' }, { field: 'spec', before: null, after: { kind: 'flow', name: 'nightly-orders', ...flowSpec } },
                    { field: 'enabled', before: null, after: true }] }]),
            bundle: 'bundle: change\nqueries:\n- name: revenue-by-region\n  connection: lakehouse\n  sql: SELECT region, sum(amount) AS total FROM marts.revenue GROUP BY region\nflows:\n- name: nightly-orders\n  spec: {...}\n',
            terraform: 'resource "cairn_saved_query" "revenue_by_region" {\n  name = "revenue-by-region"\n  connection_name = "lakehouse"\n  sql = "SELECT region, sum(amount) AS total FROM marts.revenue GROUP BY region"\n}\n',
            history: [{ at_ms: now - 40 * MIN, by: me, action: 'proposed' }] },
        { id: 'chg_alert', title: 'Tighten the revenue drop alert', status: 'proposed', author: 'urn:li:corpuser:raj', agent: 'agt_ops', sponsor: 'urn:li:corpuser:raj',
            intent: 'Check hourly instead of daily, as finance asked', allow_delete: false, kinds: ['alerts'], approved_by: null, approved_ms: null, comment: null,
            applied_by: null, applied_ms: null, error: null, created_ms: now - 3 * 60 * MIN, updated_ms: now - 3 * 60 * MIN, commit: null, may_approve: true,
            may_apply: false, variables: [], result: null,
            plan: plan([{ action: 'update', kind: 'alerts', key: 'revenue-drop', terraform_type: 'cairn_sql_alert', fields: [
                { field: 'every_minutes', before: 1440, after: 60 }] }]),
            bundle: 'bundle: change\nalerts:\n- {name: revenue-drop, query: revenue-by-region, op: <, value: 1000, every_minutes: 60}\n',
            terraform: 'resource "cairn_sql_alert" "revenue_drop" {\n  every_minutes = 60\n}\n',
            history: [{ at_ms: now - 3 * 60 * MIN, by: 'urn:li:corpuser:raj', action: 'proposed' }] },
        { id: 'chg_secret', title: 'Rotate the finance database password', status: 'approved', author: 'urn:li:corpuser:mei', agent: null, sponsor: 'urn:li:corpuser:mei',
            intent: 'Quarterly rotation', allow_delete: false, kinds: ['secrets'], approved_by: me, approved_ms: now - 20 * MIN, comment: 'ok', applied_by: null,
            applied_ms: null, error: null, created_ms: now - 2 * 60 * MIN, updated_ms: now - 20 * MIN, commit: null, may_approve: false, may_apply: true,
            variables: ['FINANCE_DB_PASSWORD'], result: null,
            plan: plan([{ action: 'update', kind: 'secrets', key: 'finance-db/password', terraform_type: 'cairn_secret', fields: [
                { field: 'value_version', before: 3, after: 4 }] }]),
            bundle: 'bundle: change\nsecrets:\n- {scope: finance-db, key: password, value: ${FINANCE_DB_PASSWORD}, value_version: 4}\n',
            terraform: 'resource "cairn_secret" "finance_db_password" {\n  value = var.finance_db_password\n}\n',
            history: [{ at_ms: now - 2 * 60 * MIN, by: 'urn:li:corpuser:mei', action: 'proposed' }, { at_ms: now - 20 * MIN, by: me, action: 'approved', comment: 'ok' }] },
    ];
    const one = (id: string) => CHANGES.find((c) => c.id === id) ?? fail(404, 'no such change request');
    on('GET', '/v1/iac/changes', (_b, q) => CHANGES.filter((c) => !q.get('status') || c.status === q.get('status')));
    on('GET', '/v1/iac/changes/assistant', () => ({ ready: true, model: 'demo-model', why: null }));
    on('GET', '/v1/iac/changes/:id', (_b, _q, [id]) => one(id));
    on('POST', '/v1/iac/changes/:id/approve', (b, _q, [id]) => {
        const c = one(id);
        if (!c.may_approve) fail(403, 'you proposed this change (or your agent did): someone else approves it (separation of duties)');
        Object.assign(c, { status: 'approved', approved_by: me, approved_ms: Date.now(), comment: b?.comment ?? '', may_approve: false, may_apply: true });
        c.history.push({ at_ms: Date.now(), by: me, action: 'approved', comment: b?.comment ?? '' });
        return c;
    });
    on('POST', '/v1/iac/changes/:id/reject', (b, _q, [id]) => {
        const c = one(id);
        Object.assign(c, { status: c.sponsor === me ? 'withdrawn' : 'rejected', comment: b?.comment ?? '' });
        c.history.push({ at_ms: Date.now(), by: me, action: c.status, comment: b?.comment ?? '' });
        return c;
    });
    on('POST', '/v1/iac/changes/:id/apply', (b, _q, [id]) => {
        const c = one(id);
        const missing = (c.variables ?? []).filter((v: string) => !b?.variables?.[v]);
        if (missing.length) fail(422, `give the values of ${missing.join(', ')} (variables) to apply this change`);
        Object.assign(c, { status: 'applied', applied_by: me, applied_ms: Date.now(), may_apply: false,
            result: { done: c.plan.changes.map((x: Json) => `${x.action === 'create' ? '+' : '~'} ${x.kind}/${x.key}`) } });
        c.history.push({ at_ms: Date.now(), by: me, action: 'applied' });
        return c;
    });
    on('POST', '/v1/iac/changes/:id/commit', (_b, _q, [id]) => {
        const c = one(id);
        c.commit = { branch: `cairn/iac-change-${id}`, commit: '9d1e0f2', pull_request: null };
        return c.commit;
    });
    on('POST', '/v1/iac/changes/draft', (b) => {
        const words = String(b?.prompt ?? '').trim();
        if (!words) fail(400, 'describe the change you want');
        const c = { ...CHANGES[0], id: `chg_${Date.now()}`, title: 'Nightly orders flow', intent: words, agent: 'cairn-iac-assistant',
            created_ms: Date.now(), updated_ms: Date.now(), kinds: ['flows'], history: [{ at_ms: Date.now(), by: me, action: 'proposed' }],
            plan: plan([CHANGES[0].plan.changes[1]]) };
        CHANGES.unshift(c);
        return { reply: 'I proposed the flow nightly-orders: refresh, check, then email the report. It waits for approval.', change: c,
            flows: [{ name: 'nightly-orders', spec: flowSpec }], tool_calls: [{ name: 'iac_plan', ok: true }, { name: 'iac_propose', ok: true }], model: 'demo-model' };
    });
}
