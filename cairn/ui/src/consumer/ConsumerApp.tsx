import { ConfigProvider, Result, Spin } from 'antd';
import React, { Suspense, useEffect } from 'react';
import { BrowserRouter, HashRouter, Redirect, Route, Switch, useLocation } from 'react-router-dom';

import { ConsumerClient } from './api/client';
import { QueryCache } from './api/cache';
import { ConsumerLayout, PLATFORM_MAP } from './components/Layout';
import { CatalogUrlContext } from './context/NativeLinks';
import { tokens } from './components/tokens';
import { CairnProvider } from './context/CairnProvider';
import { I18nProvider, useT } from './i18n';
import { HUBS } from './resources/hubs';
import { describeError } from './components/ErrorNotice';
import { movedTarget } from './moves';

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
const AssetPage = React.lazy(() => import('./pages/AssetPage').then((m) => ({ default: m.AssetPage })));
const RequestsPage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.RequestsPage })));
// The Catalog section (Cairn Core at the centre, L147).
const CatalogPage = React.lazy(() => import('./pages/CatalogPage').then((m) => ({ default: m.CatalogPage })));
const LineagePage = React.lazy(() => import('./pages/CatalogPage').then((m) => ({ default: m.LineagePage })));
const DataProductsPage = React.lazy(() => import('./pages/ProductsPages').then((m) => ({ default: m.DataProductsPage })));
const DataProductPage = React.lazy(() => import('./pages/ProductsPages').then((m) => ({ default: m.DataProductPage })));
const ContractPage = React.lazy(() => import('./pages/ProductsPages').then((m) => ({ default: m.ContractPage })));
const GlossaryPage = React.lazy(() => import('./pages/GlossaryDomainsPages').then((m) => ({ default: m.GlossaryPage })));
const GlossaryEntryPage = React.lazy(() =>
    import('./pages/GlossaryDomainsPages').then((m) => ({ default: m.GlossaryEntryPage })),
);
const DomainsPage = React.lazy(() => import('./pages/GlossaryDomainsPages').then((m) => ({ default: m.DomainsPage })));
const DomainPage = React.lazy(() => import('./pages/GlossaryDomainsPages').then((m) => ({ default: m.DomainPage })));
const AssistantPage = React.lazy(() => import('./pages/AssistantPage').then((m) => ({ default: m.AssistantPage })));
const ReportsPage = React.lazy(() => import('./pages/ReportsPage').then((m) => ({ default: m.ReportsPage })));
const DashboardsPage = React.lazy(() => import('./pages/DashboardsPage').then((m) => ({ default: m.DashboardsPage })));
const FlowsPage = React.lazy(() => import('./pages/FlowsPage').then((m) => ({ default: m.FlowsPage })));
const OntologyTermPage = React.lazy(() => import('./pages/OntologyPage').then((m) => ({ default: m.OntologyTermPage })));
const InputTablesPage = React.lazy(() => import('./pages/InputTablesPage').then((m) => ({ default: m.InputTablesPage })));
const WorkbooksPage = React.lazy(() => import('./pages/WorkbooksPage').then((m) => ({ default: m.WorkbooksPage })));
const GovernanceStudioPage = React.lazy(() =>
    import('./pages/GovernanceStudioPage').then((m) => ({ default: m.GovernanceStudioPage })),
);
const PeoplePage = React.lazy(() => import('./pages/PeoplePage').then((m) => ({ default: m.PeoplePage })));
const ObservabilityPage = React.lazy(() =>
    import('./pages/ObservabilityPage').then((m) => ({ default: m.ObservabilityPage })),
);
const InboxPage = React.lazy(() => import('./pages/InboxPage').then((m) => ({ default: m.InboxPage })));
const SettingsPage = React.lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));
const ProfilePage = React.lazy(() => import('./pages/ProfilePage').then((m) => ({ default: m.ProfilePage })));
const DevicePage = React.lazy(() => import('./pages/DevicePage').then((m) => ({ default: m.DevicePage })));
const CapabilitiesPage = React.lazy(() =>
    import('./resources/CapabilitiesPage').then((m) => ({ default: m.CapabilitiesPage })),
);
const ExtensionsPage = React.lazy(() => import('./components/Extensions').then((m) => ({ default: m.ExtensionsPage })));
const GetStartedPage = React.lazy(() => import('./guide/GetStarted').then((m) => ({ default: m.GetStartedPage })));

export const CONSUMER_ROUTES: { path: string; component: React.ComponentType; exact?: boolean }[] = [
    { path: '/discover', component: DiscoverPage, exact: true },
    { path: '/get-started', component: GetStartedPage, exact: true },
    { path: '/catalog', component: CatalogPage, exact: true },
    { path: '/products', component: DataProductsPage, exact: true },
    { path: '/products/:urn', component: DataProductPage },
    { path: '/contracts/:dataset', component: ContractPage },
    { path: '/glossary', component: GlossaryPage, exact: true },
    { path: '/glossary/:urn', component: GlossaryEntryPage },
    { path: '/domains', component: DomainsPage, exact: true },
    { path: '/domains/:urn', component: DomainPage },
    { path: '/lineage', component: LineagePage, exact: true },
    { path: '/asset/:urn', component: AssetPage },
    { path: '/requests', component: RequestsPage, exact: true },
    { path: '/assistant', component: AssistantPage, exact: true },
    { path: '/governance', component: GovernanceStudioPage, exact: true },
    { path: '/people', component: PeoplePage, exact: true },
    { path: '/observability', component: ObservabilityPage, exact: true },
    { path: '/reports', component: ReportsPage, exact: true },
    { path: '/flows', component: FlowsPage, exact: true },
    { path: '/ontology/terms/:id', component: OntologyTermPage },
    { path: '/sql/workbooks', component: WorkbooksPage, exact: true },
    { path: '/sql/input-tables', component: InputTablesPage, exact: true },
    { path: '/dashboards', component: DashboardsPage, exact: true },
    ...HUB_ROUTES,
    ...(PLATFORM_MAP ? [{ path: '/capabilities', component: CapabilitiesPage, exact: true }] : []),
    { path: '/profile', component: ProfilePage, exact: true },
    { path: '/inbox', component: InboxPage, exact: true },
    { path: '/device', component: DevicePage, exact: true },
    { path: '/settings', component: SettingsPage, exact: true },
    { path: '/extensions', component: ExtensionsPage, exact: true },
];

function ErrorView({ error }: { error: Error }) {
    const t = useT();
    return (
        <Result
            status="error"
            title={t('error.title')}
            subTitle={describeError(error)}
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

/** Old links and bookmarks: a route or tab that moved (L149) is redirected to its one home before anything renders. */
function Moved({ children }: { children: React.ReactNode }) {
    const location = useLocation();
    const to = movedTarget(location.pathname, location.search);
    return to ? <Redirect to={to} /> : <>{children}</>;
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
                    <Moved>
                        <Switch>
                            {CONSUMER_ROUTES.map((r) => (
                                <Route key={r.path} path={r.path} exact={r.exact} component={r.component} />
                            ))}
                            <Redirect exact from="/" to="/discover" />
                            <Route render={() => <NotFound />} />
                        </Switch>
                    </Moved>
                </Suspense>
            </ErrorBoundary>
        </ConsumerLayout>
    );
}

/** While the workspace is mounted the page carries `cairn-ui`, so antd's dialogs and popups (rendered in <body>, outside
 * the workspace's element) use the Cairn accent too (tokens.ts, PRIMARY_CSS). */
export function useWorkspaceBodyClass(): void {
    useEffect(() => {
        const body = typeof document !== 'undefined' ? document.body : null;
        if (!body) return undefined;
        body.classList.add('cairn-ui');
        return () => body.classList.remove('cairn-ui');
    }, []);
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
    useWorkspaceBodyClass();
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
