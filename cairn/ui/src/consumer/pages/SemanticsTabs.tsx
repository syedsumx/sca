/**
 * Catalog › Semantics (L149): what the data means, in one place. It brings together the pages that were Metrics
 * (/sql/metrics), Semantic models (/semantic), Ontology (/ontology) and Context (/context), as tabs: Metrics · Ontology ·
 * Review queue · Imports & knowledge. The UIs are the same; the stores behind them have not been merged yet, so each
 * list says which store it reads.
 */
import { Alert } from 'antd';
import React from 'react';
import { Link } from 'react-router-dom';

import { StewardReview } from '../components/Steward';
import { Muted, SectionTitle, Surface } from '../components/tokens';
import { ViewTabs } from '../components/ViewTabs';
import { Embedded } from '../guide/PageHeader';
import { ReviewQueue } from '../ontology/ReviewQueue';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ResourceView } from '../resources/ResourceView';
import { ContextFindings, DefinitionsView, MinedOntology, PastAnswersView } from './ContextFindings';
import { MetricsPage } from './MetricsPage';
import { OntologyPage } from './OntologyPage';

/** Which store a list comes from, until the two metric stores are one (ROADMAP L149, step 3). */
export function StoreNote({ store, api, usedBy }: { store: string; api: string; usedBy: string }) {
    return (
        <Muted style={{ display: 'block', margin: '0 0 12px' }} data-store={api}>
            From the <strong>{store}</strong> (<code>{api}</code>). Used by {usedBy}.
        </Muted>
    );
}

export function SemanticsMetrics() {
    return (
        <>
            <Alert type="info" showIcon style={{ marginBottom: 16 }}
                message="Two metric stores, shown together until they are merged"
                description={'The metrics layer is what workbooks, dashboards, Sheets and metric alerts compute; the semantic layer holds '
                    + 'definitions approved from context mining and imported from dbt, Cube or LookML, which the assistant and MCP tools '
                    + 'answer from. A metric of the same name in both can differ until the stores merge.'} />
            <section aria-label="Metrics layer">
                <SectionTitle>Metrics layer</SectionTitle>
                <StoreNote store="metrics layer" api="/sql/metrics" usedBy="workbooks, dashboards, Sheets and metric alerts" />
                <Embedded><MetricsPage /></Embedded>
                <Muted style={{ display: 'block', marginTop: 8 }}>
                    Alerts on these metrics: <Link to="/alerts?tab=metric-alerts">Alerts &amp; routing › Metric alerts</Link>.
                </Muted>
            </section>
            <section aria-label="Semantic layer" style={{ marginTop: 24 }}>
                <SectionTitle>Semantic layer (approved definitions)</SectionTitle>
                <StoreNote store="semantic layer" api="/context/definitions" usedBy="the assistant, the MCP tools and semantic-view exports" />
                <DefinitionsView />
            </section>
        </>
    );
}

export function SemanticsOntology() {
    const spec = (k: string) => RESOURCE_SPECS[k];
    return (
        <Embedded>
            <OntologyPage param="view" hide={['review']} extra={[
                { key: 'terms', label: 'Terms', children: <ResourceView spec={spec('ontology-terms')} /> },
                { key: 'relationships', label: 'Relationships', children: <ResourceView spec={spec('ontology-relationships')} /> },
                { key: 'runs', label: 'Compile runs', children: <ResourceView spec={spec('ontology-runs')} /> },
                { key: 'usage', label: 'From usage', children: (
                    <>
                        <Muted style={{ display: 'block', marginBottom: 8 }}>
                            The graph context mining builds from what it found (<code>/context/ontology</code>), beside the compiled one.
                        </Muted>
                        <MinedOntology />
                    </>
                ) },
            ]} />
        </Embedded>
    );
}

export function SemanticsReview() {
    return (
        <ViewTabs label="Review queue" views={[
            { key: 'ontology', label: 'Ontology proposals', render: () => <ReviewQueue /> },
            { key: 'findings', label: 'Context findings', render: () => <ContextFindings /> },
            { key: 'drafts', label: 'Semantic model drafts', render: () => <ResourceView spec={RESOURCE_SPECS['semantic-models']} /> },
            { key: 'steward', label: 'AI steward', render: () => <StewardReview /> },
        ]} />
    );
}

export function SemanticsImports() {
    return (
        <>
            <Surface style={{ padding: 16, marginBottom: 16 }}>
                <SectionTitle style={{ marginTop: 0 }}>Imports</SectionTitle>
                <p>
                    dbt MetricFlow, Cube and LookML definitions are imported into the semantic layer when a dbt project runs
                    through <code>cairn dbt</code> and by the context import commands; once approved they are in Metrics.
                    See <a href="/docs/context-and-ai.html#governed-metrics-semantic-layer" target="_blank" rel="noreferrer">Governed metrics</a>.
                </p>
                <SectionTitle>Knowledge sources</SectionTitle>
                <p>
                    Confluence, Notion, Google Docs and SharePoint pages the assistant grounds its answers in are configured in the
                    deployment (knowledge sources); documents loaded into the lakehouse are in <Link to="/ingestion?tab=document-sources">Ingestion</Link>.
                </p>
            </Surface>
            <section aria-label="Answered before">
                <SectionTitle>Answered before</SectionTitle>
                <PastAnswersView />
            </section>
        </>
    );
}
