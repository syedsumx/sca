import { Tabs } from 'antd';
import React from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { Icon } from '../components/Icons';
import { Page, tokens } from '../components/tokens';
import { RESOURCE_SPECS } from './catalog';
import { OntologyGraphView } from './OntologyGraph';
import { ResourceView } from './ResourceView';
import type { Hub, ResourceSpec } from './types';

const Head = styled.header`
    display: flex;
    gap: 16px;
    align-items: flex-start;
    margin-bottom: 20px;
    h1 {
        margin: 0;
        font-size: 26px;
        font-weight: 700;
    }
    p {
        margin: 4px 0 0;
        color: ${tokens.color.textMuted};
        max-width: 820px;
    }
`;

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
    return spec.view === 'ontology-graph' && spec.path ? <OntologyGraphView path={spec.path} /> : <ResourceView spec={spec} />;
}

/** A hub: one page per platform area, its backend areas as tabs (the tab is kept in the URL: `?tab=`). */
export function HubPage({ hub }: { hub: Hub }) {
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const tabs = hub.tabs.filter((k) => RESOURCE_SPECS[k]);
    const active = tabs.includes(params.get('tab') ?? '') ? (params.get('tab') as string) : tabs[0];
    return (
        <Page>
            <Head>
                <HubIcon><Icon name={hub.icon} size={22} /></HubIcon>
                <div>
                    <h1>{hub.title}</h1>
                    <p>{hub.description}</p>
                    {hub.route === '/semantic' && <p><Link to="/ontology">Open the Ontology explorer: graph, review queue and compile wizard</Link></p>}
                </div>
            </Head>
            {tabs.length === 1 ? (
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
    );
}
