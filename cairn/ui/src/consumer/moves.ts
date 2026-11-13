/**
 * Where things moved (L149, one home per concept). Every route and tab of earlier layouts still opens: it redirects
 * to the concept's one home, keeping the rest of its query string (`?q=`, `?incident=`, `?status=` …). Old links,
 * bookmarks, notifications and the docs' deep links therefore keep working.
 *
 * A move is either a whole route that no longer has a page of its own (`to`), or tabs of a page that still exists
 * but whose tab now lives elsewhere (`tabs`, with no `to`). `:urn` in `from` is carried into the target.
 */
export interface Move {
    from: string;
    /** The whole route's new home (absent: the route still has a page; only the tabs listed below moved). */
    to?: string;
    /** `?tab=<key>` → its new home. */
    tabs?: Record<string, string>;
}

export const MOVES: Move[] = [
    // Catalog
    { from: '/search', to: '/catalog' },
    { from: '/marketplace', to: '/products?view=browse' },
    {
        from: '/marketplace/:urn', to: '/products/:urn',
        tabs: { readme: '/products/:urn?tab=overview', assets: '/products/:urn?tab=assets', policy: '/products/:urn?tab=access',
            slos: '/products/:urn?tab=contracts', publish: '/products/:urn?tab=distribution' },
    },
    { from: '/contracts', to: '/quality?tab=contracts' },
    { from: '/sql/metrics', to: '/semantics?tab=metrics' },
    {
        from: '/semantic', to: '/semantics?tab=ontology',
        tabs: {
            'semantic-models': '/semantics?tab=review&view=drafts',
            'ontology-terms': '/semantics?tab=ontology&view=terms',
            'ontology-relationships': '/semantics?tab=ontology&view=relationships',
            'ontology-graph': '/semantics?tab=ontology&view=graph',
            'ontology-proposals': '/semantics?tab=review&view=ontology',
            'ontology-runs': '/semantics?tab=ontology&view=runs',
        },
    },
    {
        from: '/ontology', to: '/semantics?tab=ontology',
        tabs: {
            graph: '/semantics?tab=ontology&view=graph', list: '/semantics?tab=ontology&view=list',
            review: '/semantics?tab=review&view=ontology', compile: '/semantics?tab=ontology&view=compile',
        },
    },
    { from: '/context', to: '/semantics?tab=review&view=findings' },
    // Govern
    {
        from: '/trust', to: '/security',
        tabs: {
            'access-reviews': '/people?tab=access-reviews', residency: '/governance?tab=residency', posture: '/security?tab=posture',
            tokens: '/governance?tab=tokens', consent: '/governance?tab=consent', contracts: '/quality?tab=contracts',
            passkeys: '/security?tab=passkeys', 'secret-scopes': '/security?tab=secret-scopes',
            'egress-rules': '/compute?tab=egress-rules', dataplanes: '/compute?tab=dataplanes',
            'workspace-compute': '/compute?tab=workspace-compute', siem: '/security?tab=siem',
            'notification-routes': '/alerts?tab=notification-routes', limits: '/security?tab=limits',
            workspaces: '/people?tab=workspaces',
        },
    },
    {
        from: '/governance',
        tabs: {
            forms: '/automation?tab=forms', steward: '/semantics?tab=review&view=steward', automations: '/automation?tab=automations',
            playbooks: '/automation?tab=playbooks', 'tag-sync': '/automation?tab=tag-sync', 'ai-systems': '/security?tab=ai-systems',
        },
    },
    { from: '/sharing', tabs: { slos: '/quality?tab=slos' } },
    { from: '/usage', tabs: { 'ai-gateway-models': '/ai/gateway?tab=ai-gateway-models', 'gateway-routes': '/ai/gateway?tab=gateway-routes' } },
    // Quality & operations
    // Incident notifications link to /observability/incidents/<id>: it opens in the one incident list.
    { from: '/observability/incidents/:id', to: '/incidents?incident=:id' },
    // Access review notifications link to /governance/access-reviews/<id>: it opens the reviewer's list.
    { from: '/governance/access-reviews/:id', to: '/people?tab=access-reviews' },
    { from: '/observability', tabs: { pipelines: '/runs?tab=pipelines', alerts: '/alerts?tab=platform-rules', incidents: '/incidents' } },
    { from: '/pipelines', to: '/runs?tab=dbt',
        tabs: { dbt: '/runs?tab=dbt', pipes: '/ingestion?tab=pipes', streams: '/ingestion?tab=streams', derived: '/lakehouse?tab=derived',
            maintenance: '/lakehouse?tab=maintenance', warehouses: '/compute?tab=warehouses', shares: '/sharing?tab=shares' } },
    { from: '/dashboards', tabs: { alerts: '/alerts?tab=sql-alerts' } },
    // Analyze and build
    { from: '/queries', to: '/sql?tab=insight' },
    { from: '/sql/git', to: '/sql?tab=git' },
    { from: '/sql/workflows', to: '/flows?tab=sql-workflows' },
    { from: '/compute', tabs: { 'saved-queries': '/sql?tab=saved-queries', jobs: '/flows?tab=jobs', 'airflow-deployments': '/flows?tab=airflow' } },
    { from: '/lakehouse', tabs: { 'quality-monitors': '/quality?tab=quality-monitors', procedures: '/sql?tab=procedures' } },
    // AI
    {
        from: '/agents',
        tabs: { 'remote-agents': '/ingestion?tab=collectors', 'ai-steward': '/semantics?tab=review&view=steward',
            'agent-listings': '/products?view=browse' },
    },
    // Platform: Settings keeps general & guide, licence, extensions, infrastructure as code and embeds.
    {
        from: '/settings',
        tabs: { notifications: '/profile', inbox: '/inbox', 'ai-agents': '/agents?tab=agents', workspaces: '/people?tab=workspaces',
            sources: '/ingestion?tab=sources', agents: '/ingestion?tab=collectors', storage: '/lakehouse?tab=storage' },
    },
];

function match(pattern: string, pathname: string): Record<string, string> | null {
    const want = pattern.split('/');
    const got = pathname.replace(/\/+$/, '').split('/');
    if (want.length !== got.length) return null;
    const vars: Record<string, string> = {};
    for (let i = 0; i < want.length; i += 1) {
        if (want[i].startsWith(':')) {
            if (!got[i]) return null;
            vars[want[i].slice(1)] = got[i];
        } else if (want[i] !== got[i]) {
            return null;
        }
    }
    return vars;
}

/** The new home of `pathname` + `search` (a path with its query), or null when it has not moved. */
export function movedTarget(pathname: string, search: string): string | null {
    const params = new URLSearchParams(search);
    const tab = params.get('tab');
    for (const m of MOVES) {
        const vars = match(m.from, pathname);
        if (!vars) continue;
        const raw = (tab && m.tabs?.[tab]) || m.to;
        if (!raw) return null;
        const target = raw.replace(/:(\w+)/g, (_, k: string) => vars[k] ?? '');
        const [path, query = ''] = target.split('?');
        const out = new URLSearchParams(query);
        const fixed = new Set(out.keys());               // what the target sets wins; every other value is kept
        params.forEach((v, k) => {
            if (k !== 'tab' && !fixed.has(k)) out.append(k, v);   // repeated (f.domains=a&f.domains=b): all of them
        });
        const qs = out.toString();
        return `${path}${qs ? `?${qs}` : ''}`;
    }
    return null;
}

/** Every route of an earlier layout that now redirects (the vitest table checks each lands on a page). */
export const MOVED_ROUTES: string[] = MOVES.flatMap((m) => [
    ...(m.to ? [m.from] : []),
    ...Object.keys(m.tabs ?? {}).map((t) => `${m.from}?tab=${t}`),
]);
