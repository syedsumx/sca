# @dhe/web-react-modules

UI modules for the DataHub Enterprise (DHE) extension layer, built to drop into
[`datahub-web-react`](https://github.com/datahub-project/datahub/tree/master/datahub-web-react).
They use the stack datahub-web-react already ships: React 17, antd 4.24, styled-components 5, and
react-router 5. They talk to the **DHE control-plane REST API** (`/dhe/api` by default), not to
GMS GraphQL, so no GraphQL schema changes or codegen are needed.

| Module | Component | Route |
|---|---|---|
| Data quality | `QualityDashboard`, `MonitorEditor`, `MonitorDetail`, `EvaluationChart`, `DatasetHealthBadge`, `QualityTab` | `/enterprise/quality` |
| Governance | `CertificationInbox`, `PolicyEditor`, `AccessSimulator` | `/enterprise/governance/{inbox,policies,simulator}` |
| Remote agents | `AgentFleet` | `/enterprise/agents` |
| AI discovery | `AskDataHubPanel`, `DocDraftReview` | `/enterprise/ask`, `/enterprise/ai/docs` |

Every component handles its own loading, empty and error states (`401`/`403` are reported
explicitly). Authorization is always enforced server-side by the DHE PDP. The `privilege` hints on
routes only control which nav entries are shown.

## Develop standalone

```bash
npm install
npm run typecheck   # tsc --noEmit
npm test            # vitest (jsdom): client, chart scaling, helpers, page smoke tests, consumer
                    # workspace (API/cache/URL state/components/mount) and embed elements
```

## Integrate into datahub-web-react

1. **Copy the sources.** Copy `src/` to `datahub-web-react/src/app/enterprise/`. Nothing here
   imports from outside the folder, and there are no new runtime dependencies (`@ant-design/icons`
   is a devDependency only).

2. **Route `/dhe/api` to the DHE API.** In production, place the DHE API behind the same origin
   (ingress path `/dhe/api` → `dhe-api:8800`). That way the DataHub session cookie
   (`credentials: 'include'`) reaches DHE, which validates it against GMS. For local
   development, add a proxy to `vite.config.ts`:

   ```ts
   server: {
       proxy: {
           '/dhe/api': { target: 'http://localhost:8800', rewrite: (p) => p.replace(/^\/dhe\/api/, '') },
       },
   },
   ```

   You can also pass a bearer token (a DataHub PAT) explicitly:
   `<DheClientProvider baseUrl="https://dhe.example.com" token={() => getPat()}>`.

3. **Register the routes.** In `src/app/SearchRoutes.tsx` (inside its `<Switch>`):

   ```tsx
   import { ENTERPRISE_ROUTES } from '@app/enterprise/routes';
   import { DheClientProvider } from '@app/enterprise/api/hooks';

   {ENTERPRISE_ROUTES.map((r) => (
       <Route
           key={r.path}
           path={r.path}
           render={() => (
               <DheClientProvider>
                   <r.component />
               </DheClientProvider>
           )}
       />
   ))}
   ```

   `DheClientProvider` is optional. Without it, components fall back to a default client against
   `/dhe/api`. Wrapping once near the app root (for example in `AppProviders.tsx`) is the cleanest
   option.

4. **Add nav entries.** In the V2 nav sidebar (`src/app/homeV2/layout/navBarRedesign/NavSidebar.tsx`)
   or the legacy header menu, add one group per `route.group`. Filter entries with the user's
   platform privileges:

   ```tsx
   import { visibleRoutes } from '@app/enterprise/routes';
   const me = useUserContext();
   const privileges = Object.entries(me.platformPrivileges ?? {})
       .filter(([, v]) => v === true)
       .map(([k]) => k.replace(/([A-Z])/g, '_$1').toUpperCase()); // manageIngestion → MANAGE_INGESTION
   const items = visibleRoutes(privileges).map((r) => ({ key: r.path, title: r.label, link: r.path }));
   ```

5. **Embed quality on the dataset profile.** In
   `src/app/entityV2/dataset/DatasetEntity.tsx`, add a tab to `getProfileTabs()`, and put the
   badge in the header or sidebar:

   ```tsx
   import { QualityTab } from '@app/enterprise/quality/QualityTab';
   import { DatasetHealthBadge } from '@app/enterprise/quality/DatasetHealthBadge';

   // tabs
   {
       name: 'Monitors',
       component: () => <QualityTab urn={useEntityData().urn} />,
   },

   // header / sidebar section
   <DatasetHealthBadge datasetUrn={urn} onClick={() => routeToTab({ tabName: 'Monitors' })} />
   ```

   DHE also emits native `assertion` / `assertionRunEvent` aspects, so its monitors appear in the
   built-in **Quality** tab as well. `QualityTab` adds learned bands, anomaly feedback, and editing.

6. **(Optional) Ask DataHub in a drawer.** `<AskDataHubPanel embedded onOpenEntity={(urn) =>
   history.push(entityRegistry.getEntityUrl(...))} />` fits in an antd `Drawer` opened from the
   search bar.

7. Run `./gradlew :datahub-web-react:lint` and `yarn type-check` in datahub-web-react. The repo's
   eslint import-order rules may ask you to reorder imports. The code itself needs no changes.

## Consumer workspace (micro-frontend)

`src/consumer/` is the end-user workspace: Discover, Search, Asset, Data Marketplace and Requests,
Assistant, Context Inbox (with the ontology view), Governance Studio (personas and purposes), and
Settings (notifications, inbox, embed builder). It ships as a **Webpack 5 Module Federation remote**
that DataHub's built-in MFE host (`app/mfeframework`) mounts, so datahub-web-react needs no fork.

```bash
npm run build:mfe     # → dist/mfe/remoteEntry.js + hashed chunks (var library "dheConsumer", exposes ./mount)
npm run build:embed   # → dist/embed/sdk.js, frame.js, frame.html
npm run build         # both
```

- **Host config.** Point `MFE_CONFIG_FILE_PATH` on datahub-frontend at a file like
  [`mfe.config.yaml`](mfe.config.yaml) (`module: dheConsumer/mount`, `remoteEntry: /dhe-ui/remoteEntry.js`).
  The host calls `mount(el, {})` and keeps the returned cleanup function.
- **Routing.** The host registers `/mfe/dhe` as an exact route, so the workspace routes inside the URL
  hash: `/mfe/dhe#/search?q=orders&f.platform=…`, `/mfe/dhe#/asset/<urn>`. Search state (query,
  facets, page) lives in the hash and is shareable. Set `data-dhe-router="browser"` on the mount
  element, or pass `routerMode: 'browser'`, when hosting it at a non-exact route.
- **No shared deps.** The host shares nothing, so the remote bundles its own React 17, antd,
  styled-components and react-router. It renders into the host DOM and uses the host's antd 4 CSS;
  the bundle has no CSS of its own.
- **Data.** The remote calls DataHub GraphQL (`/api/v2/graphql`) for search and asset details, and
  the DHE API (`/dhe/api`) for everything else, with the user's session cookie. Serve `/dhe-ui/` from
  the same origin (the `dhe-consumer-ui` image behind the edge proxy).
- **Serving.** `docker/consumer-ui.Dockerfile` builds both bundles and serves them from unprivileged
  nginx on `:8080`, at `/dhe-ui/` and `/dhe-ui/embed/`. `remoteEntry.js`, `sdk.js` and `frame.*` are
  served with `no-cache`; hashed chunks are `immutable`; responses carry CORS and CORP headers so
  third-party pages can load the SDK.
- **Embedding in your own React app.** `import { consumer } from '@dhe/web-react-modules'`, then render
  `<consumer.ConsumerApp routerMode="browser" basename="/workspace" />`.

Building blocks, for reuse: `useDheQuery(key, loader)` (SWR cache with dedupe, `invalidate(prefix)`,
and optimistic updates with rollback), `TrustBadges`, `AssetCard` and `AssetRow`, `BookmarkToggle`,
`OmniSearchBar` (`/` or ⌘K to focus), `FacetPanel`, `MarkdownView` (safe: no raw HTML, http(s),
mailto and relative links only), the discussion thread with @mentions, `ProductCard`,
`RequestAccessDrawer`, and `ConfidenceMeter`.

Bundle sizes (production, gzip -9):

| File | gz |
|---|---|
| `remoteEntry.js` | 2.4 kB |
| initial `./mount` (vendor 541 + app shell 531) | 146.5 kB + 12.8 kB |
| all lazy route chunks combined | ≈ 180 kB |
| `embed/sdk.js` (no dependencies) | 4.0 kB |

## Embeds (web components)

`src/embed/` is a dependency-free SDK of custom elements that render in shadow DOM:
`<dhe-asset-card>`, `<dhe-quality-badge>` and `<dhe-lineage>`. They are authenticated with a scoped,
read-only embed token (no cookies), and `frame.ts` renders the same elements as an iframe page.
Usage for Tableau, Looker, Power BI and portals is in [`../embeds/`](../embeds/README.md).

```html
<script src="https://DATAHUB_HOST/dhe-ui/embed/sdk.js" defer></script>
<dhe-asset-card urn="urn:li:dataset:(…)" token="…"></dhe-asset-card>
```

## API contract

The wire types are in `src/api/types.ts` and match the snake_case payloads of the DHE FastAPI
service field for field. `src/api/client.ts` is a typed `fetch` wrapper. It provides:

- `DheApiError`, with `status`, `body`, `path`, and the helpers `isUnauthorized`, `isForbidden`, and `isNotFound`
- `AbortSignal` support on every call
- query-string building that drops empty values
- FastAPI `{detail}` error extraction

Hooks (`src/api/hooks.ts`):

- `useAsync(loader, deps)`: aborts stale requests and ignores out-of-order results
- `usePolling(loader, ms, deps)`: pauses while the tab is hidden
- `useMutation(fn)`
