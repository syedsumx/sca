import React from 'react';

import { AgentFleet } from './agents/AgentFleet';
import { AskDataHubPanel } from './ai/AskDataHubPanel';
import { DocDraftReview } from './ai/DocDraftReview';
import { AccessSimulator } from './governance/AccessSimulator';
import { CertificationInbox } from './governance/CertificationInbox';
import { PolicyEditor } from './governance/PolicyEditor';
import { QualityDashboard } from './quality/QualityDashboard';

export const ENTERPRISE_BASE_PATH = '/enterprise';

export interface EnterpriseRoute {
    /** Absolute path, compatible with react-router v5 `<Route path>`. */
    path: string;
    /** Label for navigation menus. */
    label: string;
    /** Nav group used to cluster entries in the sidebar/header menu. */
    group: 'Quality' | 'Governance' | 'Agents' | 'AI';
    /** DataHub platform privilege that should gate the nav entry (UI hint; the API enforces). */
    privilege?: string;
    component: React.ComponentType;
}

// Zero-prop page wrappers so every route component has the same shape.
const QualityPage = () => <QualityDashboard />;
const InboxPage = () => <CertificationInbox />;
const AgentsPage = () => <AgentFleet />;
const AskPage = () => <AskDataHubPanel />;

function DocsGeneratorPage() {
    return (
        <div style={{ padding: 24, maxWidth: 1200, margin: '0 auto' }}>
            <DocDraftReview />
        </div>
    );
}

export const ENTERPRISE_ROUTES: EnterpriseRoute[] = [
    { path: `${ENTERPRISE_BASE_PATH}/quality`, label: 'Data quality', group: 'Quality', component: QualityPage },
    {
        path: `${ENTERPRISE_BASE_PATH}/governance/inbox`,
        label: 'Review inbox',
        group: 'Governance',
        component: InboxPage,
    },
    {
        path: `${ENTERPRISE_BASE_PATH}/governance/policies`,
        label: 'ABAC policies',
        group: 'Governance',
        privilege: 'MANAGE_POLICIES',
        component: PolicyEditor,
    },
    {
        path: `${ENTERPRISE_BASE_PATH}/governance/simulator`,
        label: 'Access simulator',
        group: 'Governance',
        privilege: 'MANAGE_POLICIES',
        component: AccessSimulator,
    },
    {
        path: `${ENTERPRISE_BASE_PATH}/agents`,
        label: 'Remote agents',
        group: 'Agents',
        privilege: 'MANAGE_INGESTION',
        component: AgentsPage,
    },
    { path: `${ENTERPRISE_BASE_PATH}/ask`, label: 'Ask DataHub', group: 'AI', component: AskPage },
    { path: `${ENTERPRISE_BASE_PATH}/ai/docs`, label: 'AI documentation', group: 'AI', component: DocsGeneratorPage },
];

/** Filters routes for a user, given the set of platform privileges they hold. */
export function visibleRoutes(privileges: Iterable<string>): EnterpriseRoute[] {
    const held = new Set(privileges);
    return ENTERPRISE_ROUTES.filter((r) => !r.privilege || held.has(r.privilege));
}
