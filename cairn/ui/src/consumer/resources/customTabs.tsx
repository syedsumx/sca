/**
 * Hub tabs that are not a plain table of one API (L149, one home per concept): purpose-built views that used to be
 * pages or tabs of their own (Settings › Sources, Pipelines › dbt runs, the Governance studio's automations …) and
 * the cross-store lists of Quality & operations. A hub names them in `tabs` like any area.
 *
 * - CUSTOM_TABS: tabs with no area of their own.
 * - SPEC_VIEWS: an area whose richer, purpose-built view replaces the generic table (contracts, warehouses, shares…).
 * - DETAIL_EXTRAS: what a row's details add under the generic table (a pipe's files, a stream's rejects, a table's runs).
 */
import React from 'react';

import { useDheSession } from '../context/CairnProvider';
import { Embedded } from '../guide/PageHeader';
import { RESOURCE_SPECS } from './catalog';
import type { ResourceSpec } from './types';

export interface CustomTab {
    key: string;
    title: string;
    description: string;
    /** The docs section the help opens on this tab. */
    docs?: string;
    view: React.ComponentType;
}

const lazy = <P extends object>(load: () => Promise<React.ComponentType<P>>) =>
    React.lazy(() => load().then((C) => ({ default: C })));

const Operations = () => import('../components/Operations');
const Semantics = () => import('../pages/SemanticsTabs');

function embed<P extends object>(C: React.ComponentType<P>): React.ComponentType<P> {
    return function EmbeddedView(props: P) {
        return <Embedded><C {...props} /></Embedded>;
    };
}

function AgentsView() {
    const session = useDheSession();
    const Inner = AI_AGENTS;
    return <Inner isAdmin={!!session.config?.is_admin} />;
}
const AI_AGENTS = lazy(() => import('../components/AiAgents').then((m) => m.AiAgentsSettings));

const tab = (key: string, title: string, description: string, view: React.ComponentType, docs?: string): CustomTab =>
    ({ key, title, description, view, docs });

export const CUSTOM_TABS: Record<string, CustomTab> = Object.fromEntries([
    // Ingestion
    tab('sources', 'Metadata sources', 'Connectors that bring metadata (schemas, owners, lineage, usage) from your systems into Cairn Core, on a schedule or now.',
        lazy(() => import('../components/Sources').then((m) => m.SourcesSettings)), 'administration.html#sources'),
    tab('collectors', 'Collectors', 'Collectors (formerly remote agents) run metadata ingestion inside networks Cairn cannot reach: pools, enrolment, the collectors and their runs.',
        lazy(() => import('../components/Collectors').then((m) => m.CollectorsView)), 'remote-agents.html#collectors-remote-agents'),
    // Usage & cost
    tab('compute-cost', 'Tables, Spark & Trino cost', 'What tables, Spark jobs and Trino queries cost: idle tables, tables expensive per read, Spark jobs and Trino by user.',
        lazy(() => import('../pages/ReportsPage').then((m) => m.CostReports)), 'administration.html#reports'),
    // Security & compliance, Automation
    tab('ai-systems', 'AI systems', 'The register of AI systems, their risk tier and assessments (EU AI Act).',
        lazy(() => import('../components/AISystems').then((m) => m.AISystemsAdmin))),
    tab('automations', 'Automations', 'When something happens to an asset (a tag, a classification, a new dataset), do something: tag, assign, notify, start a review.',
        lazy(() => import('../components/Automations').then((m) => m.AutomationsAdmin))),
    tab('playbooks', 'Playbooks', 'Agent playbooks: steps an agent follows when a trigger fires, with approvals where they change something.',
        lazy(() => import('../components/Playbooks').then((m) => m.PlaybooksAdmin))),
    tab('tag-sync', 'Warehouse tags', 'Keep tags and classifications in step between Cairn Core and the warehouses (Snowflake, Databricks, BigQuery).',
        lazy(() => import('../components/TagSync').then((m) => m.TagSyncAdmin))),
    tab('forms', 'Compliance forms', 'Forms owners must complete about their assets (a DPIA, a retention review), assigned by rule and tracked to done.',
        lazy(() => import('../components/Forms').then((m) => m.FormsAdmin))),
    // Quality & operations
    tab('all-runs', 'All runs', 'Every run of flows, SQL workflows, jobs, dbt, derived tables, pipes and Airflow DAGs, newest first.',
        lazy(() => Operations().then((m) => m.AllRuns)), 'administration.html#runs-incidents-and-alerts'),
    tab('pipelines', 'Pipelines', 'Each pipeline across orchestrators: health, run history with task timelines, what it reaches, SLAs and pipeline data products.',
        lazy(() => Operations().then((m) => m.PipelineRuns)), 'administration.html#observability'),
    tab('dbt', 'dbt runs', 'dbt runs recorded by cairn dbt: what ran where, how it went, node by node, and the project docs.',
        lazy(() => import('../components/DbtRuns').then((m) => m.DbtRuns)), 'integrations.html#git-and-dbt'),
    tab('all-incidents', 'Incidents', 'Data quality, platform and status page incidents in one list, each labelled with its source.',
        lazy(() => Operations().then((m) => m.AllIncidents)), 'administration.html#runs-incidents-and-alerts'),
    tab('all-alerts', 'All alerts', 'Every alert, whatever it watches: saved queries, governed metrics, platform telemetry and budgets.',
        lazy(() => Operations().then((m) => m.AllAlerts)), 'administration.html#runs-incidents-and-alerts'),
    tab('sql-alerts', 'Query alerts', 'Alerts on a saved query\'s result, checked on a schedule as their owner.',
        lazy(() => Operations().then((m) => m.SqlAlertsView)), 'administration.html#sql-workspace'),
    tab('platform-rules', 'Platform rules', 'Alerts on platform telemetry (metrics and logs), their rules and silences.',
        lazy(() => Operations().then((m) => m.PlatformAlerts)), 'administration.html#observability'),
    tab('status-announcements', 'Status announcements', 'What the public status page tells people: incidents and maintenance.',
        lazy(() => Operations().then((m) => m.StatusAnnouncements)), 'administration.html#status-page'),
    // Semantics
    tab('metrics', 'Metrics', 'Governed metrics: the metrics layer and the semantic layer\'s approved definitions, each labelled with its store.',
        lazy(() => Semantics().then((m) => m.SemanticsMetrics)), 'context-and-ai.html#governed-metrics-semantic-layer'),
    tab('ontology', 'Ontology', 'The compiled ontology: graph, terms, relationships, compile runs and the graph mined from usage.',
        lazy(() => Semantics().then((m) => m.SemanticsOntology)), 'administration.html#cairn-ontology-the-ontology-compiler'),
    tab('review', 'Review queue', 'Everything proposed about meaning, to accept or dismiss: ontology proposals, context findings, semantic model drafts and the AI steward.',
        lazy(() => Semantics().then((m) => m.SemanticsReview)), 'context-and-ai.html#context-agents'),
    tab('imports', 'Imports & knowledge', 'Where definitions come from: dbt, Cube and LookML imports, knowledge sources, and questions answered before.',
        lazy(() => Semantics().then((m) => m.SemanticsImports)), 'context-and-ai.html#governed-metrics-semantic-layer'),
    // SQL
    tab('editor', 'Editor', 'Write and run SQL as yourself, chart the result and save it.',
        lazy(() => import('../pages/SqlWorkspacePage').then((m) => embed(m.SqlWorkspacePage))), 'administration.html#sql-workspace'),
    tab('insight', 'History & query insight', 'Queries run on Trino, with their profiles and tuning hints.',
        lazy(() => import('../pages/QueryInsightPage').then((m) => embed(m.QueryInsightPage))), 'administration.html#query-history-profiles-and-tuning-hints'),
    tab('git', 'Git folders', 'SQL, notebooks and dbt projects kept in git, pulled and pushed from Cairn.',
        lazy(() => import('../pages/GitFoldersPage').then((m) => embed(m.GitFoldersPage))), 'administration.html#git-folders'),
].map((x) => [x.key, x]));

/** Areas whose purpose-built view replaces the generic table. */
export const SPEC_VIEWS: Record<string, React.ComponentType> = {
    contracts: lazy(() => import('../pages/ProductsPages').then((m) => embed(m.ContractsPage))),
    warehouses: lazy(() => import('../pages/LakehouseOps').then((m) => m.WarehousesTab)),
    shares: lazy(() => import('../pages/LakehouseOps').then((m) => m.SharesTab)),
    storage: lazy(() => import('../components/Storage').then((m) => m.StorageSettings)),
    streams: lazy(() => import('../pages/LakehouseOps').then((m) => m.StreamsView)),
    agents: AgentsView,
    'clean-rooms': lazy(() => import('../components/CleanRooms').then((m) => m.CleanRoomsView)),
    // Snowflake and Databricks presets above the registered catalogs (L151).
    'external-catalogs': lazy(() => import('../components/WarehouseConnect').then((m) => m.ExternalCatalogsView)),
};

type Row = Record<string, unknown>;
const PipeDetail = lazy(() => import('../pages/LakehouseOps').then((m) => function PipeDetail({ id }: { id: string }) {
    return <><m.PipeFiles id={id} /><m.PipeRejects id={id} /></>;
}));
const DerivedDetail = lazy(() => import('../pages/LakehouseOps').then((m) => m.DerivedRuns));

/** What a row's details add under the generic table (the views the old Pipelines page had). */
export const DETAIL_EXTRAS: Record<string, (row: Row) => React.ReactNode> = {
    pipes: (r) => <PipeDetail id={String(r.id)} />,
    derived: (r) => <DerivedDetail id={String(r.id)} />,
};

/** A custom tab as an area, for the help panel, breadcrumbs and the palette (title, description, docs). */
export function asSpec(t: CustomTab): ResourceSpec {
    return { key: t.key, title: t.title, path: null, listKey: null, description: t.description, docs: t.docs, columns: [] };
}

/** Every tab a hub may name: the areas and the custom views, as areas (title, description, docs). */
export const TAB_SPECS: Record<string, ResourceSpec> = {
    ...RESOURCE_SPECS,
    ...Object.fromEntries(Object.values(CUSTOM_TABS).map((t) => [t.key, asSpec(t)])),
};

export const knownTab = (k: string) => !!TAB_SPECS[k];
