import { ConfigProvider, Result, Spin } from 'antd';
import React, { Suspense } from 'react';
import { BrowserRouter, HashRouter, Redirect, Route, Switch } from 'react-router-dom';

import { ConsumerClient } from './api/client';
import { QueryCache } from './api/cache';
import { ConsumerLayout } from './components/Layout';
import { CatalogUrlContext } from './context/NativeLinks';
import { tokens } from './components/tokens';
import { CairnProvider } from './context/CairnProvider';
import { I18nProvider, useT } from './i18n';
import { HUBS } from './resources/hubs';

// Platform hubs (/agents, /ingestion, …) load one chunk between them; each is a page of ResourceView tabs.
const HubPage = React.lazy(() => import('./resources/HubPage').then((m) => ({ default: m.HubPage })));
const HUB_ROUTES = HUBS.map((hub) => ({
    path: hub.route,
    exact: true,
    component: function HubRoute() {
        return <HubPage hub={hub} />;
    },
}));

// Route-level code splitting keeps the initial MFE payload small (spec Part 9: ≤ 250 kB gz).
const DiscoverPage = React.lazy(() => import('./pages/DiscoverPage').then((m) => ({ default: m.DiscoverPage })));
const SearchPage = React.lazy(() => import('./pages/SearchPage').then((m) => ({ default: m.SearchPage })));
const AssetPage = React.lazy(() => import('./pages/AssetPage').then((m) => ({ default: m.AssetPage })));
const MarketplacePage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.MarketplacePage })));
const ProductPage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.ProductPage })));
const RequestsPage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.RequestsPage })));
const AssistantPage = React.lazy(() => import('./pages/AssistantPage').then((m) => ({ default: m.AssistantPage })));
const ContextInboxPage = React.lazy(() => import('./pages/ContextInboxPage').then((m) => ({ default: m.ContextInboxPage })));
const ReportsPage = React.lazy(() => import('./pages/ReportsPage').then((m) => ({ default: m.ReportsPage })));
const PipelinesPage = React.lazy(() => import('./pages/PipelinesPage').then((m) => ({ default: m.PipelinesPage })));
const DashboardsPage = React.lazy(() => import('./pages/DashboardsPage').then((m) => ({ default: m.DashboardsPage })));
const SqlWorkspacePage = React.lazy(() => import('./pages/SqlWorkspacePage').then((m) => ({ default: m.SqlWorkspacePage })));
const QueryInsightPage = React.lazy(() => import('./pages/QueryInsightPage').then((m) => ({ default: m.QueryInsightPage })));
const SqlWorkflowsPage = React.lazy(() => import('./pages/SqlWorkflowsPage').then((m) => ({ default: m.SqlWorkflowsPage })));
const FlowsPage = React.lazy(() => import('./pages/FlowsPage').then((m) => ({ default: m.FlowsPage })));
const OntologyPage = React.lazy(() => import('./pages/OntologyPage').then((m) => ({ default: m.OntologyPage })));
const OntologyTermPage = React.lazy(() => import('./pages/OntologyPage').then((m) => ({ default: m.OntologyTermPage })));
const MetricsPage = React.lazy(() => import('./pages/MetricsPage').then((m) => ({ default: m.MetricsPage })));
const InputTablesPage = React.lazy(() => import('./pages/InputTablesPage').then((m) => ({ default: m.InputTablesPage })));
const WorkbooksPage = React.lazy(() => import('./pages/WorkbooksPage').then((m) => ({ default: m.WorkbooksPage })));
const GitFoldersPage = React.lazy(() => import('./pages/GitFoldersPage').then((m) => ({ default: m.GitFoldersPage })));
const GovernanceStudioPage = React.lazy(() =>
    import('./pages/GovernanceStudioPage').then((m) => ({ default: m.GovernanceStudioPage })),
);
const SettingsPage = React.lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));
const ProfilePage = React.lazy(() => import('./pages/ProfilePage').then((m) => ({ default: m.ProfilePage })));
const CapabilitiesPage = React.lazy(() =>
    import('./resources/CapabilitiesPage').then((m) => ({ default: m.CapabilitiesPage })),
);
const ExtensionsPage = React.lazy(() => import('./components/Extensions').then((m) => ({ default: m.ExtensionsPage })));

export const CONSUMER_ROUTES: { path: string; component: React.ComponentType; exact?: boolean }[] = [
    { path: '/discover', component: DiscoverPage, exact: true },
    { path: '/search', component: SearchPage, exact: true },
    { path: '/asset/:urn', component: AssetPage },
    { path: '/marketplace', component: MarketplacePage, exact: true },
    { path: '/marketplace/:urn', component: ProductPage },
    { path: '/requests', component: RequestsPage, exact: true },
    { path: '/assistant', component: AssistantPage, exact: true },
    { path: '/context', component: ContextInboxPage, exact: true },
    { path: '/governance', component: GovernanceStudioPage, exact: true },
    { path: '/reports', component: ReportsPage, exact: true },
    { path: '/pipelines', component: PipelinesPage, exact: true },
    { path: '/sql', component: SqlWorkspacePage, exact: true },
    { path: '/flows', component: FlowsPage, exact: true },
    { path: '/ontology', component: OntologyPage, exact: true },
    { path: '/ontology/terms/:id', component: OntologyTermPage },
    { path: '/sql/workflows', component: SqlWorkflowsPage, exact: true },
    { path: '/sql/workbooks', component: WorkbooksPage, exact: true },
    { path: '/sql/input-tables', component: InputTablesPage, exact: true },
    { path: '/sql/metrics', component: MetricsPage, exact: true },
    { path: '/sql/git', component: GitFoldersPage, exact: true },
    { path: '/queries', component: QueryInsightPage, exact: true },
    { path: '/dashboards', component: DashboardsPage, exact: true },
    ...HUB_ROUTES,
    { path: '/capabilities', component: CapabilitiesPage, exact: true },
    { path: '/profile', component: ProfilePage, exact: true },
    { path: '/settings', component: SettingsPage, exact: true },
    { path: '/extensions', component: ExtensionsPage, exact: true },
];

function ErrorView({ error }: { error: Error }) {
    const t = useT();
    return (
        <Result
            status="error"
            title={t('error.title')}
            subTitle={error.message}
            extra={<a href={window.location.pathname}>{t('error.reload')}</a>}
        />
    );
}

function NotFound() {
    const t = useT();
    return <Result status="404" title={t('page.notFound')} extra={<a href="./discover">{t('page.goDiscover')}</a>} />;
}

class ErrorBoundary extends React.Component<{ children?: React.ReactNode }, { error: Error | null }> {
    state = { error: null as Error | null };

    static getDerivedStateFromError(error: Error) {
        return { error };
    }

    render() {
        if (this.state.error) {
            return <ErrorView error={this.state.error} />;
        }
        return this.props.children;
    }
}

/** Routes + layout without a Router (lets tests use MemoryRouter). */
export function ConsumerRoutes() {
    return (
        <ConsumerLayout>
            <ErrorBoundary>
                <Suspense
                    fallback={
                        <div style={{ padding: 48, textAlign: 'center' }} aria-busy="true">
                            <Spin />
                        </div>
                    }
                >
                    <Switch>
                        {CONSUMER_ROUTES.map((r) => (
                            <Route key={r.path} path={r.path} exact={r.exact} component={r.component} />
                        ))}
                        <Redirect exact from="/" to="/discover" />
                        <Route render={() => <NotFound />} />
                    </Switch>
                </Suspense>
            </ErrorBoundary>
        </ConsumerLayout>
    );
}

export interface ConsumerAppProps {
    /**
     * `hash` (default) when hosted as a Cairn Core MFE: the host registers `/mfe<path>` as an *exact*
     * route, so in-app navigation must not change the pathname (URLs look like `/mfe/cairn#/search?q=x`).
     * `browser` for standalone hosting under `basename`.
     */
    routerMode?: 'hash' | 'browser';
    /** Path prefix for `browser` mode. */
    basename?: string;
    /** Cairn API base URL (default `/cairn/api`). */
    apiBaseUrl?: string;
    graphqlUrl?: string;
    client?: ConsumerClient;
    cache?: QueryCache;
    /** Base URL of the Cairn Core UI for "Open catalog view" links ('' = same origin); `null` hides them. */
    catalogUrl?: string | null;
}

export function ConsumerApp({
    routerMode = 'hash',
    basename = '/cairn',
    apiBaseUrl,
    graphqlUrl,
    client,
    cache,
    catalogUrl = '',
}: ConsumerAppProps) {
    const content = (
        <div className="cairn-consumer" style={{ fontFamily: 'inherit', color: tokens.color.text }}>
            <ConsumerRoutes />
        </div>
    );
    return (
        <ConfigProvider componentSize="middle">
            <I18nProvider>
                <CatalogUrlContext.Provider value={catalogUrl}>
                    <CairnProvider client={client} cache={cache} baseUrl={apiBaseUrl} graphqlUrl={graphqlUrl}>
                        {routerMode === 'browser' ? (
                            <BrowserRouter basename={basename}>{content}</BrowserRouter>
                        ) : (
                            <HashRouter>{content}</HashRouter>
                        )}
                    </CairnProvider>
                </CatalogUrlContext.Provider>
            </I18nProvider>
        </ConfigProvider>
    );
}
