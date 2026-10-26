# Deploying the Active Metadata & AI Context layer

This page covers deployment wiring only. For the design, see
[UX_ACTIVE_METADATA_SPEC.md](../UX_ACTIVE_METADATA_SPEC.md) (Part 8).

## Wiring

```
                         Browser (Cairn Core UI + Cairn workspace MFE)          BI tools / portals
                                    │ same origin (cookie)                       │ embed token
                                    ▼                                            ▼
                     ┌──────────── edge / ingress ─────────────────────────────────────────────┐
                     │ /            → cairn-web   (UI, /cairn/* workspace routes, GraphQL) │
                     │ /cairn-ui/*    → cairn-consumer-ui    (remoteEntry.js, embed/sdk.js)         │
                     │ /cairn/api/*   → cairn-api            (/cairn/api/embed/* is framable)         │
                     └─────────────────────────────────────────────────────────────────────────┘
cairn-web ──Bearer session JWT──► cairn-core-proxy :8890 ──► cairn-core :8080
   (MFE_CONFIG_FILE_PATH → mfe.config.yaml)   │ verifies JWT with CAIRN_SESSION_SIGNING_KEY
                                              │ masking obligations ◄── cairn-pdp
cairn-api / cairn-notifier / cairn-context-miner ───┴──► cairn-core directly (CAIRN_CORE_URL), cairn-postgres
cairn-notifier ──HTTPS 443──► Slack Web API · Teams Workflows webhook · SMTP 587/465
Slack ──signed POST──► /cairn/api/integrations/slack/{interactions,commands}
```

| Service | Compose | Helm | Notes |
|---|---|---|---|
| `cairn-core-proxy` | `cairn core-proxy --port 8890 --upstream http://cairn-core:8080` | `cairn.coreProxy` (2 replicas, PDB, optional HPA, probes `/__cairn/health`) | It sits on the UI hot path, so if it is down the UI cannot reach Cairn Core. The proxy is stateless. |
| `cairn-context-miner` | `cairn context-miner --interval 300` | `cairn.contextMiner` | Incremental. Cursors are kept in Postgres. |
| `cairn-notifier` | `cairn notifier --interval 10` | `cairn.notifier` (egress NetworkPolicy) | Needs outbound 443 (Slack/Teams) and 587/465 if SMTP is used |
| `cairn-consumer-ui` | nginx-unprivileged :8080 | `cairn.consumerUi` (routed at `/cairn-ui` by `core.ingress`) | **Must** share the Cairn Core UI's origin |
| cairn-web | `CAIRN_CORE_API_HOST=cairn-core-proxy`, `CAIRN_CORE_API_PORT=8890` (MFE config baked in) | `core.web` (points at `<release>-core-proxy` when `cairn.coreProxy.enabled`; MFE override via `core.web.mfeConfigOverride`) | Only the frontend is re-pointed. Cairn services and ingestion keep talking to Cairn Core directly. |

Startup order (compose): `cairn-core` + `cairn-pdp` → `cairn-core-proxy` → `cairn-web` →
`cairn-bootstrap` (mints the Cairn PAT) → other Cairn services. The proxy deliberately does **not** wait for
`cairn-bootstrap`, because bootstrap waits for the frontend and that would create a cycle. The proxy picks
up the token file (`CAIRN_CORE_TOKEN_FILE`) once bootstrap has written it.

## Secrets

| Variable | Required | Helm source |
|---|---|---|
| `CAIRN_ACTION_LINK_SECRET` | yes | `cairn-secrets/action-link-secret` |
| `CAIRN_EMBED_SIGNING_SECRET` | yes | `cairn-secrets/embed-signing-secret` |
| `CAIRN_SESSION_SIGNING_KEY` (proxy only) | yes | `cairn-core-secrets/session-signing-key` (the key Cairn Core signs sessions with), used read-only |
| `CAIRN_SLACK_BOT_TOKEN`, `CAIRN_SLACK_SIGNING_SECRET` | if Slack is enabled | `cairn-slack/{bot-token,signing-secret}` |
| `CAIRN_TEAMS_WEBHOOK_URL` | if Teams is enabled | `cairn-teams/webhook-url` |
| `CAIRN_SMTP_URL` | if email is enabled | `cairn-smtp/smtp-url` |
| `CAIRN_EMBED_ALLOWED_ORIGINS` / `cairn.embeds.allowedOrigins` | for embeds | plain config, which becomes CSP `frame-ancestors` |

```bash
kubectl create secret generic cairn-secrets ... \
  --from-literal=action-link-secret=$(openssl rand -hex 32) \
  --from-literal=embed-signing-secret=$(openssl rand -hex 32)
helm upgrade --install cairn helm/cairn -f helm/cairn/values.yaml \
  -f helm/cairn/values-active-metadata.yaml
```

## Slack

1. Create the app from [`slack/app-manifest.yaml`](slack/app-manifest.yaml). Replace
   `https://cairn.example.com` with your `CAIRN_PUBLIC_URL`. Slack must be able to reach
   `/cairn/api/integrations/slack/*` over public HTTPS.
2. Install the app to the workspace, then copy the **Bot User OAuth Token** and the **Signing
   Secret** into the env/secret listed above.
3. Invite the bot to the channels that receive domain routes (`/invite @Cairn Core`). DMs need no invite
   (`im:write`).
4. Users are mapped by email (`users:read.email`), so a Slack user's email must match their Cairn Core
   `corpUserInfo.email`. Unmapped users get an ephemeral "link your account" reply, and no action is
   executed.
5. Verify the setup from Cairn with `POST /cairn/api/notify/test {"channel": "slack"}`.

Every request is signature-checked (`v0` HMAC-SHA256, at most 300 s clock skew). Configure the
ingress timeout to 3 s or more for these paths; Slack retries after 3 s.

## Microsoft Teams (Workflows webhook)

Teams uses incoming webhooks with Adaptive Cards. Buttons in the cards are **signed one-click
links** (`/cairn/api/notify/actions/<token>`) that open a confirmation page and require a Cairn Core
login, so no Teams bot registration is needed.

1. In Teams, open the target channel → **⋯** → **Workflows** → template **"Post to a channel when a
   webhook request is received"**.
2. Name it (e.g. *Cairn Core alerts*), choose the team and channel, and finish. Copy the generated
   HTTP POST URL (`https://…logic.azure.com/…` or `…environment.api.powerplatform.com/…`).
3. Set it as `CAIRN_TEAMS_WEBHOOK_URL` (the default route), or add per-domain routes in **Workspace →
   Settings → Notifications** (`PUT /cairn/api/notify/routes`).
4. Test the route with `POST /cairn/api/notify/test {"channel": "teams"}`.

Webhook URLs are bearer credentials, so store them only in secrets.

## Embeds

* The edge serves `/cairn/api/embed/*` **without** `X-Frame-Options`. `cairn-api` sends
  `Content-Security-Policy: frame-ancestors <CAIRN_EMBED_ALLOWED_ORIGINS>`, and an empty value means
  embeds can only be framed same-origin.
* The web-component SDK loads from `${CAIRN_PUBLIC_URL}/cairn-ui/embed/sdk.js`, served with
  `Cross-Origin-Resource-Policy: cross-origin`.
* With Helm, `cairn.embeds.allowedOrigins` must also be allowed by your ingress (don't inject a
  global `X-Frame-Options` for `/cairn/api/embed/`).

## Validating

```bash
docker compose -f docker-compose.cairn.yml config -q
helm lint helm/cairn -f helm/cairn/values-active-metadata.yaml
helm template cairn helm/cairn --kube-version 1.30.0 \
  -f helm/cairn/values-active-metadata.yaml | kubeconform -strict -ignore-missing-schemas
```
