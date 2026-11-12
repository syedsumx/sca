/**
 * Demo data for the first look (L150): people, groups and service accounts; platform health, incidents, alerts and
 * SLOs; pipeline observability with Airflow runs; scheduled job runs; and the small reads other pages make (rights,
 * devices, system tables). All of it is fictional and lives in memory until the page is reloaded.
 */
type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any
type Handler = (body: Json, q: URLSearchParams, params: string[]) => unknown;
type On = (method: string, pattern: string, h: Handler) => void;
type Fail = (status: number, msg: string) => never;

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

export function installOpsDemo(on: On, fail: Fail, me: string) {
    const now = Date.now();
    const iso = (msAgo: number) => new Date(now - msAgo).toISOString();

    // ------------------------------------------------------------------------------------------- people and roles
    const person = (id: string, display_name: string, extra: Json) => ({
        id, urn: `urn:li:corpuser:${id}`, type: 'person', display_name, email: `${id}@northwind.example`, title: null,
        department: null, country: 'GB', employment_type: 'employee', manager: null, attributes: {}, status: 'active',
        source: 'scim', groups: [], ...extra,
    });
    const users = [
        person('alice', 'Alice Moreno', { title: 'Finance analyst', department: 'Finance', groups: ['finance-analysts', 'cairn-admins'] }),
        person('raj', 'Raj Patel', { title: 'Analytics engineer', department: 'Data platform', groups: ['data-engineering'], manager: 'tom' }),
        person('mei', 'Mei Lin', { title: 'Data steward', department: 'Customer', groups: ['data-stewards'], country: 'SG' }),
        person('tom', 'Tom Becker', { title: 'Head of data', department: 'Data platform', groups: ['data-engineering', 'cairn-admins'], country: 'DE' }),
        person('sofia', 'Sofia Rossi', { title: 'Marketing analyst', department: 'Marketing', groups: ['marketing'], country: 'IT', source: 'sso' }),
        person('ken', 'Ken Ito', { title: 'Contractor, pricing', department: 'Finance', groups: ['finance-analysts'], country: 'JP',
            employment_type: 'contractor', source: 'manual' }),
        person('olga', 'Olga Novak', { title: 'Supply planner', department: 'Supply chain', groups: [], status: 'deactivated' }),
        { ...person('svc-dbt', 'dbt Cloud (service)', { department: 'Data platform', groups: ['data-engineering'] }), type: 'service',
            email: null, source: 'manual' },
    ];
    const groups = [
        { id: 'finance-analysts', name: 'Finance analysts', description: 'Revenue, margin and forecasting', source: 'scim' },
        { id: 'data-engineering', name: 'Data engineering', description: 'Pipelines, the lakehouse and dbt', source: 'scim' },
        { id: 'data-stewards', name: 'Data stewards', description: 'Glossary, quality and access reviews', source: 'scim' },
        { id: 'marketing', name: 'Marketing', description: 'Campaign analytics', source: 'sso' },
        { id: 'cairn-admins', name: 'Cairn admins', description: 'Platform administrators', source: 'manual' },
    ].map((g) => ({ ...g, urn: `urn:li:corpGroup:${g.id}`, members: users.filter((u) => u.groups.includes(g.id)).length }));
    const personas = [
        { id: 'finance-analysts', name: 'Finance analysts', permissions: ['CAIRN_DATA_ACCESS', 'VIEW_DATASET_USAGE'] },
        { id: 'data-engineers', name: 'Data engineers', permissions: ['CAIRN_DATA_ACCESS', 'MANAGE_PIPELINES'] },
        { id: 'stewards', name: 'Stewards', permissions: ['EDIT_ENTITY_DOCS', 'MANAGE_GLOSSARIES'] },
    ];
    const personaFor = (g: string) => ({ 'finance-analysts': 'finance-analysts', 'data-engineering': 'data-engineers',
        'data-stewards': 'stewards' } as Record<string, string>)[g];
    const page = <T,>(items: T[], q: URLSearchParams) => {
        const offset = Number(q.get('offset') ?? 0);
        const limit = Number(q.get('limit') ?? 50);
        return { total: items.length, offset, limit, items: items.slice(offset, offset + limit) };
    };
    const match = (want: string | null, value: string) => !want || want === 'all' || want === value;
    on('GET', '/v1/people/users', (_b, q) => {
        const text = (q.get('q') ?? '').toLowerCase();
        return page(users.filter((u) => (!text || `${u.display_name} ${u.id} ${u.email ?? ''} ${u.department ?? ''}`.toLowerCase().includes(text))
            && match(q.get('status'), u.status) && match(q.get('source'), u.source) && match(q.get('type'), u.type)), q);
    });
    const userById = (id: string) => users.find((u) => u.id === id) ?? fail(404, 'no such person');
    on('GET', '/v1/people/users/:id', (_b, _q, [id]) => {
        const u = userById(id);
        const held = u.groups.map(personaFor).filter(Boolean).map((p) => personas.find((x) => x.id === p)!);
        return {
            ...u, groups: groups.filter((g) => u.groups.includes(g.id)),
            personas: held.map((p) => ({ ...p, reasons: [{ kind: 'group', group: u.groups.find((g) => personaFor(g) === p.id) }] })),
            persona_grants: [], workspaces: u.groups.includes('finance-analysts') ? [{ id: 'finance', name: 'Finance', role: 'member', domains: ['urn:li:domain:finance'] }] : [],
            owns: [], last_sign_in: u.status === 'active' ? { ts: iso(3 * HOUR), action: 'sso' } : null,
            open_access_reviews: [], recent_audit: [], managed: { editable: u.source !== 'scim', why: u.source === 'scim' ? 'managed by your identity provider' : null },
        };
    });
    on('GET', '/v1/people/users/:id/access', (_b, _q, [id]) => {
        const u = userById(id);
        return {
            personas: u.groups.map(personaFor).filter(Boolean).map((p) => {
                const x = personas.find((y) => y.id === p)!;
                return { ...x, reasons: [{ kind: 'group' }], privileges: x.permissions, scope_text: 'Assets in the Finance domain' };
            }),
            purposes: [{ id: 'reporting', name: 'Financial reporting', labels: ['PII'], allowed: u.groups.includes('finance-analysts'), effect: 'mask' }],
            workspaces: [],
        };
    });
    on('GET', '/v1/people/groups', (_b, q) => {
        const text = (q.get('q') ?? '').toLowerCase();
        return page(groups.filter((g) => !text || `${g.name} ${g.description}`.toLowerCase().includes(text)), q);
    });
    on('GET', '/v1/people/groups/:id', (_b, _q, [id]) => {
        const g = groups.find((x) => x.id === id) ?? fail(404, 'no such group');
        return {
            ...g, member_list: users.filter((u) => u.groups.includes(g.id)).map((u) => ({ id: u.id, display_name: u.display_name,
                email: u.email, status: u.status, source: u.source })),
            personas: personaFor(g.id) ? [personas.find((p) => p.id === personaFor(g.id))!].map((p) => ({ id: p.id, name: p.name })) : [],
            purposes: [], workspaces: [], managed: { editable: g.source === 'manual', why: g.source === 'manual' ? null : 'managed by your identity provider' },
        };
    });
    on('GET', '/v1/people/principals', (_b, q) => {
        const all = [
            { type: 'agent', id: 'context-agent', name: 'Context Agent', owner: 'urn:li:corpuser:mei', scopes: ['read_metadata', 'propose_docs'],
                enabled: true, agent_kind: 'context', purpose: 'Drafts documentation for review', last_used_ms: now - 2 * HOUR },
            { type: 'workload', id: 'spark-etl', name: 'Spark ETL (Kubernetes)', owner: 'urn:li:corpuser:raj', scopes: ['query_data'],
                enabled: true, acts_as: 'urn:li:corpuser:svc-dbt', last_used_ms: now - 25 * 60_000 },
            { type: 'service', id: 'svc-dbt', name: 'dbt Cloud', owner: 'urn:li:corpuser:tom', scopes: ['ingest', 'query_data'],
                enabled: true, last_used_ms: now - 6 * HOUR },
        ];
        const type = q.get('type');
        return type && type !== 'all' ? all.filter((p) => p.type === type) : all;
    });

    // ------------------------------------------------------------------------------- platform observability
    const components = [
        { name: 'cairn-api', state: 'up', rate: 41.2, error_ratio: 0.002, p95_ms: 180, up: true, targets: 3, firing_alerts: 0 },
        { name: 'cairn-core', state: 'up', rate: 18.7, error_ratio: 0.0, p95_ms: 240, up: true, targets: 2, firing_alerts: 0 },
        { name: 'trino', state: 'degraded', rate: 6.4, error_ratio: 0.031, p95_ms: 2600, up: true, targets: 4, firing_alerts: 1 },
        { name: 'cairn-store', state: 'up', rate: 92.1, error_ratio: 0.0, p95_ms: 35, up: true, targets: 3, firing_alerts: 0 },
        { name: 'cairn-catalog', state: 'up', rate: 12.3, error_ratio: 0.0, p95_ms: 60, up: true, targets: 2, firing_alerts: 0 },
        { name: 'airflow', state: 'up', rate: 1.2, error_ratio: 0.0, p95_ms: 410, up: true, targets: 1, firing_alerts: 0 },
    ];
    on('GET', '/v1/observability/overview', () => ({
        backends: { prometheus: true, loki: true, tempo: true, trace_links: true, log_links: true, vendor: null },
        firing: { critical: 0, warning: 1, info: 0 }, open_incidents: 1, slos: 2, alert_rules: 3, silences_active: 0,
    }));
    on('GET', '/v1/observability/health', () => ({
        window: '5m', components,
        edges: [
            { from: 'cairn-api', to: 'cairn-core', rate: 15.1, error_ratio: 0 },
            { from: 'cairn-api', to: 'trino', rate: 6.4, error_ratio: 0.031 },
            { from: 'trino', to: 'cairn-catalog', rate: 9.8, error_ratio: 0 },
            { from: 'trino', to: 'cairn-store', rate: 60.2, error_ratio: 0 },
            { from: 'airflow', to: 'cairn-api', rate: 1.2, error_ratio: 0 },
        ],
    }));
    on('GET', '/v1/observability/traces', () => ({ items: [
        { trace_id: '4bf92f3577b34da6a3ce929d0e0e4736', root_service: 'cairn-api', root_name: 'POST /sql/query', start_ms: now - 4 * 60_000, duration_ms: 2710 },
        { trace_id: '00f067aa0ba902b7a3ce929d0e0e4737', root_service: 'cairn-api', root_name: 'GET /v1/flows', start_ms: now - 9 * 60_000, duration_ms: 84 },
    ] }));
    on('GET', '/v1/observability/logs', () => ({ items: [
        { at: iso(4 * 60_000), service: 'trino', level: 'warn', trace_id: '4bf92f3577b34da6a3ce929d0e0e4736', line: 'Query 20261111_0412 queued for 1.9s (warehouse finance-small is busy)' },
    ] }));
    const slos = [
        { id: 'slo-api', name: 'API availability', service: 'cairn-api', objective: 0.999, window: '30d', enabled: true, last_evaluation: null },
        { id: 'slo-sql', name: 'SQL p95 under 3 s', service: 'trino', objective: 0.95, window: '7d', enabled: true, last_evaluation: null },
    ];
    on('GET', '/v1/observability/slos', () => ({ items: slos }));
    on('GET', '/v1/observability/slos/:id/status', (_b, _q, [id]) => ({
        sli: id === 'slo-api' ? 0.9996 : 0.962, budget_remaining: id === 'slo-api' ? 0.61 : 0.24,
        burn: [{ long: '1h', short: '5m', factor: 14.4, severity: 'critical', long_burn: 0.4, short_burn: 0.2, burning: false }],
    }));
    on('GET', '/v1/observability/slos/:id/history', () => ({ burn_1h: Array.from({ length: 24 }, (_, i) => ({ at: iso((24 - i) * HOUR), value: 0.2 + (i % 5) / 10 })) }));
    on('GET', '/v1/observability/exporters', () => ({ items: [
        { id: 'otlp-grafana', name: 'Grafana Cloud', protocol: 'otlp-http', endpoint: 'https://otlp.grafana.example/otlp', signals: ['traces', 'metrics'],
            enabled: true, headers: [{ name: 'Authorization', scope: 'observability', key: 'grafana-token' }] },
    ] }));
    const alerts = [
        { id: 'al-trino-latency', source: 'rule', name: 'Trino p95 above 2 s', severity: 'warning', state: 'firing', labels: { service: 'trino' },
            value: 2.6, summary: 'Queries on the finance warehouse are slower than usual', started_at: iso(40 * 60_000), firing_since: iso(35 * 60_000),
            resolved_at: null, silenced_by: null, acknowledged_by: null, incident_id: 'inc-trino' },
    ];
    on('GET', '/v1/observability/alerts', () => ({ items: alerts }));
    on('GET', '/v1/observability/alert-rules', () => ({ items: [
        { id: 'rule-trino-latency', name: 'Trino p95 above 2 s', language: 'promql', severity: 'warning', pending_for: '5m', enabled: true, last_evaluation: { matches: 1 } },
        { id: 'rule-api-errors', name: 'API errors above 1%', language: 'promql', severity: 'critical', pending_for: '2m', enabled: true, last_evaluation: { matches: 0 } },
        { id: 'rule-store-disk', name: 'Store disk above 85%', language: 'promql', severity: 'warning', pending_for: '15m', enabled: true, last_evaluation: { matches: 0 } },
    ] }));
    on('GET', '/v1/observability/silences', () => ({ items: [] }));
    const incidents = [
        { id: 'inc-trino', title: 'Slow queries on the finance warehouse', severity: 'warning', status: 'acknowledged', services: ['trino'],
            summary: 'p95 at 2.6 s since a large backfill started', assignee: 'urn:li:corpuser:tom', opened_at: iso(35 * 60_000), resolved_at: null },
    ];
    on('GET', '/v1/observability/incidents', () => ({ items: incidents }));
    on('GET', '/v1/observability/incidents/:id', (_b, _q, [id]) => {
        const i = incidents.find((x) => x.id === id) ?? fail(404, 'no such incident');
        return { ...i, alerts, timeline: [
            { at: iso(35 * 60_000), kind: 'opened', actor: 'urn:li:corpuser:cairn-sre-agent', by_agent: true, text: 'Opened from the alert "Trino p95 above 2 s".' },
            { at: iso(30 * 60_000), kind: 'finding', actor: 'urn:li:corpuser:cairn-sre-agent', by_agent: true, text: 'The backfill job one-off-backfill started 5 minutes before latency rose.' },
            { at: iso(20 * 60_000), kind: 'acknowledged', actor: 'urn:li:corpuser:tom', by_agent: false, text: 'Looking at it; the backfill will finish in an hour.' },
        ] };
    });
    on('GET', '/status.json', () => ({ incidents: [], maintenance: [], history: [
        { id: 'st-1', title: 'SQL editor slow to start', impact: 'minor', components: ['SQL'], starts_ms: now - 6 * DAY, resolved_ms: now - 6 * DAY + 2 * HOUR,
            updates: [{ phase: 'resolved' }] },
    ] }));

    // ------------------------------------------------------- pipeline observability (Airflow runs, SLAs, products)
    const run = (pipeline: string, n: number, state: string, startAgo: number, durMin: number, error: string | null = null) => ({
        id: `${pipeline}:${n}`, pipeline, source: 'airflow', run_id: `scheduled__${new Date(now - startAgo).toISOString().slice(0, 16)}`,
        ol_run_id: null, state, start: iso(startAgo), end: state === 'running' ? null : iso(startAgo - durMin * 60_000), start_ms: now - startAgo,
        duration_ms: state === 'running' ? null : durMin * 60_000, trace_id: null, error,
    });
    const pipelines = [
        { key: 'airflow:billing_invoices_load', name: 'billing_invoices_load', health: 'sla_missed', runs: [
            run('airflow:billing_invoices_load', 3, 'success', 7 * HOUR, 230), run('airflow:billing_invoices_load', 2, 'success', 31 * HOUR, 41),
            run('airflow:billing_invoices_load', 1, 'success', 55 * HOUR, 39)], slas: ['invoices-by-6'] },
        { key: 'airflow:orders_hourly', name: 'orders_hourly', health: 'healthy', runs: [
            run('airflow:orders_hourly', 3, 'success', 50 * 60_000, 6), run('airflow:orders_hourly', 2, 'success', 110 * 60_000, 5),
            run('airflow:orders_hourly', 1, 'success', 170 * 60_000, 7)], slas: [] },
        { key: 'airflow:marketing_spend_sync', name: 'marketing_spend_sync', health: 'failing', runs: [
            run('airflow:marketing_spend_sync', 2, 'failed', 3 * HOUR, 2, 'BigQuery: 403 Access Denied on marketing.ads'),
            run('airflow:marketing_spend_sync', 1, 'success', 27 * HOUR, 9)], slas: [] },
        { key: 'airflow:inventory_snapshot', name: 'inventory_snapshot', health: 'healthy', runs: [
            run('airflow:inventory_snapshot', 1, 'running', 12 * 60_000, 0)], slas: [] },
    ];
    const summary = (p: (typeof pipelines)[number]) => {
        const done = p.runs.filter((r) => r.duration_ms !== null);
        const ok = done.filter((r) => r.state === 'success').length;
        const ds = done.map((r) => r.duration_ms as number).sort((a, b) => a - b);
        return {
            key: p.key, source: 'airflow', name: p.name, health: p.health, runs: p.runs.length,
            success_rate: done.length ? ok / done.length : null, p50_ms: ds[Math.floor(ds.length / 2)] ?? null, p95_ms: ds[ds.length - 1] ?? null,
            trend: p.runs.slice().reverse().map((r) => ({ run_id: r.run_id, state: r.state, duration_ms: r.duration_ms, start: r.start })),
            anomalies: p.key.endsWith('invoices_load') ? 1 : 0, last_run: p.runs[0], slas: p.slas,
        };
    };
    on('GET', '/v1/observability/pipelines', (_b, q) => ({
        items: pipelines.filter((p) => !q.get('source') || q.get('source') === 'airflow').map(summary),
    }));
    on('POST', '/v1/observability/pipelines/sync', () => ({ runs: 0, airflows: 1 }));
    on('GET', '/v1/observability/pipelines/:id', (_b, _q, [key]) => {
        const p = pipelines.find((x) => x.key === key) ?? fail(404, 'no such pipeline');
        return { ...summary(p), items: p.runs, runs_list: p.runs };
    });
    on('GET', '/v1/observability/pipelines/:id/runs/:id', (_b, _q, [key, runId]) => {
        const p = pipelines.find((x) => x.key === key) ?? fail(404, 'no such pipeline');
        const r = p.runs.find((x) => x.run_id === runId) ?? fail(404, 'no such run');
        return { ...r, airflow_trace_id: null, tasks: [], impact: { tables: [], assets: [], data_products: [], pipelines: [] }, quality: [] };
    });
    on('GET', '/v1/observability/pipelines/:id/runs/:id/diagnose', () => ({ causes: [], suggestions: [], log: { task: null, tail: [] } }));
    on('GET', '/v1/observability/pipeline-slas', () => ({ items: [
        { id: 'invoices-by-6', name: 'Invoices loaded by 06:00', pipeline: 'airflow:billing_invoices_load', data_product: 'revenue',
            deadline: '06:00', timezone: 'Europe/London', max_duration: '2h', freshness: '24h', on_failure: true, anomalies: true, severity: 'warning',
            last_evaluation: { missed: 1 } },
    ] }));
    on('GET', '/v1/observability/data-products', () => ({ items: [
        { id: 'revenue', name: 'Revenue reporting', tables: ['analytics.finance.revenue_daily', 'billing.public.invoices'],
            pipelines: ['airflow:billing_invoices_load'], description: 'Daily net revenue for finance' },
    ] }));

    // ------------------------------------------------------------------------------------------ scheduled jobs
    on('GET', '/v1/jobs/:id/runs', (_b, _q, [name]) => {
        const at = (ago: number) => now - ago;
        if (name === 'one-off-backfill') {
            return [{ id: `${name}-1`, status: 'running', trigger: 'manual', started_ms: at(40 * 60_000), finished_ms: null, error: null }];
        }
        return [
            { id: `${name}-3`, status: 'succeeded', trigger: 'schedule', started_ms: at(5 * HOUR), finished_ms: at(5 * HOUR - 8 * 60_000), error: null },
            { id: `${name}-2`, status: 'succeeded', trigger: 'schedule', started_ms: at(29 * HOUR), finished_ms: at(29 * HOUR - 9 * 60_000), error: null },
            { id: `${name}-1`, status: 'failed', trigger: 'schedule', started_ms: at(53 * HOUR), finished_ms: at(53 * HOUR - 60_000),
                error: 'KeyError: store_id' },
        ];
    });

    // ------------------------------------------------------------------------- a managed Airflow deployment's detail
    on('GET', '/v1/airflow/deployments/:id/status', () => ({
        health: 'healthy', problems: [], scheduler_heartbeat_age_s: 4, tasks: { running: 1, queued: 0 },
        components: { metadatabase: { status: 'healthy' }, scheduler: { status: 'healthy' }, dag_processor: { status: 'healthy' },
            triggerer: { status: 'healthy' } },
    }));
    on('GET', '/v1/airflow/deployments/:id/deploys', (_b, _q, [name]) => [
        { id: `${name}-d2`, seq: 2, kind: 'dags', state: 'live', image: null, sha256: 'b41c09e2', files: 6, git_sha: '9f2c1ab', note: 'Invoices DAG retries',
            source: 'git', created_by: 'urn:li:corpuser:raj', created_ms: now - 2 * DAY, checked: { dags: ['billing_invoices_load', 'orders_hourly'] }, error: null },
        { id: `${name}-d1`, seq: 1, kind: 'dags', state: 'superseded', image: null, sha256: '77ad0c41', files: 5, git_sha: '1c0e7d2', note: null,
            source: 'git', created_by: 'urn:li:corpuser:raj', created_ms: now - 9 * DAY, checked: { dags: ['orders_hourly'] }, error: null },
    ]);
    on('GET', '/v1/airflow/deployments/:id/promotions', () => []);
    // ------------------------------------------------------------------------- a managed NiFi flow's status
    on('GET', '/v1/nifi/flows/:id/status', (_b, _q, [name]) => ({
        name, state: 'running', counts: { flowFilesReceived: 1200, flowFilesSent: 0, flowFilesIn: 1200, flowFilesOut: 1200, queuedCount: 0 },
        processors: [{ name: 'Query contacts', type: 'QueryDatabaseTableRecord', run_status: 'Running', in: 0, out: 1200 },
            { name: 'Land in the lakehouse', type: 'PutIcebergRecord', run_status: 'Running', in: 1200, out: 1200 }],
        bulletins: [],
    }));
    on('GET', '/v1/airflow/deployments/:id/logs', (_b, q) => ({
        log: `[${iso(60_000)}] INFO - ${q.get('component') ?? 'scheduler'}: heartbeat ok\n[${iso(30_000)}] INFO - 1 task running, 0 queued`,
    }));
    on('GET', '/v1/system-tables', () => ({ catalog: 'system', interval_seconds: 300, freshness_seconds: 120, tables: [] }));
    on('GET', '/catalog/products', () => ({ products: [] }));

    // ------------------------------------------------------------------------------------------- small reads
    on('GET', '/catalog/glossary/rights', () => ({ may_manage: true, reason: '', may_propose: true }));
    on('GET', '/catalog/domains/rights', () => ({ may_manage: true, reason: '' }));
    on('GET', '/v1/me/devices', () => ({ devices: [
        { id: 'dev-laptop', name: 'MacBook Pro', client: 'Chrome', signed_in_ms: now - 3 * DAY, last_used_ms: now - 5 * 60_000,
            expires_ms: now + 27 * DAY, current: true },
    ] }));
    on('GET', '/governance/audit', () => [
        { seq: 3, ts: iso(20 * 60_000), category: 'access', action: 'request.approved', actor: 'urn:li:corpuser:raj', subject: me },
        { seq: 2, ts: iso(3 * HOUR), category: 'policy', action: 'persona.updated', actor: 'urn:li:corpuser:tom', subject: 'finance-analysts' },
        { seq: 1, ts: iso(26 * HOUR), category: 'people', action: 'user.provisioned', actor: 'scim', subject: 'urn:li:corpuser:ken' },
    ]);
    on('GET', '/v1/ml/monitors', () => []);
    on('GET', '/sql/metric-alerts', () => []);
    on('GET', '/sql/reports', () => []);
    on('GET', '/marketplace/publications', () => []);
}
