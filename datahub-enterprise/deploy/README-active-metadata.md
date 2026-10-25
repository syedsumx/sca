# Deploying the Active Metadata & AI Context layer

This page covers deployment wiring only. For the design, see
[UX_ACTIVE_METADATA_SPEC.md](../UX_ACTIVE_METADATA_SPEC.md) (Part 8).

## Wiring

```
                         Browser (DataHub UI + DHE workspace MFE)          BI tools / portals
                                    │ same origin (cookie)                       │ embed token
                                    ▼                                            ▼
                     ┌──────────── edge / ingress ─────────────────────────────────────────────┐
                     │ /            → datahub-frontend   (UI, /dhe/* workspace routes, GraphQL) │
                     │ /dhe-ui/*    → dhe-consumer-ui    (remoteEntry.js, embed/sdk.js)         │
                     │ /dhe/api/*   → dhe-api            (/dhe/api/embed/* is framable)         │
                     └─────────────────────────────────────────────────────────────────────────┘
datahub-frontend ──Bearer session JWT──► dhe-gms-proxy :8890 ──► datahub-gms :8080
   (MFE_CONFIG_FILE_PATH → mfe.config.yaml)   │ verifies JWT with DATAHUB_TOKEN_SERVICE_SIGNING_KEY
                                              │ masking obligations ◄── dhe-pdp
dhe-api / dhe-notifier / dhe-context-miner ───┴──► datahub-gms directly (DHE_GMS_URL), dhe-postgres
dhe-notifier ──HTTPS 443──► Slack Web API · Teams Workflows webhook · SMTP 587/465
Slack ──signed POST──► /dhe/api/integrations/slack/{interactions,commands}
```

| Service | Compose | Helm | Notes |
|---|---|---|---|
| `dhe-gms-proxy` | `dhe gms-proxy --port 8890 --upstream http://datahub-gms:8080` | `dhe.gmsProxy` (2 replicas, PDB, optional HPA, probes `/__dhe/health`) | It sits on the UI hot path, so if it is down the UI cannot reach GMS. The proxy is stateless. |
| `dhe-context-miner` | `dhe context-miner --interval 300` | `dhe.contextMiner` | Incremental. Cursors are kept in Postgres. |
| `dhe-notifier` | `dhe notifier --interval 10` | `dhe.notifier` (egress NetworkPolicy) | Needs outbound 443 (Slack/Teams) and 587/465 if SMTP is used |
| `dhe-consumer-ui` | nginx-unprivileged :8080 | `dhe.consumerUi` (+ optional Ingress `/dhe-ui`) | **Must** share the DataHub UI's origin |
| datahub-frontend | `DATAHUB_GMS_HOST=dhe-gms-proxy`, `DATAHUB_GMS_PORT=8890`, `MFE_CONFIG_FILE_PATH` | `datahub.datahub-frontend.datahub.gms.host/port` + MFE ConfigMap mount | Only the frontend is re-pointed. DHE services and ingestion keep talking to GMS directly. |

Startup order (compose): `datahub-gms` + `dhe-pdp` → `dhe-gms-proxy` → `datahub-frontend` →
`dhe-bootstrap` (mints the DHE PAT) → other DHE services. The proxy deliberately does **not** wait for
`dhe-bootstrap`, because bootstrap waits for the frontend and that would create a cycle. The proxy picks
up the token file (`DHE_GMS_TOKEN_FILE`) once bootstrap has written it.

## Secrets

| Variable | Required | Helm source |
|---|---|---|
| `DHE_ACTION_LINK_SECRET` | yes | `dhe-secrets/action-link-secret` |
| `DHE_EMBED_SIGNING_SECRET` | yes | `dhe-secrets/embed-signing-secret` |
| `DHE_SESSION_SIGNING_KEY` (proxy only) | yes | `datahub-auth-secrets/token_service_signing_key` (the official chart's secret), used read-only |
| `DHE_SLACK_BOT_TOKEN`, `DHE_SLACK_SIGNING_SECRET` | if Slack is enabled | `dhe-slack/{bot-token,signing-secret}` |
| `DHE_TEAMS_WEBHOOK_URL` | if Teams is enabled | `dhe-teams/webhook-url` |
| `DHE_SMTP_URL` | if email is enabled | `dhe-smtp/smtp-url` |
| `DHE_EMBED_ALLOWED_ORIGINS` / `dhe.embeds.allowedOrigins` | for embeds | plain config, which becomes CSP `frame-ancestors` |

```bash
kubectl create secret generic dhe-secrets ... \
  --from-literal=action-link-secret=$(openssl rand -hex 32) \
  --from-literal=embed-signing-secret=$(openssl rand -hex 32)
helm upgrade --install datahub helm/datahub-enterprise -f helm/datahub-enterprise/values.yaml \
  -f helm/datahub-enterprise/values-active-metadata.yaml
```

## Slack

1. Create the app from [`slack/app-manifest.yaml`](slack/app-manifest.yaml). Replace
   `https://datahub.example.com` with your `DHE_PUBLIC_URL`. Slack must be able to reach
   `/dhe/api/integrations/slack/*` over public HTTPS.
2. Install the app to the workspace, then copy the **Bot User OAuth Token** and the **Signing
   Secret** into the env/secret listed above.
3. Invite the bot to the channels that receive domain routes (`/invite @DataHub`). DMs need no invite
   (`im:write`).
4. Users are mapped by email (`users:read.email`), so a Slack user's email must match their DataHub
   `corpUserInfo.email`. Unmapped users get an ephemeral "link your account" reply, and no action is
   executed.
5. Verify the setup from DHE with `POST /dhe/api/notify/test {"channel": "slack"}`.

Every request is signature-checked (`v0` HMAC-SHA256, at most 300 s clock skew). Configure the
ingress timeout to 3 s or more for these paths; Slack retries after 3 s.

## Microsoft Teams (Workflows webhook)

Teams uses incoming webhooks with Adaptive Cards. Buttons in the cards are **signed one-click
links** (`/dhe/api/notify/actions/<token>`) that open a confirmation page and require a DataHub
login, so no Teams bot registration is needed.

1. In Teams, open the target channel → **⋯** → **Workflows** → template **"Post to a channel when a
   webhook request is received"**.
2. Name it (e.g. *DataHub alerts*), choose the team and channel, and finish. Copy the generated
   HTTP POST URL (`https://…logic.azure.com/…` or `…environment.api.powerplatform.com/…`).
3. Set it as `DHE_TEAMS_WEBHOOK_URL` (the default route), or add per-domain routes in **Workspace →
   Settings → Notifications** (`PUT /dhe/api/notify/routes`).
4. Test the route with `POST /dhe/api/notify/test {"channel": "teams"}`.

Webhook URLs are bearer credentials, so store them only in secrets.

## Embeds

* The edge serves `/dhe/api/embed/*` **without** `X-Frame-Options`. `dhe-api` sends
  `Content-Security-Policy: frame-ancestors <DHE_EMBED_ALLOWED_ORIGINS>`, and an empty value means
  embeds can only be framed same-origin.
* The web-component SDK loads from `${DHE_PUBLIC_URL}/dhe-ui/embed/sdk.js`, served with
  `Cross-Origin-Resource-Policy: cross-origin`.
* With Helm, `dhe.embeds.allowedOrigins` must also be allowed by your ingress (don't inject a
  global `X-Frame-Options` for `/dhe/api/embed/`).

## Validating

```bash
docker compose -f docker-compose.enterprise.yml config -q
helm lint helm/datahub-enterprise -f helm/datahub-enterprise/values-active-metadata.yaml
helm template datahub helm/datahub-enterprise --kube-version 1.30.0 \
  -f helm/datahub-enterprise/values-active-metadata.yaml | kubeconform -strict -ignore-missing-schemas
```
