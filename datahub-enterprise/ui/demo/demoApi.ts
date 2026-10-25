/**
 * In-browser stand-in for the DHE API + DataHub GraphQL, used by the standalone demo build.
 * State is held in memory for the session, so bookmarks, discussions, access requests and
 * findings behave like the real backend until the page is reloaded. All data is fictional.
 */

type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

const ME = 'urn:li:corpuser:alice';
const PEOPLE: Record<string, string> = {
    [ME]: 'Alice Moreno',
    'urn:li:corpuser:raj': 'Raj Patel',
    'urn:li:corpuser:mei': 'Mei Lin',
    'urn:li:corpuser:tom': 'Tom Becker',
    'urn:li:corpuser:dhe-context-agent': 'Context Agent',
};
const DAY = 86_400_000;
const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();
const ds = (platform: string, name: string) => `urn:li:dataset:(urn:li:dataPlatform:${platform},${name},PROD)`;

const FINANCE = 'urn:li:domain:finance';
const MARKETING = 'urn:li:domain:marketing';
const SUPPLY = 'urn:li:domain:supply-chain';
const DOMAINS: Record<string, string> = { [FINANCE]: 'Finance', [MARKETING]: 'Marketing', [SUPPLY]: 'Supply Chain' };

interface Col {
    fieldPath: string;
    nativeDataType: string;
    description?: string;
    terms?: string[];
}
interface Asset {
    urn: string;
    type: string;
    name: string;
    platform: string | null;
    description: string;
    domain: string | null;
    owners: string[];
    tags: string[];
    terms: string[];
    columns?: Col[];
    certification: string | null;
    quality: string | null;
    views: number;
}

const PII_EMAIL = 'urn:li:glossaryTerm:Classification.PII.Email';
const PII_PHONE = 'urn:li:glossaryTerm:Classification.PII.Phone';
const T_REVENUE = 'urn:li:glossaryTerm:Finance.NetRevenue';
const T_CUSTOMER = 'urn:li:glossaryTerm:Customer.ActiveCustomer';

const ORDERS = ds('snowflake', 'analytics.core.orders');
const CUSTOMERS = ds('snowflake', 'analytics.core.customers');
const REV_DAILY = ds('snowflake', 'analytics.finance.revenue_daily');
const FCT_ORDERS = ds('dbt', 'analytics.marts.fct_orders');
const INVOICES = ds('postgres', 'billing.public.invoices');
const SHIPMENTS = ds('snowflake', 'analytics.supply.shipments');
const INVENTORY = ds('postgres', 'wms.public.inventory_levels');
const CAMPAIGNS = ds('bigquery', 'marketing.ads.campaign_spend');
const DASH_REV = 'urn:li:dashboard:(looker,revenue_overview)';
const CHART_REV = 'urn:li:chart:(looker,revenue_by_region)';
const DASH_CX = 'urn:li:dashboard:(tableau,customer_health)';

const ASSETS: Asset[] = [
    {
        urn: ORDERS, type: 'DATASET', name: 'orders', platform: 'snowflake', domain: FINANCE,
        description: 'One row per customer order, loaded hourly from the commerce platform. Source of truth for order counts and gross merchandise value.',
        owners: ['urn:li:corpuser:raj'], tags: ['urn:li:tag:Tier1'], terms: [], certification: 'CERTIFIED', quality: 'HEALTHY', views: 41,
        columns: [
            { fieldPath: 'order_id', nativeDataType: 'VARCHAR', description: 'Order identifier' },
            { fieldPath: 'customer_id', nativeDataType: 'VARCHAR', description: 'Joins to customers.id' },
            { fieldPath: 'order_ts', nativeDataType: 'TIMESTAMP_NTZ', description: 'When the order was placed (UTC)' },
            { fieldPath: 'amount', nativeDataType: 'NUMBER(12,2)', description: 'Order total before discounts', terms: [] },
            { fieldPath: 'discount', nativeDataType: 'NUMBER(12,2)', description: 'Promotional discount applied' },
            { fieldPath: 'region', nativeDataType: 'VARCHAR', description: 'Sales region' },
            { fieldPath: 'contact_email', nativeDataType: 'VARCHAR', description: 'Email captured at checkout', terms: [PII_EMAIL] },
        ],
    },
    {
        urn: CUSTOMERS, type: 'DATASET', name: 'customers', platform: 'snowflake', domain: MARKETING,
        description: 'Customer master with lifecycle status and contact details. Contains PII; samples are masked unless you hold the Customer Support purpose.',
        owners: ['urn:li:corpuser:mei'], tags: ['urn:li:tag:Tier1'], terms: [T_CUSTOMER], certification: 'CERTIFIED', quality: 'WARNING', views: 33,
        columns: [
            { fieldPath: 'id', nativeDataType: 'VARCHAR', description: 'Customer identifier' },
            { fieldPath: 'email', nativeDataType: 'VARCHAR', description: 'Primary email', terms: [PII_EMAIL] },
            { fieldPath: 'phone', nativeDataType: 'VARCHAR', description: 'Mobile number', terms: [PII_PHONE] },
            { fieldPath: 'status', nativeDataType: 'VARCHAR', description: 'active | churned | prospect', terms: [T_CUSTOMER] },
            { fieldPath: 'signup_date', nativeDataType: 'DATE' },
        ],
    },
    {
        urn: REV_DAILY, type: 'DATASET', name: 'revenue_daily', platform: 'snowflake', domain: FINANCE,
        description: 'Daily net revenue by region. Feeds the Revenue Overview dashboard and the board pack.',
        owners: ['urn:li:corpuser:raj'], tags: [], terms: [T_REVENUE], certification: 'CERTIFIED', quality: 'FAILING', views: 57,
        columns: [
            { fieldPath: 'day', nativeDataType: 'DATE' },
            { fieldPath: 'region', nativeDataType: 'VARCHAR' },
            { fieldPath: 'net_revenue', nativeDataType: 'NUMBER(14,2)', description: 'SUM(amount - discount)', terms: [T_REVENUE] },
            { fieldPath: 'orders', nativeDataType: 'NUMBER' },
        ],
    },
    {
        urn: FCT_ORDERS, type: 'DATASET', name: 'fct_orders', platform: 'dbt', domain: FINANCE,
        description: 'dbt mart joining orders to customers with net amounts.',
        owners: ['urn:li:corpuser:tom'], tags: [], terms: [], certification: null, quality: 'HEALTHY', views: 18,
        columns: [
            { fieldPath: 'order_id', nativeDataType: 'VARCHAR' },
            { fieldPath: 'customer_email', nativeDataType: 'VARCHAR', terms: [PII_EMAIL], description: 'Propagated PII from orders.contact_email' },
            { fieldPath: 'net_amount', nativeDataType: 'NUMBER(12,2)' },
        ],
    },
    {
        urn: INVOICES, type: 'DATASET', name: 'invoices', platform: 'postgres', domain: FINANCE,
        description: 'Billing system invoices (operational database replica).',
        owners: ['urn:li:corpuser:tom'], tags: [], terms: [], certification: null, quality: null, views: 7,
        columns: [
            { fieldPath: 'invoice_id', nativeDataType: 'uuid' },
            { fieldPath: 'order_id', nativeDataType: 'varchar' },
            { fieldPath: 'total', nativeDataType: 'numeric' },
        ],
    },
    {
        urn: SHIPMENTS, type: 'DATASET', name: 'shipments', platform: 'snowflake', domain: SUPPLY,
        description: 'Carrier shipment events with promised and actual delivery dates.',
        owners: ['urn:li:corpuser:tom'], tags: [], terms: [], certification: 'CERTIFIED', quality: 'HEALTHY', views: 22,
    },
    {
        urn: INVENTORY, type: 'DATASET', name: 'inventory_levels', platform: 'postgres', domain: SUPPLY,
        description: 'On-hand stock per SKU and warehouse, snapshotted every 15 minutes.',
        owners: ['urn:li:corpuser:tom'], tags: [], terms: [], certification: null, quality: 'HEALTHY', views: 12,
    },
    {
        urn: CAMPAIGNS, type: 'DATASET', name: 'campaign_spend', platform: 'bigquery', domain: MARKETING,
        description: 'Daily ad spend and attributed conversions per campaign.',
        owners: ['urn:li:corpuser:mei'], tags: [], terms: [], certification: null, quality: 'HEALTHY', views: 15,
    },
    {
        urn: DASH_REV, type: 'DASHBOARD', name: 'Revenue Overview', platform: 'looker', domain: FINANCE,
        description: 'Executive revenue dashboard: net revenue, orders and AOV by region.',
        owners: ['urn:li:corpuser:raj'], tags: [], terms: [T_REVENUE], certification: 'CERTIFIED', quality: null, views: 64,
    },
    {
        urn: CHART_REV, type: 'CHART', name: 'Revenue by region', platform: 'looker', domain: FINANCE,
        description: 'Stacked net revenue by region, last 90 days.', owners: ['urn:li:corpuser:raj'], tags: [], terms: [],
        certification: null, quality: null, views: 20,
    },
    {
        urn: DASH_CX, type: 'DASHBOARD', name: 'Customer Health', platform: 'tableau', domain: MARKETING,
        description: 'Active customers, churn and NPS by cohort.', owners: ['urn:li:corpuser:mei'], tags: [], terms: [T_CUSTOMER],
        certification: null, quality: null, views: 26,
    },
];
const byUrn = new Map(ASSETS.map((a) => [a.urn, a]));

const LINEAGE: Record<string, string[]> = {
    // downstream → upstreams
    [FCT_ORDERS]: [ORDERS, CUSTOMERS],
    [REV_DAILY]: [FCT_ORDERS, INVOICES],
    [CHART_REV]: [REV_DAILY],
    [DASH_REV]: [CHART_REV],
    [DASH_CX]: [CUSTOMERS],
};

const PRODUCTS = [
    {
        urn: 'urn:li:dataProduct:orders', name: 'Orders & Revenue', domain: FINANCE,
        description: 'Certified order and net revenue data for finance reporting and forecasting.',
        owners: ['urn:li:corpuser:raj'], tags: ['urn:li:tag:Tier1'], certification: 'CERTIFIED', quality_status: 'FAILING',
        assets: [ORDERS, REV_DAILY, FCT_ORDERS, DASH_REV], consumers: 23,
        sla: { freshness: 'hourly, 30 min lag', availability: '99.5%' },
        policy: { auto_approve: true, max_duration_days: 90, purposes: ['Financial reporting', 'Forecasting', 'Other'], approvers: ['urn:li:corpuser:raj'] },
        readme: '## What this product gives you\n\n- **orders**: one row per order, hourly\n- **revenue_daily**: net revenue by region and day\n- **Revenue Overview**: the executive dashboard\n\nNet revenue is `SUM(amount - discount)`. Refunds are excluded until the refunds feed lands in Q1.',
    },
    {
        urn: 'urn:li:dataProduct:customer-360', name: 'Customer 360', domain: MARKETING,
        description: 'Unified customer profile with lifecycle status. Contact fields are PII and masked outside approved purposes.',
        owners: ['urn:li:corpuser:mei'], tags: ['urn:li:tag:PII'], certification: 'CERTIFIED', quality_status: 'WARNING',
        assets: [CUSTOMERS, DASH_CX, CAMPAIGNS], consumers: 11,
        sla: { freshness: 'daily by 06:00 UTC', availability: '99%' },
        policy: { auto_approve: false, max_duration_days: 30, purposes: ['Customer support', 'Churn analysis', 'Marketing attribution'], approvers: ['urn:li:corpuser:mei'] },
        readme: '## Customer 360\n\nAccess requires a stated purpose. Email and phone stay masked unless your purpose is **Customer support**.',
    },
    {
        urn: 'urn:li:dataProduct:supply-kpis', name: 'Supply Chain KPIs', domain: SUPPLY,
        description: 'On-time delivery, inventory cover and carrier performance.',
        owners: ['urn:li:corpuser:tom'], tags: [], certification: null, quality_status: 'HEALTHY',
        assets: [SHIPMENTS, INVENTORY], consumers: 6,
        sla: { freshness: 'every 15 min', availability: '99%' },
        policy: { auto_approve: true, max_duration_days: 180, purposes: [], approvers: ['urn:li:corpuser:tom'] },
        readme: '## Supply Chain KPIs\n\nOn-time delivery is measured against the promised date at order time.',
    },
];

// ------------------------------------------------------------------------------- mutable state
let seq = 100;
const id = (p: string) => `${p}_${(seq++).toString(36)}`;
const state = {
    bookmarks: [{ urn: REV_DAILY, note: 'Board pack source', created_at: iso(3 * DAY) }] as Json[],
    follows: [{ urn: REV_DAILY, created_at: iso(3 * DAY) }, { urn: CUSTOMERS, created_at: iso(9 * DAY) }] as Json[],
    recent: [REV_DAILY, DASH_REV, CUSTOMERS, ORDERS] as string[],
    subscriptions: new Set<string>(['urn:li:dataProduct:orders']),
    threads: [
        {
            id: 'thr_1', entity_urn: REV_DAILY, field_path: 'net_revenue', status: 'OPEN', created_by: 'urn:li:corpuser:mei', created_at: iso(2 * DAY),
            comments: [
                { id: 'cmt_1', thread_id: 'thr_1', author: 'urn:li:corpuser:mei', body_md: "Yesterday's net revenue for EMEA is about 40% below forecast. Is this a data issue? @raj", created_at: iso(2 * DAY), edited_at: null, mentions: ['urn:li:corpuser:raj'], reactions: { '👀': ['urn:li:corpuser:tom'] } },
                { id: 'cmt_2', thread_id: 'thr_1', author: 'urn:li:corpuser:raj', body_md: 'Looks like the invoices load from billing ran late. Checking the upstream job now.', created_at: iso(1.8 * DAY), edited_at: null, mentions: [], reactions: { '👍': [ME, 'urn:li:corpuser:mei'] } },
            ],
        },
        {
            id: 'thr_2', entity_urn: CUSTOMERS, field_path: null, status: 'RESOLVED', created_by: ME, created_at: iso(12 * DAY),
            comments: [
                { id: 'cmt_3', thread_id: 'thr_2', author: ME, body_md: 'Does `status = active` include customers with a paused subscription?', created_at: iso(12 * DAY), edited_at: null, mentions: [], reactions: {} },
                { id: 'cmt_4', thread_id: 'thr_2', author: 'urn:li:corpuser:mei', body_md: 'No. Paused customers are `churned` after 60 days, otherwise `active`. Added this to the glossary term.', created_at: iso(11 * DAY), edited_at: null, mentions: [], reactions: { '🎉': [ME] } },
            ],
        },
    ] as Json[],
    requests: [
        {
            id: 'ar_101', product_urn: 'urn:li:dataProduct:supply-kpis', product_name: 'Supply Chain KPIs', requester: ME, purpose: 'Forecasting',
            justification: 'Inventory cover feeds the Q4 demand forecast.', duration_days: 90, state: 'APPROVED', auto_approved: true,
            approvers: ['urn:li:corpuser:tom'], decided_by: 'urn:li:corpuser:dhe-marketplace', decided_at: iso(20 * DAY), expires_at: iso(-70 * DAY), created_at: iso(20 * DAY),
            history: [{ at: iso(20 * DAY), actor: ME, from_state: null, to_state: 'PENDING', comment: null }, { at: iso(20 * DAY), actor: 'urn:li:corpuser:dhe-marketplace', from_state: 'PENDING', to_state: 'APPROVED', comment: 'auto-approved by product policy' }],
        },
        {
            id: 'ar_102', product_urn: 'urn:li:dataProduct:orders', product_name: 'Orders & Revenue', requester: 'urn:li:corpuser:mei', purpose: 'Marketing attribution',
            justification: 'Need order-level revenue to attribute campaign ROI for the holiday push.', duration_days: 30, state: 'PENDING', auto_approved: false,
            approvers: [ME, 'urn:li:corpuser:raj'], decided_by: null, decided_at: null, expires_at: null, created_at: iso(5 * 3600_000),
            history: [{ at: iso(5 * 3600_000), actor: 'urn:li:corpuser:mei', from_state: null, to_state: 'PENDING', comment: 'Need order-level revenue to attribute campaign ROI.' }],
        },
    ] as Json[],
    findings: [
        {
            id: 'fnd_1', type: 'DEFINITION_DRIFT', title: '“net revenue” has 2 conflicting definitions', confidence: 0.85, status: 'OPEN', created_at: iso(1 * DAY),
            summary_md: 'The business metric **net revenue** is defined differently across sources:\n- `SUM(amount - discount)` — 14× in queries, dbt `revenue_daily`\n- `SUM(amount)` — 3× in Looker explore `orders`',
            entities: [REV_DAILY, ORDERS, T_REVENUE],
            evidence: [{ source: 'dbt', ref: 'model.revenue_daily', snippet: 'sum(amount - discount) as net_revenue' }, { source: 'looker', ref: 'orders.view.lkml', snippet: 'measure: net_revenue { sql: SUM(${amount}) ;; }' }],
            proposed_changes: [{ kind: 'open_discussion', entity_urn: REV_DAILY, field_path: null, value: 'Align net revenue definition' }],
        },
        {
            id: 'fnd_2', type: 'TERM_LINK', title: 'Link “Active Customer” to customers.status', confidence: 0.78, status: 'OPEN', created_at: iso(2 * DAY),
            summary_md: '`status` is filtered with `= \'active\'` in 22 queries that also reference the **Active Customer** glossary term in their dashboards.',
            entities: [CUSTOMERS, T_CUSTOMER], evidence: [{ source: 'query', ref: 'urn:li:query:7f1', snippet: "where c.status = 'active'" }],
            proposed_changes: [{ kind: 'add_term', entity_urn: CUSTOMERS, field_path: 'status', value: T_CUSTOMER }],
        },
        {
            id: 'fnd_3', type: 'JOIN_RELATIONSHIP', title: 'orders.customer_id → customers.id', confidence: 0.92, status: 'OPEN', created_at: iso(3 * DAY),
            summary_md: 'Joined on `orders.customer_id = customers.id` in 37 queries. Propose recording it as an ER relationship.',
            entities: [ORDERS, CUSTOMERS], evidence: [{ source: 'query', ref: 'urn:li:query:a21', snippet: 'from orders o join customers c on o.customer_id = c.id' }],
            proposed_changes: [{ kind: 'create_relationship', entity_urn: ORDERS, field_path: 'customer_id', value: { to: CUSTOMERS, field: 'id' } }],
        },
        {
            id: 'fnd_4', type: 'RUN_ANOMALY', title: 'billing_invoices_load ran 3h 40m late', confidence: 0.88, status: 'OPEN', created_at: iso(1.9 * DAY),
            summary_md: 'The Airflow task `billing_invoices_load` usually finishes by 02:10 UTC. On its last run it finished at 05:50 UTC, after `revenue_daily` had already built.',
            entities: [INVOICES, REV_DAILY], evidence: [{ source: 'airflow', ref: 'billing.billing_invoices_load', snippet: 'end 05:50 UTC (p95 02:10)' }],
            proposed_changes: [],
        },
        {
            id: 'fnd_5', type: 'METRIC_DEFINITION', title: 'Metric “aov” = SUM(amount) / COUNT(DISTINCT order_id)', confidence: 0.7, status: 'OPEN', created_at: iso(4 * DAY),
            summary_md: '`aov` is computed the same way in 9 places (queries, Looker). No glossary term exists yet.',
            entities: [ORDERS], evidence: [{ source: 'looker', ref: 'orders.view.lkml', snippet: 'measure: aov' }],
            proposed_changes: [{ kind: 'create_term', entity_urn: null, field_path: null, value: { name: 'Average Order Value' } }],
        },
    ] as Json[],
    inbox: [
        { id: 'ntf_1', type: 'access.requested', title: 'Access request: Orders & Revenue', body: 'mei requests 30 days of access for “Marketing attribution”.', entity_urn: 'urn:li:dataProduct:orders', created_at: iso(5 * 3600_000), read_at: null, action_url: '#/requests' },
        { id: 'ntf_2', type: 'quality.incident.raised', title: 'Quality incident: revenue_daily volume', body: 'Row count for EMEA is 41% below the expected range.', entity_urn: REV_DAILY, created_at: iso(1.9 * DAY), read_at: null, action_url: `#/asset/${encodeURIComponent(REV_DAILY)}` },
        { id: 'ntf_3', type: 'context.drift', title: '“net revenue” has 2 conflicting definitions', body: 'Looker and dbt disagree on discounts.', entity_urn: REV_DAILY, created_at: iso(1 * DAY), read_at: null, action_url: '#/context' },
        { id: 'ntf_4', type: 'discussion.reply', title: 'New reply on customers', body: 'Paused customers are churned after 60 days…', entity_urn: CUSTOMERS, created_at: iso(11 * DAY), read_at: iso(10 * DAY), action_url: null },
    ] as Json[],
    personas: [
        {
            id: 'finance-analysts', name: 'Finance analysts', description: 'Analysts in FP&A and the finance data team.', color: '#533fd1',
            members: { users: [ME], groups: ['urn:li:corpGroup:fpna'], attribute_rules: [{ attr: 'department', op: 'eq', value: 'Finance' }] },
            scope: { domains: [FINANCE], data_products: [], platforms: [], tags: [], glossary_terms: [], asset_urns: [] },
            permissions: ['view', 'view_samples', 'view_queries', 'comment', 'request_access'], enabled: true, updated_at: iso(30 * DAY),
        },
        {
            id: 'marketing', name: 'Marketing', description: 'Growth and lifecycle marketing.', color: '#0f9d58',
            members: { users: ['urn:li:corpuser:mei'], groups: ['urn:li:corpGroup:marketing'], attribute_rules: [] },
            scope: { domains: [MARKETING], data_products: [], platforms: [], tags: [], glossary_terms: [], asset_urns: [] },
            permissions: ['view', 'view_samples', 'comment', 'request_access', 'edit_docs'], enabled: true, updated_at: iso(45 * DAY),
        },
    ] as Json[],
    purposes: [
        {
            id: 'customer-pii', name: 'Customer support', description: 'Contact details are visible only when helping a customer.',
            tags: ['PII.Email', 'PII.Phone'], allowed: { users: [], groups: ['urn:li:corpGroup:support'], personas: [] },
            deny: ['view_samples', 'view_queries'], mask: { enabled: true, strategy: 'partial' }, enabled: true, updated_at: iso(60 * DAY),
        },
    ] as Json[],
    notify: {
        channels: { slack: { enabled: true, user_id: 'U04ALICE' }, teams: { enabled: false, webhook_url: null }, email: { enabled: false }, in_app: { enabled: true } },
        events: {
            'access.requested': true, 'access.granted': true, 'certification.requested': true, 'quality.incident.raised': true,
            'discussion.mention': true, 'discussion.reply': true, 'context.drift': true, 'asset.deprecated': true,
        },
    } as Json,
};

const PERMISSION_BUNDLES: Record<string, string[]> = {
    view: ['VIEW_ENTITY_PAGE', 'SEARCH_PRIVILEGE', 'GET_ENTITY_PRIVILEGE'],
    view_samples: ['VIEW_DATASET_PROFILE'],
    view_queries: ['VIEW_ENTITY_QUERIES'],
    view_usage: ['VIEW_DATASET_USAGE'],
    comment: ['DHE_COMMENT'],
    request_access: ['DHE_REQUEST_ACCESS'],
    edit_docs: ['EDIT_ENTITY_DOCS', 'EDIT_ENTITY_DOC_LINKS'],
    edit_tags: ['EDIT_ENTITY_TAGS', 'EDIT_DATASET_COL_TAGS'],
    edit_terms: ['EDIT_ENTITY_GLOSSARY_TERMS', 'EDIT_DATASET_COL_GLOSSARY_TERMS'],
    edit_owners: ['EDIT_ENTITY_OWNERS'],
    manage_quality: ['DHE_MANAGE_MONITORS'],
    certify: ['DHE_APPROVE_CERTIFICATION'],
    approve_access: ['DHE_APPROVE_ACCESS'],
};

// ------------------------------------------------------------------------------- helpers
function summary(a: Asset) {
    return { urn: a.urn, name: a.name, type: a.type, platform: a.platform, description: a.description };
}
function engagement(urn: string) {
    const a = byUrn.get(urn);
    const p = PRODUCTS.find((x) => x.urn === urn);
    return {
        views_7d: a?.views ?? p?.consumers ?? 0,
        bookmarks: state.bookmarks.filter((b) => b.urn === urn).length + (a ? Math.floor(a.views / 12) : 0),
        bookmarked: state.bookmarks.some((b) => b.urn === urn),
        following: state.follows.some((f) => f.urn === urn),
        threads_open: state.threads.filter((t) => t.entity_urn === urn && t.status === 'OPEN').length,
        certification: a?.certification ?? p?.certification ?? null,
        quality_status: a?.quality ?? p?.quality_status ?? null,
    };
}
function access(productUrn: string) {
    const mine = state.requests.filter((r) => r.product_urn === productUrn && r.requester === ME);
    const r = mine[mine.length - 1];
    if (!r) return { status: 'NONE', request_id: null, expires_at: null };
    const status = r.state === 'APPROVED' ? 'GRANTED' : ['PENDING', 'REJECTED', 'EXPIRED'].includes(r.state) ? r.state : 'NONE';
    return { status, request_id: r.id, expires_at: r.expires_at };
}
function card(p: (typeof PRODUCTS)[number]) {
    return {
        urn: p.urn, name: p.name, description: p.description, domain: { urn: p.domain, name: DOMAINS[p.domain] },
        owners: p.owners, tags: p.tags, certification: p.certification, quality_status: p.quality_status,
        assets_count: p.assets.length, consumers: p.consumers + (access(p.urn).status === 'GRANTED' ? 1 : 0), sla: p.sla,
        access: access(p.urn), request_policy: p.policy,
    };
}
function nameOf(urn: string) {
    return byUrn.get(urn)?.name ?? PRODUCTS.find((p) => p.urn === urn)?.name ?? PEOPLE[urn] ?? urn.split(/[:.,]/).filter(Boolean).pop() ?? urn;
}
function notify(item: Omit<Json, 'id' | 'created_at' | 'read_at'>) {
    state.inbox.unshift({ id: id('ntf'), created_at: new Date().toISOString(), read_at: null, ...item });
}

// ------------------------------------------------------------------------------- GraphQL
function gqlSearch(input: Json) {
    const q = String(input.query || '*').toLowerCase().trim();
    const types: string[] = input.types ?? [];
    const clauses: { field: string; values: string[] }[] = input.orFilters?.[0]?.and ?? [];
    const base = ASSETS.filter((a) => {
        if (q && q !== '*') {
            const hay = `${a.name} ${a.description} ${a.platform} ${(a.columns ?? []).map((c) => c.fieldPath).join(' ')}`.toLowerCase();
            if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
        }
        return true;
    });
    const matches = (a: Asset, field: string, values: string[]) => {
        if (field === 'platform') return values.includes(`urn:li:dataPlatform:${a.platform}`);
        if (field === 'domains') return !!a.domain && values.includes(a.domain);
        if (field === 'tags') return a.tags.some((t) => values.includes(t));
        if (field === 'glossaryTerms') return a.terms.some((t) => values.includes(t));
        if (field === '_entityType') return values.includes(a.type);
        return true;
    };
    const filtered = base.filter((a) => (!types.length || types.includes(a.type)) && clauses.every((c) => matches(a, c.field, c.values)));
    const count = (field: string, keyOf: (a: Asset) => string[]) => {
        const m = new Map<string, number>();
        base.filter((a) => clauses.filter((c) => c.field !== field).every((c) => matches(a, c.field, c.values))
            && (field === '_entityType' || !types.length || types.includes(a.type)))
            .forEach((a) => keyOf(a).forEach((k) => m.set(k, (m.get(k) ?? 0) + 1)));
        return [...m.entries()].sort((x, y) => y[1] - x[1]);
    };
    const facets = [
        { field: '_entityType', displayName: 'Type', aggregations: count('_entityType', (a) => [a.type]).map(([value, c]) => ({ value, count: c, entity: null })) },
        { field: 'platform', displayName: 'Platform', aggregations: count('platform', (a) => (a.platform ? [`urn:li:dataPlatform:${a.platform}`] : [])).map(([value, c]) => ({ value, count: c, entity: { urn: value, name: value.split(':').pop() } })) },
        { field: 'domains', displayName: 'Domain', aggregations: count('domains', (a) => (a.domain ? [a.domain] : [])).map(([value, c]) => ({ value, count: c, entity: { urn: value, properties: { name: DOMAINS[value] } } })) },
        { field: 'glossaryTerms', displayName: 'Glossary term', aggregations: count('glossaryTerms', (a) => a.terms).map(([value, c]) => ({ value, count: c, entity: { urn: value, properties: { name: value.split('.').pop() } } })) },
    ];
    const start = input.start ?? 0;
    return {
        searchAcrossEntities: {
            total: filtered.length,
            searchResults: filtered.slice(start, start + (input.count ?? 20)).map((a) => ({
                entity: { urn: a.urn, type: a.type, name: a.name, platform: a.platform ? { name: a.platform } : null, properties: { name: a.name, description: a.description } },
            })),
            facets,
        },
    };
}

function gqlAsset(urn: string) {
    const a = byUrn.get(urn);
    if (!a) return { entity: null };
    const term = (t: string) => ({ term: { urn: t, properties: { name: t.split('.').pop() } } });
    return {
        entity: {
            urn: a.urn, type: a.type, name: a.name, platform: a.platform ? { name: a.platform } : null,
            properties: { name: a.name, description: a.description }, editableProperties: null,
            domain: a.domain ? { domain: { urn: a.domain, properties: { name: DOMAINS[a.domain] } } } : null,
            ownership: { owners: a.owners.map((o) => ({ owner: { urn: o, username: o.split(':').pop() }, ownershipType: { info: { name: 'Technical Owner' } } })) },
            glossaryTerms: { terms: a.terms.map(term) },
            tags: { tags: a.tags.map((t) => ({ tag: { urn: t, properties: { name: t.split(':').pop() } } })) },
            schemaMetadata: a.columns ? { fields: a.columns.map((c) => ({ fieldPath: c.fieldPath, nativeDataType: c.nativeDataType, description: c.description ?? null, glossaryTerms: { terms: (c.terms ?? []).map(term) } })) } : null,
            editableSchemaMetadata: null,
        },
    };
}

// ------------------------------------------------------------------------------- assistant
function chat(message: string, conversation_id?: string) {
    const m = message.toLowerCase();
    let reply: string;
    let citations: string[];
    let tool_calls: { name: string; summary: string }[];
    if (/why|drop|wrong|broken|low|spike|changed/.test(m)) {
        reply = `**EMEA net revenue dropped because the invoices load ran late.**\n\n` +
            `- \`billing_invoices_load\` finished at 05:50 UTC instead of ~02:10, after [${REV_DAILY}] had already built.\n` +
            `- The volume monitor on revenue_daily flagged EMEA rows 41% below the expected range.\n` +
            `- [${ORDERS}] is healthy, so orders themselves did not drop.\n\n` +
            `A re-run of revenue_daily after 06:00 UTC should restore the numbers. Raj Patel owns this dataset.`;
        citations = [REV_DAILY, INVOICES, ORDERS];
        tool_calls = [{ name: 'explain_lineage', summary: 'explained lineage for revenue_daily' }, { name: 'get_quality_status', summary: 'get quality status(dataset_urn=revenue_daily)' }];
    } else if (/customer|churn|email|pii/.test(m)) {
        reply = `Use [${CUSTOMERS}] (certified, owned by Mei Lin). \`status = 'active'\` follows the **Active Customer** glossary definition.\n\n` +
            `Email and phone are **PII**: samples are masked for you because the *Customer support* purpose doesn't apply. For analysis, request **Customer 360** in the marketplace with the purpose *Churn analysis*.`;
        citations = [CUSTOMERS, DASH_CX];
        tool_calls = [{ name: 'search_assets', summary: 'search assets(query=customers)' }, { name: 'check_access', summary: 'check access(privilege=VIEW_DATASET_PROFILE)' }];
    } else {
        reply = `For revenue reporting, start with the certified [${REV_DAILY}] table (net revenue = \`SUM(amount - discount)\`) or the [${DASH_REV}] dashboard.\n\n` +
            `Note: the Context Agent found that one Looker explore computes net revenue without subtracting discounts, so numbers there can be higher.`;
        citations = [REV_DAILY, DASH_REV];
        tool_calls = [{ name: 'semantic_search', summary: 'semantic search(query=revenue)' }, { name: 'get_asset', summary: 'get asset(urn=revenue_daily)' }];
    }
    return {
        conversation_id: conversation_id || id('conv'), reply_md: reply, citations, tool_calls,
        suggested_actions: [
            { label: 'Open', kind: 'open', urn: citations[0] },
            { label: 'Explain changes', kind: 'explain', urn: citations[0] },
            { label: 'Follow', kind: 'follow', urn: citations[0] },
        ],
    };
}

function explain(urn: string) {
    const ups = LINEAGE[urn] ?? [];
    const failing = urn === REV_DAILY || ups.includes(REV_DAILY) || urn === DASH_REV || urn === CHART_REV;
    if (!failing) {
        return { urn, summary_md: `No anomalies found on **${nameOf(urn)}** or its ${ups.length} upstream asset(s) in the last 7 days.`, likely_causes: [], timeline: [], used_llm: false };
    }
    return {
        urn,
        summary_md: `**${nameOf(urn)}** is affected by a late upstream load. \`billing_invoices_load\` finished 3h 40m late, so **revenue_daily** built with missing EMEA invoices and its volume monitor failed.`,
        likely_causes: [
            { urn: INVOICES, name: 'invoices', reason: 'Upstream load finished 3h 40m late (RUN_ANOMALY)', evidence: ['billing_invoices_load ended 05:50 UTC, p95 02:10'], score: 0.91 },
            { urn: REV_DAILY, name: 'revenue_daily', reason: 'Volume monitor failing: EMEA rows 41% below range', evidence: ['monitor “EMEA row count” FAIL'], score: 0.84 },
            { urn: ORDERS, name: 'orders', reason: 'Healthy: ruled out', evidence: ['freshness + volume PASS'], score: 0.08 },
        ],
        timeline: [
            { at: iso(1.95 * DAY), event: 'revenue_daily built (02:30 UTC)', urn: REV_DAILY },
            { at: iso(1.92 * DAY), event: 'billing_invoices_load finished late (05:50 UTC)', urn: INVOICES },
            { at: iso(1.9 * DAY), event: 'Volume monitor failed, incident raised', urn: REV_DAILY },
        ],
        used_llm: false,
    };
}

function markdownDoc(urn: string) {
    const a = byUrn.get(urn);
    if (!a) return { markdown: `# ${nameOf(urn)}`, workflow_id: null };
    const cols = (a.columns ?? []).map((c) => `| \`${c.fieldPath}\` | ${c.nativeDataType} | ${c.description ?? ''} |`).join('\n');
    const ups = (LINEAGE[a.urn] ?? []).map((u) => `\`${nameOf(u)}\``).join(', ');
    return {
        markdown: `# ${a.name}\n\n**Platform:** ${a.platform} · **Domain:** ${a.domain ? DOMAINS[a.domain] : '—'} · **Owners:** ${a.owners.map((o) => PEOPLE[o]).join(', ')}\n\n## What it is\n\n${a.description}\n\n` +
            (cols ? `## Columns\n\n| Column | Type | Description |\n|---|---|---|\n${cols}\n\n` : '') +
            (ups ? `## Lineage\n\n**Built from:** ${ups}\n\n` : '') + '_Generated by DataHub Enterprise._',
        workflow_id: null,
    };
}

function health(urn: string) {
    const a = byUrn.get(urn);
    const st = a?.quality;
    if (!st) return { dataset_urn: urn, status: 'UNKNOWN', monitors: [], open_incidents: 0 };
    const status = st === 'FAILING' ? 'FAIL' : st === 'WARNING' ? 'WARN' : 'PASS';
    const mon = (mid: string, name: string, type: string, s: string, msg: string, value: number, lo: number, hi: number) => ({
        monitor: { id: mid, name, dataset_urn: urn, type, schedule: { cron: '0 * * * *' }, config: {}, sensitivity: 'MEDIUM', severity: 'HIGH', enabled: true, status: 'ACTIVE', created_at: iso(90 * DAY), updated_at: iso(DAY) },
        last_evaluation: { id: `${mid}_ev`, monitor_id: mid, ts: iso(3600_000), metric_value: value, expected_low: lo, expected_high: hi, status: s, score: null, message: msg, feedback: null },
    });
    return {
        dataset_urn: urn, status, open_incidents: status === 'FAIL' ? 1 : 0,
        monitors: [
            mon(`${a!.name}_fresh`, 'Freshness', 'freshness', 'PASS', 'Updated 42 minutes ago (SLA 2h)', 42, 0, 120),
            mon(`${a!.name}_vol`, status === 'FAIL' ? 'EMEA row count' : 'Row count', 'volume', status, status === 'FAIL' ? 'EMEA rows 41% below expected range' : status === 'WARN' ? 'Null rate on email rose to 3.1%' : 'Within expected range', status === 'FAIL' ? 1180 : 2010, 1900, 2300),
        ],
    };
}

// ------------------------------------------------------------------------------- REST routes
type Handler = (body: Json, q: URLSearchParams, params: string[]) => unknown;
const routes: [string, RegExp, Handler][] = [];
const on = (method: string, pattern: string, h: Handler) =>
    routes.push([method, new RegExp(`^${pattern.replace(/:rest/g, '(.+)').replace(/:id/g, '([^/]+)')}$`), h]);

on('GET', '/ui/config', () => ({
    actor: ME, user: { urn: ME, display_name: PEOPLE[ME] },
    personas: [{ id: 'finance-analysts', name: 'Finance analysts' }], is_admin: true,
    features: { assistant: true, assistant_llm: true, marketplace: true, context: true, semantic_search: true, slack: true, teams: false, email: false, embeds: true, masking_proxy: true },
    slack_enabled: true, teams_enabled: false, embed_allowed_origins: ['https://*.tableau.northwind.example', 'https://looker.northwind.example'],
}));
on('GET', '/collab/trending', (_b, q) => {
    const type = q.get('entity_type');
    return [...ASSETS].filter((a) => !type || a.type === type.toUpperCase()).sort((x, y) => y.views - x.views).slice(0, Number(q.get('limit') ?? 12)).map((a, i) => ({
        ...summary(a), score: a.views * 1.3, views_7d: a.views, unique_viewers_7d: Math.ceil(a.views / 3),
        bookmarks: engagement(a.urn).bookmarks, trend: i === 2 ? 'new' : a.quality === 'FAILING' ? 'up' : 'steady',
        certification: a.certification, quality_status: a.quality,
    }));
});
on('POST', '/collab/enrich', (b) => Object.fromEntries((b.urns as string[]).map((u) => [u, engagement(u)])));
on('GET', '/collab/bookmarks', () => state.bookmarks);
on('PUT', '/collab/bookmarks/:rest', (b, _q, [urn]) => {
    state.bookmarks = state.bookmarks.filter((x) => x.urn !== urn);
    const bm = { urn, note: b?.note ?? null, created_at: new Date().toISOString() };
    state.bookmarks.unshift(bm);
    return bm;
});
on('DELETE', '/collab/bookmarks/:rest', (_b, _q, [urn]) => { state.bookmarks = state.bookmarks.filter((x) => x.urn !== urn); });
on('GET', '/collab/follows', () => state.follows);
on('PUT', '/collab/follows/:rest', (_b, _q, [urn]) => {
    if (!state.follows.some((f) => f.urn === urn)) state.follows.unshift({ urn, created_at: new Date().toISOString() });
    return { urn };
});
on('DELETE', '/collab/follows/:rest', (_b, _q, [urn]) => { state.follows = state.follows.filter((x) => x.urn !== urn); });
on('POST', '/collab/views', (b) => { state.recent = [b.urn, ...state.recent.filter((u) => u !== b.urn)].slice(0, 20); });
on('GET', '/collab/recent', () => state.recent.map((urn, i) => ({ urn, last_viewed_at: iso(i * 3 * 3600_000) })));
on('GET', '/collab/activity', () => [
    { id: 'act_1', type: 'mention', actor: 'urn:li:corpuser:mei', entity_urn: REV_DAILY, entity_name: 'revenue_daily', summary: 'asked about EMEA net revenue on revenue_daily.net_revenue', created_at: iso(2 * DAY) },
    { id: 'act_2', type: 'comment', actor: 'urn:li:corpuser:raj', entity_urn: REV_DAILY, entity_name: 'revenue_daily', summary: 'commented on revenue_daily: Looks like the invoices load from billing ran late.', created_at: iso(1.8 * DAY) },
    { id: 'act_3', type: 'comment', actor: 'urn:li:corpuser:mei', entity_urn: CUSTOMERS, entity_name: 'customers', summary: 'resolved a question on customers', created_at: iso(11 * DAY) },
]);
on('GET', '/collab/threads', (_b, q) => state.threads.filter((t) => t.entity_urn === q.get('entity_urn')));
on('POST', '/collab/threads', (b) => {
    const tid = id('thr');
    const mentions = Array.from(String(b.body_md).matchAll(/@([a-z]+)/g)).map((m) => `urn:li:corpuser:${m[1]}`);
    const t = {
        id: tid, entity_urn: b.entity_urn, field_path: b.field_path ?? null, status: 'OPEN', created_by: ME, created_at: new Date().toISOString(),
        comments: [{ id: id('cmt'), thread_id: tid, author: ME, body_md: b.body_md, created_at: new Date().toISOString(), edited_at: null, mentions, reactions: {} }],
    };
    state.threads.unshift(t);
    return t;
});
on('POST', '/collab/threads/:id/comments', (b, _q, [tid]) => {
    const t = state.threads.find((x) => x.id === tid);
    const c = { id: id('cmt'), thread_id: tid, author: ME, body_md: b.body_md, created_at: new Date().toISOString(), edited_at: null, mentions: [], reactions: {} };
    t.comments.push(c);
    t.status = 'OPEN';
    return c;
});
on('POST', '/collab/threads/:id/:id', (_b, _q, [tid, action]) => {
    const t = state.threads.find((x) => x.id === tid);
    t.status = action === 'resolve' ? 'RESOLVED' : 'OPEN';
    return t;
});
const react = (add: boolean): Handler => (b, _q, [cid]) => {
    for (const t of state.threads) {
        const c = t.comments.find((x: Json) => x.id === cid);
        if (!c) continue;
        const list: string[] = c.reactions[b.emoji] ?? [];
        c.reactions[b.emoji] = add ? Array.from(new Set([...list, ME])) : list.filter((a) => a !== ME);
        if (!c.reactions[b.emoji].length) delete c.reactions[b.emoji];
        return c;
    }
    return null;
};
on('POST', '/collab/comments/:id/reactions', react(true));
on('DELETE', '/collab/comments/:id/reactions', react(false));

on('GET', '/marketplace/products', (_b, q) => {
    const text = (q.get('q') ?? '').toLowerCase();
    let items = PRODUCTS.filter((p) => !text || text === '*' || `${p.name} ${p.description}`.toLowerCase().includes(text)).map(card);
    if (q.get('domain')) items = items.filter((c) => c.domain?.urn === q.get('domain'));
    if (q.get('certified') === 'true') items = items.filter((c) => c.certification === 'CERTIFIED');
    if (q.get('access')) items = items.filter((c) => c.access.status === q.get('access'));
    return items;
});
on('GET', '/marketplace/products/:rest', (_b, _q, [urn]) => {
    const p = PRODUCTS.find((x) => x.urn === urn)!;
    return {
        ...card(p), readme_md: p.readme, subscribed: state.subscriptions.has(urn),
        assets: p.assets.map((u) => { const a = byUrn.get(u)!; return { urn: u, name: a.name, type: a.type, platform: a.platform }; }),
    };
});
on('POST', '/marketplace/products/:rest/subscription', (_b, _q, [urn]) => { state.subscriptions.add(urn); });
on('DELETE', '/marketplace/products/:rest/subscription', (_b, _q, [urn]) => { state.subscriptions.delete(urn); });
on('POST', '/marketplace/access-requests', (b) => {
    const p = PRODUCTS.find((x) => x.urn === b.product_urn)!;
    if (b.duration_days > p.policy.max_duration_days) throw new HttpError(422, `maximum duration for this product is ${p.policy.max_duration_days} days`);
    const status = access(p.urn).status;
    if (status === 'GRANTED' || status === 'PENDING') throw new HttpError(409, `you already have a ${status.toLowerCase()} request for this product`);
    const t = new Date().toISOString();
    const auto = p.policy.auto_approve;
    const r = {
        id: id('ar'), product_urn: p.urn, product_name: p.name, requester: ME, purpose: b.purpose, justification: b.justification ?? '',
        duration_days: b.duration_days, state: auto ? 'APPROVED' : 'PENDING', auto_approved: auto, approvers: p.policy.approvers,
        decided_by: auto ? 'urn:li:corpuser:dhe-marketplace' : null, decided_at: auto ? t : null,
        expires_at: auto ? new Date(Date.now() + b.duration_days * DAY).toISOString() : null, created_at: t,
        history: [{ at: t, actor: ME, from_state: null, to_state: 'PENDING', comment: b.justification || null }]
            .concat(auto ? [{ at: t, actor: 'urn:li:corpuser:dhe-marketplace', from_state: 'PENDING', to_state: 'APPROVED', comment: 'auto-approved by product policy' }] : []),
    };
    state.requests.push(r);
    if (auto) notify({ type: 'access.granted', title: `Access to ${p.name} granted`, body: `Auto-approved for ${b.duration_days} days. Purpose: ${b.purpose}.`, entity_urn: p.urn, action_url: '#/requests' });
    return r;
});
on('GET', '/marketplace/access-requests', (_b, q) => {
    const scope = q.get('scope') ?? 'mine';
    let items = [...state.requests].reverse();
    if (scope === 'mine') items = items.filter((r) => r.requester === ME);
    if (scope === 'inbox') items = items.filter((r) => r.requester !== ME && r.approvers.includes(ME));
    if (q.get('state')) items = items.filter((r) => r.state === q.get('state'));
    return items;
});
on('POST', '/marketplace/access-requests/:id/:id', (b, _q, [rid, action]) => {
    const r = state.requests.find((x) => x.id === rid);
    const to = { approve: 'APPROVED', reject: 'REJECTED', cancel: 'CANCELLED', revoke: 'REVOKED' }[action as 'approve'];
    if ((action === 'approve' || action === 'reject') && r.requester === ME) throw new HttpError(403, 'separation of duties: you cannot approve your own request');
    const t = new Date().toISOString();
    r.history.push({ at: t, actor: ME, from_state: r.state, to_state: to, comment: b?.comment ?? null });
    r.state = to;
    if (to === 'APPROVED') { r.decided_by = ME; r.decided_at = t; r.expires_at = new Date(Date.now() + r.duration_days * DAY).toISOString(); }
    if (to === 'REJECTED') { r.decided_by = ME; r.decided_at = t; }
    return r;
});

on('GET', '/governance/personas/permission-bundles', () => PERMISSION_BUNDLES);
on('GET', '/governance/personas', () => state.personas);
on('POST', '/governance/personas', (b) => { const p = { ...b, id: id('persona'), updated_at: new Date().toISOString() }; state.personas.push(p); return p; });
on('PUT', '/governance/personas/:id', (b, _q, [pid]) => {
    const p = { ...b, id: pid, updated_at: new Date().toISOString() };
    state.personas = state.personas.map((x) => (x.id === pid ? p : x));
    return p;
});
on('DELETE', '/governance/personas/:id', (_b, _q, [pid]) => { state.personas = state.personas.filter((x) => x.id !== pid); });
on('POST', '/governance/personas/:id/preview', (_b, _q, [pid]) => {
    const p = state.personas.find((x) => x.id === pid);
    const inScope = ASSETS.filter((a) => !p.scope.domains.length || (a.domain && p.scope.domains.includes(a.domain)));
    return { members_count: p.members.users.length + p.members.groups.length * 9, sample_members: p.members.users.map((u: string) => u), assets_in_scope: inScope.length, sample_assets: inScope.slice(0, 6).map((a) => ({ urn: a.urn, name: a.name })) };
});
on('GET', '/governance/purposes', () => state.purposes);
on('POST', '/governance/purposes', (b) => { const p = { ...b, id: id('purpose'), updated_at: new Date().toISOString() }; state.purposes.push(p); return p; });
on('PUT', '/governance/purposes/:id', (b, _q, [pid]) => {
    const p = { ...b, id: pid, updated_at: new Date().toISOString() };
    state.purposes = state.purposes.map((x) => (x.id === pid ? p : x));
    return p;
});
on('DELETE', '/governance/purposes/:id', (_b, _q, [pid]) => { state.purposes = state.purposes.filter((x) => x.id !== pid); });
on('GET', '/governance/policies', () => {
    const compiled = [
        ...state.personas.filter((p) => p.enabled).map((p) => ({
            id: `persona:${p.id}`, description: `Persona '${p.name}': ${p.permissions.join(', ')}`, effect: 'allow', priority: 10,
            actions: p.permissions.flatMap((x: string) => PERMISSION_BUNDLES[x] ?? []), source: 'persona',
            condition: { all: [{ attr: 'subject.personas', op: 'contains', value: p.id }, { attr: 'resource.domains', op: 'contains_any', value: p.scope.domains }] },
        })),
        ...state.purposes.filter((p) => p.enabled).flatMap((p) => [
            { id: `purpose:${p.id}:deny`, description: `Purpose '${p.name}': deny ${p.deny.join(', ')} outside purpose`, effect: 'deny', priority: 1000, actions: p.deny.flatMap((x: string) => PERMISSION_BUNDLES[x] ?? []), source: 'purpose', condition: { labels: p.tags } },
            { id: `purpose:${p.id}:mask`, description: `Purpose '${p.name}': mask ${p.mask.strategy} outside purpose`, effect: 'allow', priority: 1000, actions: ['VIEW_DATASET_PROFILE', 'VIEW_ENTITY_QUERIES'], source: 'purpose', obligations: [{ type: 'mask_columns', params: { labels: p.tags, strategy: p.mask.strategy } }] },
        ]),
    ];
    return { policies: [], compiled };
});

on('GET', '/context/findings', (_b, q) => {
    const status = q.get('status') ?? 'OPEN';
    const ent = q.get('entity_urn');
    return state.findings.filter((f) => (status === 'ALL' || f.status === status) && (!q.get('type') || f.type === q.get('type')) && (!ent || f.entities.includes(ent)));
});
on('POST', '/context/findings/:id/:id', (_b, _q, [fid, action]) => {
    const f = state.findings.find((x) => x.id === fid);
    f.status = action === 'accept' ? 'ACCEPTED' : 'DISMISSED';
    return f;
});
on('GET', '/context/ontology', () => {
    const nodes = [
        { id: T_REVENUE, label: 'Net Revenue', kind: 'term' }, { id: T_CUSTOMER, label: 'Active Customer', kind: 'term' },
        { id: 'metric:aov', label: 'AOV', kind: 'metric' }, { id: 'metric:net_revenue', label: 'net_revenue', kind: 'metric' },
        { id: ORDERS, label: 'orders', kind: 'dataset' }, { id: CUSTOMERS, label: 'customers', kind: 'dataset' }, { id: REV_DAILY, label: 'revenue_daily', kind: 'dataset' },
        { id: `${ORDERS}::customer_id`, label: 'customer_id', kind: 'column' }, { id: `${CUSTOMERS}::id`, label: 'id', kind: 'column' },
        { id: `${CUSTOMERS}::status`, label: 'status', kind: 'column' }, { id: `${REV_DAILY}::net_revenue`, label: 'net_revenue', kind: 'column' },
    ];
    const edges = [
        { source: T_REVENUE, target: 'metric:net_revenue', kind: 'defines', weight: 3 }, { source: 'metric:net_revenue', target: `${REV_DAILY}::net_revenue`, kind: 'computed_in', weight: 2 },
        { source: `${REV_DAILY}::net_revenue`, target: REV_DAILY, kind: 'column_of', weight: 1 }, { source: 'metric:aov', target: ORDERS, kind: 'computed_in', weight: 2 },
        { source: `${ORDERS}::customer_id`, target: `${CUSTOMERS}::id`, kind: 'joins', weight: 3 }, { source: `${ORDERS}::customer_id`, target: ORDERS, kind: 'column_of', weight: 1 },
        { source: `${CUSTOMERS}::id`, target: CUSTOMERS, kind: 'column_of', weight: 1 }, { source: T_CUSTOMER, target: `${CUSTOMERS}::status`, kind: 'tags', weight: 2 },
        { source: `${CUSTOMERS}::status`, target: CUSTOMERS, kind: 'column_of', weight: 1 },
    ];
    return { nodes, edges };
});

on('POST', '/ai/chat', (b) => chat(b.message, b.conversation_id));
on('POST', '/ai/explain/lineage', (b) => explain(b.urn));
on('POST', '/ai/docs/markdown', (b) => markdownDoc(b.urn));
on('GET', '/quality/datasets/:rest/health', (_b, _q, [urn]) => health(urn));

on('GET', '/notify/settings', () => state.notify);
on('PUT', '/notify/settings', (b) => { state.notify = b; return b; });
on('GET', '/notify/inbox', (_b, q) => (q.get('unread') === 'true' ? state.inbox.filter((n) => !n.read_at) : state.inbox));
on('GET', '/notify/inbox/count', () => ({ unread: state.inbox.filter((n) => !n.read_at).length }));
on('POST', '/notify/inbox/read', (b) => { state.inbox.forEach((n) => { if ((b.ids ?? []).includes(n.id)) n.read_at = new Date().toISOString(); }); });
on('POST', '/embed/v1/tokens', (b) => {
    const token = `demo.${btoa(JSON.stringify({ sub: ME, urns: [b.urn], scopes: b.scopes })).replace(/=+$/, '')}.sig`;
    const base = 'https://datahub.northwind.example/dhe/api/embed/v1';
    const tag = b.scopes.includes('lineage') ? 'dhe-lineage' : b.scopes.includes('quality') ? 'dhe-quality-badge' : 'dhe-asset-card';
    return {
        token, expires_at: new Date(Date.now() + (b.ttl_seconds ?? 604800) * 1000).toISOString(),
        snippet_html: `<script src="https://datahub.northwind.example/dhe-ui/embed/sdk.js" defer></script>\n<${tag} urn="${b.urn}" token="${token}" base-url="${base}"></${tag}>`,
        iframe_url: `${base}/frame/card?urn=${encodeURIComponent(b.urn)}&token=${token}`,
    };
});

class HttpError extends Error {
    constructor(readonly status: number, message: string) {
        super(message);
    }
}

const json = (value: unknown, status = 200) =>
    new Response(value === undefined ? '' : JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });

/** `fetch` replacement routed to the in-memory backend (with a small artificial latency). */
export async function demoFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const url = new URL(String(input), 'https://demo.local');
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    await new Promise((r) => setTimeout(r, 120 + Math.random() * 160));
    if (url.pathname.endsWith('/graphql')) {
        const q: string = body.query;
        if (q.includes('dheConsumerSearch')) return json({ data: gqlSearch(body.variables.input) });
        if (q.includes('dheConsumerAsset')) return json({ data: gqlAsset(body.variables.urn) });
        return json({ data: {} });
    }
    const path = url.pathname.replace(/^\/dhe\/api/, '');
    for (const [m, rx, h] of routes) {
        if (m !== method) continue;
        const match = rx.exec(path);
        if (!match) continue;
        try {
            return json(await h(body, url.searchParams, match.slice(1).map(decodeURIComponent)));
        } catch (e) {
            if (e instanceof HttpError) return json({ detail: e.message }, e.status);
            return json({ detail: (e as Error).message }, 500);
        }
    }
    return json({ detail: `Not available in the demo: ${method} ${path}` }, 404);
}
