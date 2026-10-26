---
title: Integrations
order: 7
---
# Integrations

## Notifications

Cairn notifies people in the workspace inbox and, per their settings, in Slack, Microsoft Teams or
email. Routes per domain or event type are configured in **Workspace → Settings → Notifications**;
generic webhooks receive the same events as JSON.

| Channel | Setup |
|---|---|
| Slack | install the Cairn Slack app; set `CAIRN_SLACK_BOT_TOKEN` and `CAIRN_SLACK_SIGNING_SECRET` |
| Microsoft Teams | add a Workflows webhook and set `CAIRN_TEAMS_WEBHOOK_URL` or a per-domain route |
| Email | set `CAIRN_SMTP_URL` |
| Webhook | add a route with a URL |

## Actions in chat

Approvals (access requests, certifications) carry signed one-click buttons. In Slack, `/cairn`
searches the catalog as the linked user. People are matched to their Cairn account by email;
unmatched users are refused.

## Embeds

Show trust information inside BI tools and portals:

* **Web components**: load `/cairn-ui/embed/sdk.js`, then use `<cairn-asset-card>`,
  `<cairn-quality-badge>` and `<cairn-lineage>`.
* **Embeddable pages** for tools that only take a URL:
  `/cairn/api/embed/v1/frame/{card,quality,lineage}?urn=…&token=…`.

Embed tokens are scoped to assets, bound to the origins in `CAIRN_EMBED_ALLOWED_ORIGINS` and can be
revoked. Packaged guides exist for Tableau, Looker and Power BI.
