import type { Hub } from './types';

/**
 * The platform hub pages (kept apart from the area specs so the route table stays small). Since L149 each concept has
 * one home: no tab appears in two hubs (a test checks it), and a tab is either an area of `catalog.ts` or a
 * purpose-built view of `customTabs.tsx`.
 */
export const HUBS: Hub[] = [
    {
        "route": "/semantics",
        "title": "Semantics",
        "icon": "layers",
        "description": "What the data means: governed metrics, the ontology Cairn Ontology compiles, the review queue of everything proposed about meaning, and where definitions are imported from.",
        "tabs": ["metrics", "ontology", "review", "imports"]
    },
    {
        "route": "/automation",
        "title": "Automation",
        "icon": "repeat",
        "description": "Rules that act when something happens in the catalog: automations, agent playbooks, warehouse tag sync and compliance forms.",
        "tabs": ["automations", "playbooks", "tag-sync", "forms"]
    },
    {
        "route": "/security",
        "title": "Security & compliance",
        "icon": "lock",
        "description": "Security posture, passkeys, secret scopes, SIEM export, API limits, the AI systems register and the audit log.",
        "tabs": ["posture", "passkeys", "secret-scopes", "siem", "limits", "ai-systems", "audit"]
    },
    {
        "route": "/sharing",
        "title": "External sharing",
        "icon": "share",
        "description": "Data shared outside the organisation: Cairn-hosted shares (Iceberg REST and Delta Sharing), data packages, clean rooms and how shares are used. A product's own publications to Snowflake and Databricks are on its Distribution tab.",
        "tabs": ["shares", "publications", "share-packages", "clean-rooms", "share-usage"]
    },
    {
        "route": "/usage",
        "title": "Usage & cost",
        "icon": "coin",
        "description": "What the platform costs and who uses it: tables, Spark and Trino cost, AI usage, budgets and monthly chargeback.",
        "tabs": ["my-ai-usage", "compute-cost", "ai-usage", "budgets", "chargeback", "chargeback-cost-centres"]
    },
    {
        "route": "/quality",
        "title": "Data quality",
        "icon": "shield",
        "description": "Whether the catalog's data can be trusted: quality monitors on datasets, the data contracts producers publish, data products' service levels and drift monitors on served models.",
        "tabs": ["quality-monitors", "contracts", "slos", "model-monitors"]
    },
    {
        "route": "/runs",
        "title": "Runs",
        "icon": "play",
        "description": "Every run of every orchestrator in one list (flows, SQL workflows, jobs, dbt, derived tables, pipes, Airflow), each pipeline's health and SLAs, and dbt runs.",
        "tabs": ["all-runs", "pipelines", "dbt"]
    },
    {
        "route": "/incidents",
        "title": "Incidents",
        "icon": "bell",
        "description": "Data quality, platform and status page incidents in one list, each labelled with its source; platform incidents open with the SRE agent.",
        "tabs": ["all-incidents"]
    },
    {
        "route": "/alerts",
        "title": "Alerts & routing",
        "icon": "bell",
        "description": "Every kind of alert (on saved queries, metrics, platform telemetry and budgets), silences, where notifications go and the public status page's announcements.",
        "tabs": ["all-alerts", "sql-alerts", "metric-alerts", "platform-rules", "notification-routes", "status-announcements"]
    },
    {
        "route": "/sql",
        "title": "SQL",
        "icon": "code",
        "description": "Write and run SQL as yourself, your saved queries, the history of what ran with tuning hints, stored procedures and git folders.",
        "tabs": ["editor", "saved-queries", "insight", "procedures", "git"]
    },
    {
        "route": "/ingestion",
        "title": "Ingestion",
        "icon": "download",
        "description": "Everything that brings metadata and data in: metadata sources and the collectors that run them in other networks, database mirrors (Cairn replicates a database for you), SaaS syncs, Apache NiFi's connectors run by Cairn, files and documents, streams (you already publish change events to Kafka), reverse syncs and samples.",
        "tabs": [
            "sources",
            "collectors",
            "mirrors",
            "saas-syncs",
            "connector-syncs",
            "nifi-connectors",
            "document-sources",
            "parsed-documents",
            "pipes",
            "streams",
            "reverse-syncs",
            "samples"
        ]
    },
    {
        "route": "/lakehouse",
        "title": "Lakehouse",
        "icon": "database",
        "description": "Tables kept by Cairn: derived tables and materialized views, the medallion, Parquet registered in place, external catalogs, locations and storage credentials, volumes, storage buckets, vector indexes, maintenance and synthetic data.",
        "tabs": [
            "derived",
            "medallion",
            "parquet-tables",
            "external-catalogs",
            "external-locations",
            "storage-credentials",
            "volumes",
            "storage",
            "vectors",
            "maintenance",
            "maintenance-runs",
            "synthetic"
        ]
    },
    {
        "route": "/compute",
        "title": "Compute",
        "icon": "server",
        "description": "Where work runs: SQL warehouses that start on demand, Spark, managed Postgres instances and their databases, serving tables for lookups by key, data apps and app packages, and data planes with their network rules.",
        "tabs": [
            "warehouses",
            "spark-history",
            "postgres-instances",
            "databases",
            "serving-tables",
            "apps",
            "app-installs",
            "dataplanes",
            "workspace-compute",
            "egress-rules"
        ]
    },
    {
        "route": "/agents",
        "title": "Agents",
        "icon": "bot",
        "description": "AI agents that work for people in Cairn, each with its own identity and scopes: agent apps, custom agents, identities, sandboxes, scheduled tasks and their outbox, what their traces say, intent denials, abuse findings, memories and the pipeline agent.",
        "tabs": [
            "agent-apps",
            "custom-agents",
            "agent-examples",
            "agents",
            "sandbox-runs",
            "scheduled-tasks",
            "outbox",
            "agent-insights",
            "agent-findings",
            "agent-intent-denials",
            "agent-abuse-findings",
            "agent-memories",
            "pipeline-agent"
        ]
    },
    {
        "route": "/ml",
        "title": "Machine learning",
        "icon": "brain",
        "description": "Models governed through Cairn: the registry, model serving, feature tables and fine-tuning on governed data.",
        "tabs": [
            "ml-models",
            "ml-deployments",
            "ml-feature-tables",
            "ml-finetunes"
        ]
    },
    {
        "route": "/ai/gateway",
        "title": "AI gateway",
        "icon": "sparkle",
        "description": "The models the AI gateway serves and where each runs, the routes that pick one for each request, the other MCP servers Cairn reaches, and Cairn in your editor.",
        "tabs": ["ai-gateway-models", "gateway-routes", "mcp-servers", "connect-ide"]
    }
];

/**
 * Areas shown as a tab of a purpose-built page rather than a hub (L149): the command palette offers them and their
 * create forms like a hub's (`<route>?tab=<tab>&action=<label>`).
 */
export const PAGE_AREAS: { route: string; title: string; areas: { tab: string; spec: string }[] }[] = [
    { route: '/flows', title: 'Flows', areas: [{ tab: 'jobs', spec: 'jobs' }, { tab: 'airflow', spec: 'airflow-deployments' }] },
    { route: '/governance', title: 'Access policies', areas: [{ tab: 'consent', spec: 'consent' }, { tab: 'tokens', spec: 'tokens' },
        { tab: 'residency', spec: 'residency' }] },
    { route: '/people', title: 'People & roles', areas: [{ tab: 'access-reviews', spec: 'access-reviews' }] },
    { route: '/dashboards', title: 'Dashboards', areas: [{ tab: 'exports', spec: 'scheduled-exports' }] },
];
