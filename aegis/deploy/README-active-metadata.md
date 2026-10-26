# Deploying the Active Metadata & AI Context layer

This page covers deployment wiring only. For the design, see
[UX_ACTIVE_METADATA_SPEC.md](../UX_ACTIVE_METADATA_SPEC.md) (Part 8).

## Wiring

```
                         Browser (DataHub UI + Aegis workspace MFE)          BI tools / portals
                                    │ same origin (cookie)                       │ embed token
                                    ▼                                            ▼
                     ┌──────────── edge / ingress ─────────────────────────────────────────────┐
                     │ /            → datahub-frontend   (UI, /aegis/* workspace routes, GraphQL) │
                     │ /aegis-ui/*    → aegis-consumer-ui    (remoteEntry.js, embed/sdk.js)         │
                     │ /aegis/api/*   → aegis-api            (/aegis/api/embed/* is framable)         │
                     └─────────────────────────────────────────────────────────────────────────┘
datahub-frontend ──Bearer session JWT──► aegis-gms-proxy :8890 ──► datahub-gms :8080
   (MFE_CONFIG_FILE_PATH → mfe.config.yaml)   │ verifies JWT with DATAHUB_TOKEN_SERVICE_SIGNING_KEY
                                              │ masking obligations ◄── aegis-pdp
aegis-api / aegis-notifier / aegis-context-miner ───┴──► datahub-gms directly (AEGIS_GMS_URL), aegis-postgres
aegis-notifier ──HTTPS 443──► Slack Web API · Teams Workflows webhook · SMTP 587/465
Slack ──signed POST──► /aegis/api/integrations/slack/{interactions,commands}
```

| Service | Compose | Helm | Notes |
|---|---|---|---|
| `aegis-gms-proxy` | `aegis gms-proxy --port 8890 --upstream http://datahub-gms:8080` | `aegis.gmsProxy` (2 replicas, PDB, optional HPA, probes `/__aegis/health`) | It sits on the UI hot path, so if it is down the UI cannot reach GMS. The proxy is stateless. |
| `aegis-context-miner` | `aegis context-miner --interval 300` | `aegis.contextMiner` | Incremental. Cursors are kept in Postgres. |
| `aegis-notifier` | `aegis notifier --interval 10` | `aegis.notifier` (egress NetworkPolicy) | Needs outbound 443 (Slack/Teams) and 587/465 if SMTP is used |
| `aegis-consumer-ui` | nginx-unprivileged :8080 | `aegis.consumerUi` (+ optional Ingress `/aegis-ui`) | **Must** share the DataHub UI's origin |
| datahub-frontend | `DATAHUB_GMS_HOST=aegis-gms-proxy`, `DATAHUB_GMS_PORT=8890`, `MFE_CONFIG_FILE_PATH` | `datahub.datahub-frontend.datahub.gms.host/port` + MFE ConfigMap mount | Only the frontend is re-pointed. Aegis services and ingestion keep talking to GMS directly. |

Startup order (compose): `datahub-gms` + `aegis-pdp` → `aegis-gms-proxy` → `datahub-frontend` →
`aegis-bootstrap` (mints the Aegis PAT) → other Aegis services. The proxy deliberately does **not** wait for
`aegis-bootstrap`, because bootstrap waits for the frontend and that would create a cycle. The proxy picks
up the token file (`AEGIS_GMS_TOKEN_FILE`) once bootstrap has written it.

## Secrets

| Variable | Required | Helm source |
|---|---|---|
| `AEGIS_ACTION_LINK_SECRET` | yes | `aegis-secrets/action-link-secret` |
| `AEGIS_EMBED_SIGNING_SECRET` | yes | `aegis-secrets/embed-signing-secret` |
| `AEGIS_SESSION_SIGNING_KEY` (proxy only) | yes | `datahub-auth-secrets/token_service_signing_key` (the official chart's secret), used read-only |
| `AEGIS_SLACK_BOT_TOKEN`, `AEGIS_SLACK_SIGNING_SECRET` | if Slack is enabled | `aegis-slack/{bot-token,signing-secret}` |
| `AEGIS_TEAMS_WEBHOOK_URL` | if Teams is enabled | `aegis-teams/webhook-url` |
| `AEGIS_SMTP_URL` | if email is enabled | `aegis-smtp/smtp-url` |
| `AEGIS_EMBED_ALLOWED_ORIGINS` / `aegis.embeds.allowedOrigins` | for embeds | plain config, which becomes CSP `frame-ancestors` |

```bash
kubectl create secret generic aegis-secrets ... \
  --from-literal=action-link-secret=$(openssl rand -hex 32) \
  --from-literal=embed-signing-secret=$(openssl rand -hex 32)
helm upgrade --install datahub helm/aegis -f helm/aegis/values.yaml \
  -f helm/aegis/values-active-metadata.yaml
```

## Slack

1. Create the app from [`slack/app-manifest.yaml`](slack/app-manifest.yaml). Replace
   `https://aegis.example.com` with your `AEGIS_PUBLIC_URL`. Slack must be able to reach
   `/aegis/api/integrations/slack/*` over public HTTPS.
2. Install the app to the workspace, then copy the **Bot User OAuth Token** and the **Signing
   Secret** into the env/secret listed above.
3. Invite the bot to the channels that receive domain routes (`/invite @DataHub`). DMs need no invite
   (`im:write`).
4. Users are mapped by email (`users:read.email`), so a Slack user's email must match their DataHub
   `corpUserInfo.email`. Unmapped users get an ephemeral "link your account" reply, and no action is
   executed.
5. Verify the setup from Aegis with `POST /aegis/api/notify/test {"channel": "slack"}`.

Every request is signature-checked (`v0` HMAC-SHA256, at most 300 s clock skew). Configure the
ingress timeout to 3 s or more for these paths; Slack retries after 3 s.

## Microsoft Teams (Workflows webhook)

Teams uses incoming webhooks with Adaptive Cards. Buttons in the cards are **signed one-click
links** (`/aegis/api/notify/actions/<token>`) that open a confirmation page and require a DataHub
login, so no Teams bot registration is needed.

1. In Teams, open the target channel → **⋯** → **Workflows** → template **"Post to a channel when a
   webhook request is received"**.
2. Name it (e.g. *DataHub alerts*), choose the team and channel, and finish. Copy the generated
   HTTP POST URL (`https://…logic.azure.com/…` or `…environment.api.powerplatform.com/…`).
3. Set it as `AEGIS_TEAMS_WEBHOOK_URL` (the default route), or add per-domain routes in **Workspace →
   Settings → Notifications** (`PUT /aegis/api/notify/routes`).
4. Test the route with `POST /aegis/api/notify/test {"channel": "teams"}`.

Webhook URLs are bearer credentials, so store them only in secrets.

## Embeds

* The edge serves `/aegis/api/embed/*` **without** `X-Frame-Options`. `aegis-api` sends
  `Content-Security-Policy: frame-ancestors <AEGIS_EMBED_ALLOWED_ORIGINS>`, and an empty value means
  embeds can only be framed same-origin.
* The web-component SDK loads from `${AEGIS_PUBLIC_URL}/aegis-ui/embed/sdk.js`, served with
  `Cross-Origin-Resource-Policy: cross-origin`.
* With Helm, `aegis.embeds.allowedOrigins` must also be allowed by your ingress (don't inject a
  global `X-Frame-Options` for `/aegis/api/embed/`).

## Validating

```bash
docker compose -f docker-compose.aegis.yml config -q
helm lint helm/aegis -f helm/aegis/values-active-metadata.yaml
helm template datahub helm/aegis --kube-version 1.30.0 \
  -f helm/aegis/values-active-metadata.yaml | kubeconform -strict -ignore-missing-schemas
```
