/**
 * In-browser stand-in for the Cairn API + Cairn Core GraphQL, used by the standalone demo build.
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
    'urn:li:corpuser:cairn-context-agent': 'Context Agent',
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
            approvers: ['urn:li:corpuser:tom'], decided_by: 'urn:li:corpuser:cairn-marketplace', decided_at: iso(20 * DAY), expires_at: iso(-70 * DAY), created_at: iso(20 * DAY),
            history: [{ at: iso(20 * DAY), actor: ME, from_state: null, to_state: 'PENDING', comment: null }, { at: iso(20 * DAY), actor: 'urn:li:corpuser:cairn-marketplace', from_state: 'PENDING', to_state: 'APPROVED', comment: 'auto-approved by product policy' }],
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
    comment: ['CAIRN_COMMENT'],
    request_access: ['CAIRN_REQUEST_ACCESS'],
    edit_docs: ['EDIT_ENTITY_DOCS', 'EDIT_ENTITY_DOC_LINKS'],
    edit_tags: ['EDIT_ENTITY_TAGS', 'EDIT_DATASET_COL_TAGS'],
    edit_terms: ['EDIT_ENTITY_GLOSSARY_TERMS', 'EDIT_DATASET_COL_GLOSSARY_TERMS'],
    edit_owners: ['EDIT_ENTITY_OWNERS'],
    manage_quality: ['CAIRN_MANAGE_MONITORS'],
    certify: ['CAIRN_APPROVE_CERTIFICATION'],
    approve_access: ['CAIRN_APPROVE_ACCESS'],
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
            (ups ? `## Lineage\n\n**Built from:** ${ups}\n\n` : '') + '_Generated by Cairn._',
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
    personas: [{ id: 'finance-analysts', name: 'Finance analysts' }], is_admin: true, can_manage_agents: true,
    gateway_url: 'https://cairn-gateway.northwind.example',
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
    // Same contract as cairn-api: access ∈ {mine, requestable}, sort ∈ {relevance, consumers, name}.
    const accessFilter = q.get('access');
    if (accessFilter && !['mine', 'requestable'].includes(accessFilter)) throw new HttpError(422, `invalid access filter ${accessFilter}`);
    if (accessFilter === 'mine') items = items.filter((c) => ['GRANTED', 'PENDING'].includes(c.access.status));
    if (accessFilter === 'requestable') items = items.filter((c) => ['NONE', 'EXPIRED', 'REJECTED'].includes(c.access.status));
    const sort = q.get('sort') ?? 'relevance';
    if (!['relevance', 'consumers', 'name'].includes(sort)) throw new HttpError(422, `invalid sort ${sort}`);
    if (sort === 'consumers') items.sort((a, b) => b.consumers - a.consumers);
    if (sort === 'name') items.sort((a, b) => a.name.localeCompare(b.name));
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
        decided_by: auto ? 'urn:li:corpuser:cairn-marketplace' : null, decided_at: auto ? t : null,
        expires_at: auto ? new Date(Date.now() + b.duration_days * DAY).toISOString() : null, created_at: t,
        history: [{ at: t, actor: ME, from_state: null, to_state: 'PENDING', comment: b.justification || null }]
            .concat(auto ? [{ at: t, actor: 'urn:li:corpuser:cairn-marketplace', from_state: 'PENDING', to_state: 'APPROVED', comment: 'auto-approved by product policy' }] : []),
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
    const base = 'https://cairn.northwind.example/cairn/api/embed/v1';
    const tag = b.scopes.includes('lineage') ? 'cairn-lineage' : b.scopes.includes('quality') ? 'cairn-quality-badge' : 'cairn-asset-card';
    return {
        token, expires_at: new Date(Date.now() + (b.ttl_seconds ?? 604800) * 1000).toISOString(),
        snippet_html: `<script src="https://cairn.northwind.example/cairn-ui/embed/sdk.js" defer></script>\n<${tag} urn="${b.urn}" token="${token}" base-url="${base}"></${tag}>`,
        iframe_url: `${base}/frame/card?urn=${encodeURIComponent(b.urn)}&token=${token}`,
    };
});

class HttpError extends Error {
    constructor(readonly status: number, message: string) {
        super(message);
    }
}

// ------------------------------------------------------------------------------- semantic search
const SYNONYMS: Record<string, string[]> = {
    money: ['revenue', 'amount', 'spend'], earn: ['revenue'], made: ['revenue'], sales: ['revenue', 'order'],
    buyer: ['customer'], client: ['customer'], people: ['customer'], email: ['email'], contact: ['email', 'phone'],
    stock: ['inventory'], warehouse: ['inventory', 'shipment'], delivery: ['shipment'], parcel: ['shipment'],
    ad: ['campaign'], advertising: ['campaign', 'spend'], bill: ['invoice'], daily: ['day'], each: [],
};
const stem = (w: string) => (w.length > 4 && w.endsWith('ies') ? `${w.slice(0, -3)}y` : w.length > 3 && w.endsWith('s') && !w.endsWith('ss') ? w.slice(0, -1) : w);
const words = (t: string) => t.toLowerCase().replace(/([a-z])([A-Z])/g, '$1 $2').split(/[^a-z0-9]+/).filter((w) => w.length > 2).map(stem);
const docOf = (a: Asset) => words(`${a.name} ${a.description} ${(a.columns ?? []).map((c) => c.fieldPath).join(' ')}`);
function semanticHits(query: string, exclude: string[], limit: number, skip?: string) {
    const terms = words(query).filter((w) => !['show', 'what', 'which', 'where', 'table', 'data', 'the', 'per'].includes(w));
    return ASSETS.filter((a) => a.urn !== skip && !exclude.includes(a.urn)).map((a) => {
        const doc = new Set(docOf(a));
        const matched = terms.filter((w) => doc.has(w) || (SYNONYMS[w] ?? []).some((x) => doc.has(x)));
        return { a, matched, score: terms.length ? matched.length / terms.length : 0 };
    }).filter((h) => h.score > 0).sort((x, y) => y.score - x.score).slice(0, limit).map(({ a, matched, score }) => ({
        urn: a.urn, name: a.name, type: a.type.toLowerCase(), platform: a.platform, description: a.description,
        score: Math.round((0.35 + score * 0.5) * 1000) / 1000, matched,
    }));
}
on('POST', '/ai/search/semantic', (b) => ({ results: semanticHits(b.query, b.exclude ?? [], b.limit ?? 5) }));
on('GET', '/ai/similar', (_b, q) => {
    const a = ASSETS.find((x) => x.urn === q.get('urn'));
    return { urn: q.get('urn'), results: a ? semanticHits(docOf(a).join(' '), [], Number(q.get('limit') ?? 5), a.urn) : [] };
});

// ------------------------------------------------------------------------------- remote agents
const agentState = {
    pools: [
        { name: 'finance-vpc', description: 'Billing and ERP databases in the finance VPC', allowed_platforms: ['postgres'], max_concurrency: 2, agents_online: 1 },
        { name: 'plant-floor', description: 'On-premises warehouse systems', allowed_platforms: ['postgres', 'mysql'], max_concurrency: 1, agents_online: 0 },
    ],
    agents: [
        { agent_id: 'agt_7f3a91c2d4e5b6a7', pool: 'finance-vpc', hostname: 'ingest-01.finance.internal', version: '0.1.0', status: 'ONLINE', last_heartbeat: iso(12_000), enrolled_at: iso(9 * DAY), running_tasks: 1 },
        { agent_id: 'agt_19c0de44aa21f003', pool: 'plant-floor', hostname: 'wms-gw.plant.local', version: '0.1.0', status: 'OFFLINE', last_heartbeat: iso(2 * DAY), enrolled_at: iso(30 * DAY), running_tasks: 0 },
    ],
    runs: [
        { run_id: 'run_a1b2c3d4e5f60718', task_id: 'task_running_billing', pool: 'finance-vpc', agent_id: 'agt_7f3a91c2d4e5b6a7', status: 'RUNNING', attempts: 1, started_at: iso(95_000), finished_at: null, mcps_forwarded: 214, mcps_rejected: 0, execution_request_urn: null },
        { run_id: 'run_0f9e8d7c6b5a4932', task_id: 'task_nightly_erp', pool: 'finance-vpc', agent_id: 'agt_7f3a91c2d4e5b6a7', status: 'SUCCESS', attempts: 1, started_at: iso(DAY), finished_at: iso(DAY - 240_000), mcps_forwarded: 1873, mcps_rejected: 3, execution_request_urn: null },
        { run_id: null, task_id: 'task_queued_wms', pool: 'plant-floor', agent_id: null, status: 'QUEUED', attempts: 0, started_at: null, finished_at: null, mcps_forwarded: 0, mcps_rejected: 0, execution_request_urn: null },
    ] as Json[],
    logs: {
        task_running_billing: ['--- attempt 1 on ingest-01.finance.internal (agt_7f3a91c2d4e5b6a7), run run_a1b2c3d4e5f60718 ---',
            'run run_a1b2c3d4e5f60718 started on the agent (postgres source)',
            'INFO ingestion.source.postgres connecting to billing-db:5432 as catalog_reader with password ***',
            'INFO ingestion.source.postgres found 3 schemas, 41 tables'],
        task_nightly_erp: ['--- attempt 1 on ingest-01.finance.internal (agt_7f3a91c2d4e5b6a7), run run_0f9e8d7c6b5a4932 ---',
            'run run_0f9e8d7c6b5a4932 started on the agent (postgres source)',
            'INFO ingestion.source.postgres read 112 tables, 1,873 metadata changes',
            'run run_0f9e8d7c6b5a4932 finished: SUCCESS (1873 metadata changes sent)'],
        task_queued_wms: [],
    } as Record<string, string[]>,
};
let tick = 0;
on('GET', '/agents/pools', () => agentState.pools);
on('POST', '/agents/pools', (b) => { const p = { ...b, agents_online: 0 }; agentState.pools.push(p); return p; });
on('GET', '/agents', () => agentState.agents);
on('GET', '/agents/runs', () => agentState.runs);
on('POST', '/agents/pools/:id/enrollment-tokens', () => ({ token: `cairn_enr_demo${Math.random().toString(36).slice(2, 14)}`, expires_at: iso(-3600_000) }));
on('POST', '/agents/:id/revoke', (_b, _q, [aid]) => { const a = agentState.agents.find((x) => x.agent_id === aid); if (a) a.status = 'REVOKED'; });
on('POST', '/agents/tasks/:id/cancel', (_b, _q, [tid]) => {
    const r = agentState.runs.find((x) => x.task_id === tid);
    if (!r) throw new HttpError(404, 'task not found');
    if (!['QUEUED', 'LEASED', 'RUNNING'].includes(r.status)) throw new HttpError(409, `task already finished (${r.status})`);
    const was = r.status; r.status = 'CANCELLED'; r.finished_at = new Date().toISOString();
    agentState.logs[tid].push(`--- cancelled by ${ME} ---`);
    if (was === 'RUNNING') agentState.logs[tid].push(`run ${r.run_id} finished: CANCELLED (${r.mcps_forwarded} metadata changes sent)`);
    return { task_id: tid, status: 'CANCELLED', was };
});
on('GET', '/agents/tasks/:id/logs', (_b, q, [tid]) => {
    const r = agentState.runs.find((x) => x.task_id === tid);
    const lines = agentState.logs[tid] ?? [];
    if (r?.status === 'RUNNING' && lines.length < 12) {      // a live run keeps writing
        tick += 1;
        lines.push(`INFO ingestion.source.postgres extracted table billing.public.${['invoices', 'payments', 'credit_notes', 'tax_rates', 'ledger'][tick % 5]}`);
        r.mcps_forwarded = Number(r.mcps_forwarded) + 37;
    }
    const after = Math.min(Number(q.get('after') ?? 0), lines.length);
    return { task_id: tid, run_id: r?.run_id ?? null, status: r?.status ?? 'QUEUED', lines: lines.slice(after), next: lines.length,
        done: ['SUCCESS', 'FAILURE', 'CANCELLED'].includes(String(r?.status)) };
});

// ------------------------------------------------------------------------------- reports and costs
const cov = (d: number, o: number, c: number, ce: number, m: number) =>
    ({ description: d, owner: o, classification: c, certified: ce, monitored: m });
on('GET', '/reports', () => ({
    day: new Date().toISOString().slice(0, 10), computed_ms: now - 3_600_000, assets: 12,
    coverage: {
        overall: cov(0.83, 0.92, 0.67, 0.42, 0.58),
        by_domain: [
            { key: FINANCE, assets: 5, ...cov(1, 1, 0.8, 0.8, 0.8) },
            { key: MARKETING, assets: 3, ...cov(0.67, 1, 0.67, 0, 0.33) },
            { key: SUPPLY, assets: 3, ...cov(0.67, 0.67, 0.33, 0.33, 0.67) },
            { key: null, assets: 1, ...cov(1, 1, 1, 0, 0) },
        ],
        by_platform: [
            { key: 'snowflake', assets: 5, ...cov(1, 1, 0.8, 0.6, 0.8) },
            { key: 'postgres', assets: 2, ...cov(0.5, 1, 0.5, 0.5, 0.5) },
            { key: 'bigquery', assets: 1, ...cov(1, 1, 0, 0, 0) },
            { key: 'looker', assets: 2, ...cov(1, 0.5, 1, 0, 0) },
        ],
    },
    adoption: { weekly_active_users: 46, monthly_active_users: 118, asset_views_7d: 1312, requests_30d: 23, approved_30d: 19,
        pending_requests: 2, median_hours_to_decision: 3.5 },
    quality: { open_incidents: 1, monitored_pct: 0.58, resolved_30d: 9, mean_hours_to_resolve: 5.2 },
    governance: { personas: 3, purposes: 2, active_grants: 31, grants_expiring_7d: 4 },
}));
on('POST', '/reports/refresh', () => ({}));
on('GET', '/reports/assets', (_b, q) => ASSETS.filter((a) => a.type === 'DATASET').map((a) => ({
    urn: a.urn, name: a.name, domain: a.domain, platform: a.platform ?? '', description: !!a.description, owner: a.owners.length > 0,
    classification: a.columns?.some((c) => c.terms?.length) ?? false, certified: a.certification === 'CERTIFIED', monitored: !!a.quality,
})).filter((r) => !q.get('missing') || !(r as Json)[String(q.get('missing'))]));
const costRow = (urn: string, name: string, platform: string, storage: number, compute: number, reads: number) =>
    ({ urn, name, platform, storage_usd: storage, compute_usd: compute, cost_usd: storage + compute, reads,
       storage_bytes: Math.round(storage * 4e10), last_day: iso(DAY).slice(0, 10), monthly_storage_usd: storage,
       cost_per_read_usd: reads ? (storage + compute) / reads : undefined });
on('GET', '/reports/costs/idle', () => [costRow(INVENTORY, 'inventory_levels', 'postgres', 182.4, 0, 0),
    costRow(CAMPAIGNS, 'campaign_spend', 'bigquery', 64.1, 3.2, 0)]);
on('GET', '/reports/costs/expensive', () => [costRow(FCT_ORDERS, 'fct_orders', 'dbt', 96.0, 1240.5, 412),
    costRow(ORDERS, 'orders', 'snowflake', 210.3, 688.0, 2903), costRow(REV_DAILY, 'revenue_daily', 'snowflake', 12.2, 140.7, 1866)]);
on('POST', '/reports/costs/propose-deprecation', () => ({ proposed: true }));

// ------------------------------------------------------------------------------- compliance forms
const tally = (assets: number, pending: number, completed: number, verified: number) =>
    ({ assets, PENDING: pending, COMPLETED: completed, VERIFIED: verified, done_pct: Math.round(((completed + verified) / assets) * 1000) / 10 });
const FORM_SPEC = {
    name: 'GDPR record of processing', description: 'Confirm purpose, retention and owner for every dataset holding personal data.',
    kind: 'verification', select: { attr: 'resource.classification', op: 'contains', value: 'PII' },
    assignees: { owners: true, ownership_types: [], users: [] }, due_days: 30,
    questions: [
        { id: 'purpose', type: 'text', title: 'Why do we keep this data?', required: true, allowed: [], multiple: false },
        { id: 'retention', type: 'structured_property', title: 'Retention period', required: true, allowed: ['1y', '3y', '7y'],
          multiple: false, property_urn: 'urn:li:structuredProperty:io.cairn.retention' },
        { id: 'owner', type: 'owners', title: 'Business owner', required: true, allowed: [], multiple: false },
    ],
};
on('GET', '/governance/forms', () => [
    { id: 'frm_gdpr', name: FORM_SPEC.name, state: 'ACTIVE', created_by: ME, created_ms: now - 12 * DAY, spec: FORM_SPEC, progress: tally(6, 2, 1, 3) },
    { id: 'frm_sox', name: 'SOX financial data attestation', state: 'ACTIVE', created_by: 'urn:li:corpuser:raj', created_ms: now - 40 * DAY,
      spec: { ...FORM_SPEC, name: 'SOX financial data attestation', kind: 'completion' }, progress: tally(4, 0, 4, 0) },
]);
on('GET', '/governance/forms/tasks', () => [
    { form_id: 'frm_gdpr', form_name: FORM_SPEC.name, urn: CUSTOMERS, state: 'PENDING', kind: 'verification', assigned_ms: now - 12 * DAY, due_ms: now + 18 * DAY },
    { form_id: 'frm_gdpr', form_name: FORM_SPEC.name, urn: INVOICES, state: 'PENDING', kind: 'verification', assigned_ms: now - 12 * DAY, due_ms: now + 18 * DAY },
]);
on('GET', '/governance/forms/:id/progress', (_b, _q, [fid]) => ({ form_id: fid, total: tally(6, 2, 1, 3),
    by_domain: { Finance: tally(3, 1, 0, 2), Marketing: tally(3, 1, 1, 1) },
    by_assignee: { [PEOPLE['urn:li:corpuser:raj']]: tally(3, 1, 0, 2), [PEOPLE['urn:li:corpuser:mei']]: tally(3, 1, 1, 1) } }));

// ------------------------------------------------------------------------------- automations, tag sync, contracts
on('GET', '/governance/automations', () => [
    { id: 'aut_pii_tag', name: 'Tag personal data for GDPR', enabled: true, created_by: ME, identity: 'urn:li:corpuser:cairn-automation',
      spec: { name: 'Tag personal data for GDPR', description: 'Every dataset with a PII column gets the GDPR tag and its owner is told.',
              select: { attr: 'resource.classification', op: 'contains', value: 'PII' }, every_minutes: 60,
              actions: [{ type: 'add_tag', value: 'urn:li:tag:GDPR' }, { type: 'notify', recipients: ['owners'] }] },
      last_run: { id: 'run_1', automation_id: 'aut_pii_tag', started_ms: now - 1_800_000, trigger: 'schedule', matched: 4, changed: 1,
                  batch_id: 'bat_91', state: 'APPLIED', detail: { assets: [INVOICES], notified: ['urn:li:corpuser:mei'] } } },
    { id: 'aut_finance_domain', name: 'Billing tables belong to Finance', enabled: true, created_by: 'urn:li:corpuser:raj',
      identity: 'urn:li:corpuser:cairn-automation',
      spec: { name: 'Billing tables belong to Finance', select: { attr: 'resource.platform_instances', op: 'contains', value: 'billing' },
              every_minutes: 1440, actions: [{ type: 'set_domain', value: FINANCE }] },
      last_run: { id: 'run_2', automation_id: 'aut_finance_domain', started_ms: now - 6 * 3_600_000, trigger: 'schedule', matched: 2,
                  changed: 0, batch_id: null, state: 'NOOP', detail: {} } },
]);
on('GET', '/governance/tag-sync', () => [{
    id: 'snowflake-prod', dialect: 'snowflake', platform: 'snowflake', connection: 'warehouse', dry_run: false,
    mappings: [{ label: PII_EMAIL, tag: 'GOVERNANCE.TAGS.PII', value: 'email' }, { label: PII_PHONE, tag: 'GOVERNANCE.TAGS.PII', value: 'phone' }],
    managed_tags: 7, last_run: { at_ms: now - 5 * 3_600_000, actor: ME, dry_run: false, datasets: 5, tagged_objects: 7, planned: 2, applied: 2, failed: 0, conflicts: 0 },
}]);
on('GET', '/quality/contracts/one', (_b, q) => {
    if (q.get('dataset') !== ORDERS) throw new HttpError(404, 'no contract');
    return {
        dataset: ORDERS, version: '2.1.0', owner: 'urn:li:corpuser:raj', status: 'MET', checked_ms: now - 600_000, created_by: 'urn:li:corpuser:raj',
        updated_ms: now - 20 * DAY, spec: { freshness: { max_age: '2h' }, volume: { min_rows: 1000 } },
        parts: [
            { part: 'schema', status: 'MET', detail: '4 columns as promised' },
            { part: 'freshness', status: 'MET', detail: 'last load 38 minutes ago (promise: 2 hours)' },
            { part: 'volume', status: 'MET', detail: '48,210 rows today (promise: at least 1,000)' },
            { part: 'quality', name: 'order_id is unique', status: 'MET', detail: '0 duplicates' },
        ],
        versions: [{ version: '2.1.0', created_by: 'urn:li:corpuser:raj', created_ms: now - 20 * DAY },
                   { version: '2.0.0', created_by: 'urn:li:corpuser:raj', created_ms: now - 90 * DAY }],
    };
});

// ------------------------------------------------------------------------------- AI steward, context layer, data
const stewardState: Json[] = [
    { id: 'stw_1', urn: INVENTORY, name: 'inventory_levels', kind: 'owner', field: '', value: 'urn:li:corpuser:tom', confidence: 0.86, state: 'PROPOSED',
      evidence: ['Tom Becker ran 71% of the queries on this table in the last 90 days', 'Tom owns shipments, which reads from it'] },
    { id: 'stw_2', urn: CUSTOMERS, name: 'customers', kind: 'term', field: 'phone', value: PII_PHONE, confidence: 0.97, state: 'PROPOSED',
      evidence: ['Values match phone number patterns in 99% of a 1,000-row sample', 'Column name "phone"'] },
    { id: 'stw_3', urn: CAMPAIGNS, name: 'campaign_spend', kind: 'description', field: '', confidence: 0.74, state: 'PROPOSED',
      value: { description: 'Daily ad spend per campaign and channel, loaded from the ads platforms each morning.', columns: [] },
      evidence: ['Read 30 queries that use this table', 'Upstream: ads platform exports'] },
];
on('GET', '/ai/steward/proposals', (_b, q) => stewardState.filter((p) => p.state === 'PROPOSED' && (!q.get('kind') || p.kind === q.get('kind'))));
on('POST', '/ai/steward/decide', (b) => {
    for (const p of stewardState) if (b.ids.includes(p.id)) p.state = b.action === 'accept' ? 'ACCEPTED' : 'REJECTED';
    return { decided: b.ids.length, batch_id: b.action === 'accept' ? 'bat_demo' : null };
});
on('POST', '/ai/steward/run', () => ({ run_id: 'stw_run', datasets_with_gaps: 3, examined: 3, proposed: 3, auto_accepted: 0,
    by_kind: { owner: 1, term: 1, description: 1 } }));
on('GET', '/context/definitions', () => [
    { id: 'def_net_revenue', kind: 'metric', name: 'net_revenue', version: 3, approved_by: 'urn:li:corpuser:raj', updated_ms: now - 4 * DAY,
      doc: { expression: 'sum(amount) - sum(refunds)', description: 'Revenue after refunds, before tax.', datasets: [ORDERS, REV_DAILY] } },
    { id: 'def_orders_customers', kind: 'join', name: 'orders → customers', version: 1, approved_by: ME, updated_ms: now - 9 * DAY,
      doc: { source: ORDERS, destination: CUSTOMERS, source_field: 'customer_id', destination_field: 'id', datasets: [ORDERS, CUSTOMERS] } },
]);
on('GET', '/context/answers', () => [{ urn: 'urn:li:query:rev_by_region', title: 'Net revenue by region, last quarter', score: 0.82,
    sql: 'select c.region, sum(o.amount) - sum(o.refunds) as net_revenue\nfrom analytics.core.orders o join analytics.core.customers c on o.customer_id = c.id\nwhere o.order_ts >= dateadd(quarter, -1, current_date)\ngroup by 1 order by 2 desc',
    tables: [ORDERS, CUSTOMERS] }]);
const SAMPLE = {
    columns: ['order_id', 'customer_id', 'order_ts', 'amount', 'email'],
    rows: [['o-100231', 'c-8812', '2026-10-27 09:14:02', 129.5, 'a****@example.com'], ['o-100232', 'c-1023', '2026-10-27 09:15:40', 54.0, 'j****@example.com'],
           ['o-100233', 'c-8812', '2026-10-27 09:21:11', 310.25, 'a****@example.com'], ['o-100234', 'c-5550', '2026-10-27 09:30:58', 18.99, 'm****@example.com']],
    masked_columns: ['email'], truncated: false, elapsed_ms: 412, tables: [ORDERS],
};
on('GET', '/data/preview', () => SAMPLE);
on('POST', '/data/query', () => ({ ...SAMPLE, elapsed_ms: 655 }));
on('GET', '/data/sources', () => [{ connection: 'warehouse', platform: 'snowflake', database: 'analytics', dialect: 'snowflake' }]);

// ------------------------------------------------------------------------------- AI systems
const ASSESSMENT = {
    name: 'EU AI Act risk assessment', restricted_labels: ['PII', 'Confidential', 'Restricted'],
    questions: [
        { id: 'prohibited', title: 'Does the system do any of the following?', type: 'multi', tier_if_yes: null, required_for: [], options: [
            { value: 'social_scoring', tier: 'unacceptable', label: 'Social scoring that leads to detrimental or unfavourable treatment' },
            { value: 'face_scraping', tier: 'unacceptable', label: 'Untargeted scraping of facial images to build facial recognition databases' },
            { value: 'emotion_work_school', tier: 'unacceptable', label: 'Inferring emotions at work or in education' }] },
        { id: 'high_risk_use', title: 'Is the system used for any of the following?', type: 'multi', tier_if_yes: null, required_for: [], options: [
            { value: 'employment', tier: 'high', label: 'Employment - recruitment, promotion, termination, task allocation or monitoring of workers' },
            { value: 'essential_services', tier: 'high', label: 'Access to essential services - credit scoring, insurance pricing, public benefits' },
            { value: 'education', tier: 'high', label: 'Education and training - admission, assessment, proctoring' }] },
        { id: 'safety_component', title: 'Is it a safety component of a product covered by EU product-safety law?', type: 'boolean',
          options: [], tier_if_yes: 'high', required_for: [] },
        { id: 'transparency', title: 'Does the system do any of the following?', type: 'multi', tier_if_yes: null, required_for: [], options: [
            { value: 'interacts_with_people', tier: 'limited', label: 'Interacts directly with people (for example a chatbot or assistant)' },
            { value: 'generates_content', tier: 'limited', label: 'Generates or alters images, audio, video or text' }] },
        { id: 'human_oversight', title: 'How do people oversee the system, and how can they stop or override it?', type: 'text',
          options: [], tier_if_yes: null, required_for: ['high'] },
    ],
};
const TIER_ORDER = ['unacceptable', 'high', 'limited', 'minimal'];
const tierOf = (answers: Json) => {
    const found = ['minimal'];
    for (const q of ASSESSMENT.questions) {
        const a = answers?.[q.id];
        if (q.type === 'multi') for (const o of q.options) if ((a ?? []).includes(o.value)) found.push(o.tier);
        if (q.type === 'boolean' && a === true && q.tier_if_yes) found.push(q.tier_if_yes);
    }
    return found.sort((x, y) => TIER_ORDER.indexOf(x) - TIER_ORDER.indexOf(y))[0];
};
const aiSystems: Json[] = [
    { id: 'ais_churn', name: 'Churn prediction', purpose: 'Scores customers by likelihood to cancel, for the retention team.', owner: 'urn:li:corpuser:mei',
      models: ['urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn-xgb,PROD)'], datasets: [CUSTOMERS, ORDERS], vendor: null, data_purpose_id: 'customer-support',
      answers: { prohibited: [], high_risk_use: [], safety_component: false, transparency: [] }, state: 'APPROVED',
      decided_by: ME, decided_ms: now - 30 * DAY, decision_note: 'Minimal risk; reviewed with the DPO.' },
    { id: 'ais_cv', name: 'CV screening assistant', purpose: 'Ranks job applicants for recruiters by fit with the role.', owner: 'urn:li:corpuser:tom',
      models: [], datasets: [CUSTOMERS], vendor: 'TalentAI Ltd', data_purpose_id: null,
      answers: { prohibited: [], high_risk_use: ['employment'], safety_component: false, transparency: ['interacts_with_people'],
                 human_oversight: 'A recruiter reviews every ranking before anyone is contacted.' }, state: 'IN_REVIEW',
      decided_by: null, decided_ms: null, decision_note: null },
    { id: 'ais_support_bot', name: 'Support chatbot', purpose: 'Answers order-status questions on the website.', owner: 'urn:li:corpuser:raj',
      models: [], datasets: [ORDERS], vendor: null, data_purpose_id: null,
      answers: { prohibited: [], high_risk_use: [], safety_component: false, transparency: ['interacts_with_people', 'generates_content'] },
      state: 'DRAFT', decided_by: null, decided_ms: null, decision_note: null },
];
const aiPublic = (s: Json) => {
    const tier = tierOf(s.answers);
    const violations = ['high', 'unacceptable'].includes(tier) && !s.data_purpose_id
        ? s.datasets.filter((u: string) => u === CUSTOMERS).map((u: string) => ({ dataset: u, labels: ['PII.Email', 'PII.Phone'], reason: 'no approved purpose' }))
        : [];
    const missing = ASSESSMENT.questions.filter((q) => (q.type === 'text' ? q.required_for.includes(tier) && !String(s.answers?.[q.id] ?? '').trim()
        : s.answers?.[q.id] === undefined)).map((q) => q.id);
    return { ...s, tier, violations, missing, updated_ms: now - DAY };
};
on('GET', '/governance/ai-systems/assessment', () => ASSESSMENT);
on('GET', '/governance/ai-systems/report', () => {
    const list = aiSystems.filter((s) => s.state !== 'RETIRED').map(aiPublic);
    const by_tier: Json = { unacceptable: 0, high: 0, limited: 0, minimal: 0 };
    const by_state: Json = {};
    for (const s of list) { by_tier[s.tier] += 1; by_state[s.state] = (by_state[s.state] ?? 0) + 1; }
    return { assessment: ASSESSMENT.name, total: list.length, by_tier, by_state,
        high_risk_not_approved: list.filter((s) => ['high', 'unacceptable'].includes(s.tier) && s.state !== 'APPROVED').map((s) => s.id),
        violations: list.flatMap((s) => s.violations.map((v: Json) => ({ system: s.id, name: s.name, state: s.state, ...v }))), systems: [] };
});
on('GET', '/governance/ai-systems', () => aiSystems.map(aiPublic));
on('POST', '/governance/ai-systems', (b) => {
    const s = { ...b, id: `ais_${Math.random().toString(36).slice(2, 8)}`, owner: ME, state: 'DRAFT', decided_by: null, decided_ms: null, decision_note: null };
    aiSystems.push(s);
    return aiPublic(s);
});
on('PUT', '/governance/ai-systems/:id', (b, _q, [sid]) => {
    const s = aiSystems.find((x) => x.id === sid);
    Object.assign(s, b, { state: s.state === 'APPROVED' ? 'DRAFT' : s.state });
    return aiPublic(s);
});
on('POST', '/governance/ai-systems/:id/submit', (_b, _q, [sid]) => {
    const s = aiSystems.find((x) => x.id === sid);
    const cur = aiPublic(s);
    if (cur.missing.length) throw new HttpError(422, `answer the assessment first: ${cur.missing.join(', ')}`);
    s.state = 'IN_REVIEW';
    return aiPublic(s);
});
on('POST', '/governance/ai-systems/:id/decision', (b, _q, [sid]) => {
    const s = aiSystems.find((x) => x.id === sid);
    const cur = aiPublic(s);
    if (b.approve && cur.violations.length)
        throw new HttpError(422, 'high-risk systems need an approved purpose for restricted data: ' + cur.violations.map((v: Json) => `${v.dataset}: ${v.reason}`).join('; '));
    Object.assign(s, { state: b.approve ? 'APPROVED' : 'REJECTED', decided_by: ME, decided_ms: Date.now(), decision_note: b.note });
    return aiPublic(s);
});

// ------------------------------------------------------------------------------- extensions and licence
const extensionState: Json[] = [{
    name: 'incident-log', version: '1.0.0', enabled: true, actor: 'urn:li:corpuser:cairn-ext-incident-log', installed_by: ME, installed_ms: now - 6 * DAY,
    manifest: { publisher: 'Northwind data platform', description: 'Recent quality incidents and broken contracts, with each asset’s trust badges.',
                permissions: ['VIEW_ENTITY_PAGE'], scope: null, events: [{ url: 'https://incident-log.northwind.internal/events' }],
                ui: [{ label: 'Incident log', url: 'about:blank', placement: 'workspace' }] },
}];
on('GET', '/admin/extensions', () => extensionState);
on('PUT', '/admin/extensions', (b) => {
    const name = /name:\s*([a-z][a-z0-9-]+)/.exec(String(b))?.[1];
    if (!name) throw new HttpError(422, 'invalid manifest: name is required');
    extensionState.push({ name, version: /version:\s*(\S+)/.exec(String(b))?.[1] ?? '1.0.0', enabled: true, actor: `urn:li:corpuser:cairn-ext-${name}`,
        installed_by: ME, installed_ms: Date.now(), manifest: { publisher: 'You', description: '', permissions: ['VIEW_ENTITY_PAGE'], scope: null, events: [], ui: [] } });
    return { name, version: '1.0.0', actor: `urn:li:corpuser:cairn-ext-${name}`, token: `cx_${name}.demo${Math.random().toString(36).slice(2, 14)}`,
        signing_secret: Math.random().toString(16).slice(2).repeat(4).slice(0, 64) };
});
on('POST', '/admin/extensions/:id/:id', (_b, _q, [name, action]) => {
    const x = extensionState.find((e) => e.name === name);
    if (x) x.enabled = action === 'enable';
    return x;
});
on('DELETE', '/admin/extensions/:id', (_b, _q, [name]) => { extensionState.splice(extensionState.findIndex((e) => e.name === name), 1); });
on('GET', '/extensions/ui', () => extensionState.filter((e) => e.enabled).flatMap((e) =>
    e.manifest.ui.map((u: Json) => ({ extension: e.name, publisher: e.manifest.publisher, ...u, url: demoExtensionPage() }))));
// The example extension's page, rendered inline so the demo needs no extension server.
const demoExtensionPage = () => 'data:text/html;charset=utf-8,' + encodeURIComponent(
    '<!doctype html><meta charset=utf-8><style>body{font:14px system-ui;margin:16px;color:#1f2937}td,th{padding:6px 10px;border-bottom:1px solid #eee;text-align:left}</style>' +
    '<h2>Incident log</h2><table><tr><th>When</th><th>Event</th><th>Asset</th><th>Quality</th></tr>' +
    '<tr><td>06:12</td><td>quality.incident.raised</td><td>revenue_daily</td><td>FAILING</td></tr>' +
    '<tr><td>Yesterday</td><td>contract.restored</td><td>orders</td><td>HEALTHY</td></tr>' +
    '<tr><td>Mon</td><td>quality.incident.resolved</td><td>shipments</td><td>HEALTHY</td></tr></table>');
const licence = {
    status: 'valid', read_only: false, days_left: 214, over_seats: false, required: false, tenant: 'northwind', seats_used: 118, seats_limit: 250, problem: null,
    licence: { id: 'lic_7c2e19', customer: 'Northwind Traders', tenant: 'northwind', edition: 'enterprise',
               features: ['ai', 'automations', 'costs', 'data_access', 'tag_sync', 'ticketing'], max_users: 250,
               issued_at: '2026-06-01', expires_at: '2027-05-31', grace_days: 14, development: false },
};
on('GET', '/admin/licence', () => licence);
on('PUT', '/admin/licence', () => { throw new HttpError(422, 'the licence signature does not match: it was altered or not issued for Cairn'); });

// ------------------------------------------------------------------------------- sharing data products
on('POST', '/marketplace/products/:rest/publish/:id/plan', (b, _q, [urn, target]) => planFor(urn, target, b, false));
on('POST', '/marketplace/products/:rest/publish/:id', (b, _q, [urn, target]) => planFor(urn, target, b.options, true));
function planFor(urn: string, target: string, o: Json, applied: boolean) {
    const share = String(o.share || 'share');
    const p = PRODUCTS.find((x) => x.urn === urn);
    if (target === 'snowflake') {
        const manifest = `title: ${p?.name ?? 'Data product'}\nsubtitle: ${p?.name ?? ''}\ndescription: ${p?.description ?? ''}\nlisting_terms:\n  type: STANDARD\n` +
            (o.consumers?.length ? `targets:\n  accounts:\n${o.consumers.map((c: string) => `  - ${c}`).join('\n')}\n` : '') +
            `data_dictionary:\n  featured:\n    database_objects:\n  - name: ANALYTICS.CORE.ORDERS\n    domain: TABLE\n  - name: ANALYTICS.FINANCE.REVENUE_DAILY\n    domain: TABLE\n` +
            `usage_examples:\n- title: Net revenue by region, last quarter\n  query: select c.region, sum(o.amount) ...\n`;
        const S = `"${share.toUpperCase()}"`;
        return { product: urn, target, tables: ['analytics.core.orders', 'analytics.finance.revenue_daily'], skipped: [INVOICES], manifest,
            statements: [`CREATE SHARE IF NOT EXISTS ${S}`, `GRANT USAGE ON DATABASE "ANALYTICS" TO SHARE ${S}`,
                `GRANT USAGE ON SCHEMA "ANALYTICS"."CORE" TO SHARE ${S}`, `GRANT USAGE ON SCHEMA "ANALYTICS"."FINANCE" TO SHARE ${S}`,
                `GRANT SELECT ON TABLE "ANALYTICS"."CORE"."ORDERS" TO SHARE ${S}`, `GRANT SELECT ON TABLE "ANALYTICS"."FINANCE"."REVENUE_DAILY" TO SHARE ${S}`,
                `CREATE EXTERNAL LISTING IF NOT EXISTS ${S} SHARE ${S} AS $$\n${manifest}$$ PUBLISH = FALSE REVIEW = FALSE`],
            fingerprint: 'demo', published: applied ? { fingerprint: 'demo', published_by: ME, published_ms: Date.now() } : null, in_sync: applied, applied, failed: [] };
    }
    return { product: urn, target, tables: [], skipped: [ORDERS, REV_DAILY, INVOICES], manifest: null,
        statements: [`CREATE SHARE IF NOT EXISTS \`${share}\``], fingerprint: 'demo', published: null, in_sync: false };
}

const json = (value: unknown, status = 200) =>
    new Response(value === undefined ? '' : JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });

/** `fetch` replacement routed to the in-memory backend (with a small artificial latency). */
export async function demoFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const url = new URL(String(input), 'https://demo.local');
    const method = (init?.method ?? 'GET').toUpperCase();
    let body: any;
    try {
        body = init?.body ? JSON.parse(String(init.body)) : undefined;
    } catch {
        body = String(init?.body);                     // e.g. a YAML extension manifest
    }
    await new Promise((r) => setTimeout(r, 120 + Math.random() * 160));
    if (url.pathname.endsWith('/graphql')) {
        const q: string = body.query;
        if (q.includes('cairnConsumerSearch')) return json({ data: gqlSearch(body.variables.input) });
        if (q.includes('cairnConsumerAsset')) return json({ data: gqlAsset(body.variables.urn) });
        return json({ data: {} });
    }
    const path = url.pathname.replace(/^\/cairn\/api/, '');
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
    console.warn(`DEMO-MISSING ${method} ${path}`);
    return json({ detail: `Not available in the demo: ${method} ${path}` }, 404);
}
