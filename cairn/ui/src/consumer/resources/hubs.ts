import type { Hub } from './types';

/** The platform hub pages (kept apart from the area specs so the route table stays small). */
export const HUBS: Hub[] = [
    {
        "route": "/semantic",
        "title": "Semantic models",
        "icon": "cube",
        "description": "Governed metric layers drafted from your tables and query history, reviewed before they go live.",
        "tabs": [
            "semantic-models"
        ]
    },
    {
        "route": "/ingestion",
        "title": "Ingestion",
        "icon": "download",
        "description": "Everything that brings data into the lakehouse: database mirrors, document sources, SaaS and reverse syncs, file pipes, parsed documents and streams.",
        "tabs": [
            "mirrors",
            "document-sources",
            "saas-syncs",
            "reverse-syncs",
            "pipes",
            "parsed-documents",
            "streams",
            "samples"
        ]
    },
    {
        "route": "/lakehouse",
        "title": "Lakehouse",
        "icon": "database",
        "description": "Tables kept by Cairn: derived tables and materialized views, Parquet registered in place, vector indexes, volumes, storage buckets, maintenance and stored procedures.",
        "tabs": [
            "derived",
            "external-catalogs",
            "medallion",
            "parquet-tables",
            "vectors",
            "volumes",
            "storage",
            "maintenance",
            "maintenance-runs",
            "procedures"
        ]
    },
    {
        "route": "/compute",
        "title": "Compute & jobs",
        "icon": "server",
        "description": "Where work runs: SQL warehouses that start on demand, Postgres databases and branches, serving tables for lookups by key, scheduled jobs, data apps and Spark applications.",
        "tabs": [
            "warehouses",
            "databases",
            "serving-tables",
            "jobs",
            "apps",
            "spark-history"
        ]
    },
    {
        "route": "/agents",
        "title": "Agents",
        "icon": "bot",
        "description": "Agents that work for people in Cairn, each with its own identity and scopes: code-first agent apps, custom agents, scheduled tasks and their outbox, what their traces say, intent denials, abuse findings, what they remember about you, the pipeline agent and the AI steward.",
        "tabs": [
            "agent-apps",
            "custom-agents",
            "agent-listings",
            "scheduled-tasks",
            "outbox",
            "agent-insights",
            "agent-findings",
            "agent-intent-denials",
            "agent-abuse-findings",
            "agent-memories",
            "pipeline-agent",
            "ai-steward",
            "agents",
            "remote-agents"
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
        "route": "/trust",
        "title": "Trust & security",
        "icon": "lock",
        "description": "Controls that keep data safe and auditable: access reviews, data residency, tokenisation, security posture, consent, data contracts, passkeys, secrets, SIEM export and limits.",
        "tabs": [
            "access-reviews",
            "residency",
            "posture",
            "tokens",
            "consent",
            "contracts",
            "passkeys",
            "secret-scopes",
            "siem",
            "limits",
            "workspaces"
        ]
    },
    {
        "route": "/sharing",
        "title": "Sharing",
        "icon": "share",
        "description": "Data shared outside the platform and the promises made about it: Delta shares and data product service levels.",
        "tabs": [
            "shares",
            "share-usage",
            "slos"
        ]
    },
    {
        "route": "/usage",
        "title": "Cost & usage",
        "icon": "coin",
        "description": "What the platform costs and who uses it: AI usage and budgets, the models the AI gateway serves and its routes, and monthly chargeback.",
        "tabs": [
            "ai-usage",
            "budgets",
            "ai-gateway-models",
            "gateway-routes",
            "chargeback",
            "chargeback-cost-centres"
        ]
    }
];
