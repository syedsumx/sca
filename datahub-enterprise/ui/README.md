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
npm test            # vitest (jsdom): client, chart scaling, helpers, page smoke tests
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
