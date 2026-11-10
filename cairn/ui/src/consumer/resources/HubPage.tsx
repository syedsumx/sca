import { Alert, Tabs } from 'antd';
import React, { useEffect } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { Page, tokens } from '../components/tokens';
import { visibleTabs } from '../guide/capabilities';
import { useGuide } from '../guide/GuideProvider';
import { hubTab, rememberHubTab } from '../guide/help';
import { Breadcrumbs, PageHeader } from '../guide/PageHeader';
import { RESOURCE_SPECS } from './catalog';
import { AirflowDeploymentsView } from './AirflowDeployments';
import { OntologyGraphView } from './OntologyGraph';
import { PostgresInstancesView } from './PostgresInstances';
import { ResourceView } from './ResourceView';
import type { Hub, ResourceSpec } from './types';

export const HubIcon = styled.span`
    display: inline-grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 12px;
    background: ${tokens.color.primarySoft};
    color: ${tokens.color.primary};
    flex: none;
`;

function AreaView({ spec }: { spec: ResourceSpec }) {
    if (spec.view === 'postgres-instances') return <PostgresInstancesView />;
    if (spec.view === 'airflow-deployments') return <AirflowDeploymentsView />;
    return spec.view === 'ontology-graph' && spec.path ? <OntologyGraphView path={spec.path} /> : <ResourceView spec={spec} />;
}

const known = (k: string) => !!RESOURCE_SPECS[k];
const tabTitle = (k: string) => RESOURCE_SPECS[k]?.title;

/**
 * A hub: one page per platform area, its backend areas as tabs (the tab is kept in the URL, `?tab=`, and remembered
 * on this device). Tabs whose area the person can't use at all are left out; the API decides every request anyway.
 */
export function HubPage({ hub }: { hub: Hub }) {
    const location = useLocation();
    const history = useHistory();
    const g = useGuide();
    const tabs = visibleTabs(hub, g.caps, known);
    const active = hubTab(hub, location.search, tabs);
    useEffect(() => {
        if (active) rememberHubTab(hub.route, active);
    }, [hub.route, active]);
    return (
        <>
            <Breadcrumbs tabTitle={tabTitle} />
            <Page>
                <PageHeader title={hub.title} description={hub.description} icon={hub.icon}>
                    {hub.route === '/semantic' && <p><Link to="/ontology">Open the Ontology explorer: graph, review queue and compile wizard</Link></p>}
                </PageHeader>
                {tabs.length === 0 ? (
                    <Alert type="info" showIcon message={g.caps.loading ? '…' : hub.description} />
                ) : tabs.length === 1 ? (
                    <AreaView spec={RESOURCE_SPECS[tabs[0]]} />
                ) : (
                    <Tabs
                        activeKey={active}
                        onChange={(k) => history.replace({ pathname: location.pathname, search: `?tab=${k}` })}
                        items={tabs.map((k) => ({ key: k, label: RESOURCE_SPECS[k].title,
                            children: k === active ? <AreaView spec={RESOURCE_SPECS[k]} /> : null }))}
                    />
                )}
            </Page>
        </>
    );
}
