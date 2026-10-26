import { ConfigProvider, Result, Spin } from 'antd';
import React, { Suspense } from 'react';
import { BrowserRouter, HashRouter, Redirect, Route, Switch } from 'react-router-dom';

import { ConsumerClient } from './api/client';
import { QueryCache } from './api/cache';
import { ConsumerLayout } from './components/Layout';
import { DataHubUrlContext } from './context/NativeLinks';
import { tokens } from './components/tokens';
import { AegisProvider } from './context/AegisProvider';

// Route-level code splitting keeps the initial MFE payload small (spec Part 9: ≤ 250 kB gz).
const DiscoverPage = React.lazy(() => import('./pages/DiscoverPage').then((m) => ({ default: m.DiscoverPage })));
const SearchPage = React.lazy(() => import('./pages/SearchPage').then((m) => ({ default: m.SearchPage })));
const AssetPage = React.lazy(() => import('./pages/AssetPage').then((m) => ({ default: m.AssetPage })));
const MarketplacePage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.MarketplacePage })));
const ProductPage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.ProductPage })));
const RequestsPage = React.lazy(() => import('./pages/MarketplacePages').then((m) => ({ default: m.RequestsPage })));
const AssistantPage = React.lazy(() => import('./pages/AssistantPage').then((m) => ({ default: m.AssistantPage })));
const ContextInboxPage = React.lazy(() => import('./pages/ContextInboxPage').then((m) => ({ default: m.ContextInboxPage })));
const GovernanceStudioPage = React.lazy(() =>
    import('./pages/GovernanceStudioPage').then((m) => ({ default: m.GovernanceStudioPage })),
);
const SettingsPage = React.lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));

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
    { path: '/settings', component: SettingsPage, exact: true },
];

class ErrorBoundary extends React.Component<{ children?: React.ReactNode }, { error: Error | null }> {
    state = { error: null as Error | null };

    static getDerivedStateFromError(error: Error) {
        return { error };
    }

    render() {
        if (this.state.error) {
            return (
                <Result
                    status="error"
                    title="Something went wrong"
                    subTitle={this.state.error.message}
                    extra={<a href={window.location.pathname}>Reload</a>}
                />
            );
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
                        <Route render={() => <Result status="404" title="Page not found" extra={<a href="./discover">Go to Discover</a>} />} />
                    </Switch>
                </Suspense>
            </ErrorBoundary>
        </ConsumerLayout>
    );
}

export interface ConsumerAppProps {
    /**
     * `hash` (default) when hosted as a DataHub MFE: the host registers `/mfe<path>` as an *exact*
     * route, so in-app navigation must not change the pathname (URLs look like `/mfe/aegis#/search?q=x`).
     * `browser` for standalone hosting under `basename`.
     */
    routerMode?: 'hash' | 'browser';
    /** Path prefix for `browser` mode. */
    basename?: string;
    /** Aegis API base URL (default `/aegis/api`). */
    apiBaseUrl?: string;
    graphqlUrl?: string;
    client?: ConsumerClient;
    cache?: QueryCache;
    /** Base URL of the DataHub UI for "Open catalog view" links ('' = same origin); `null` hides them. */
    datahubUrl?: string | null;
}

export function ConsumerApp({
    routerMode = 'hash',
    basename = '/aegis',
    apiBaseUrl,
    graphqlUrl,
    client,
    cache,
    datahubUrl = '',
}: ConsumerAppProps) {
    const content = (
        <div className="aegis-consumer" style={{ fontFamily: 'inherit', color: tokens.color.text }}>
            <ConsumerRoutes />
        </div>
    );
    return (
        <ConfigProvider componentSize="middle">
            <DataHubUrlContext.Provider value={datahubUrl}>
                <AegisProvider client={client} cache={cache} baseUrl={apiBaseUrl} graphqlUrl={graphqlUrl}>
                    {routerMode === 'browser' ? (
                        <BrowserRouter basename={basename}>{content}</BrowserRouter>
                    ) : (
                        <HashRouter>{content}</HashRouter>
                    )}
                </AegisProvider>
            </DataHubUrlContext.Provider>
        </ConfigProvider>
    );
}
