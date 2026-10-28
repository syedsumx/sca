---
title: Integrations
order: 7
---
# Integrations

## Notifications

Cairn notifies people in the workspace inbox and, per their settings, in Slack, Microsoft Teams or
email. Routes per domain or event type are configured in **Workspace → Workspace settings → Notifications**;
generic webhooks receive the same events as JSON.

| Channel | Setup |
|---|---|
| Slack | install the Cairn Slack app; set `CAIRN_SLACK_BOT_TOKEN` and `CAIRN_SLACK_SIGNING_SECRET` |
| Microsoft Teams | add a Workflows webhook and set `CAIRN_TEAMS_WEBHOOK_URL` or a per-domain route |
| Email | set `CAIRN_SMTP_URL` |
| Webhook | add a route with a URL |

What the notifier will and will not call:

* **Slack** messages are checked against Slack's Block Kit limits before they are sent; long titles
  and bodies are shortened rather than rejected by Slack, and mentions in catalog text are escaped
  so a description cannot ping a channel. When Slack rate-limits Cairn for a few seconds the message
  waits; longer limits are retried on the next delivery round. For GovSlack or an egress proxy set
  `CAIRN_SLACK_API_URL`. Replies to button clicks go only to `hooks.slack.com`
  (`CAIRN_SLACK_RESPONSE_HOSTS` adds origins).
* **Teams** webhook URLs, including the personal ones people paste into their settings, must be
  https on a Microsoft webhook host (`.webhook.office.com`, `.logic.azure.com`,
  `.powerplatform.com`, `.powerautomate.com`). Anything else is refused when it is saved and again
  when it is called. `CAIRN_TEAMS_WEBHOOK_HOSTS` replaces the list (host suffixes, or exact origins
  such as a corporate proxy).
* **Webhooks** never reach loopback, link-local (including cloud metadata at 169.254.169.254) or
  unspecified addresses, and do not follow redirects. Private networks are allowed for on-premises
  receivers; `CAIRN_WEBHOOK_ALLOW_PRIVATE=false` limits webhooks to public addresses.

## Actions in chat

Approvals (access requests, certifications) carry signed one-click buttons. In Slack, `/cairn`
searches the catalog as the linked user. People are matched to their Cairn account by email;
unmatched users are refused. In Teams and email the buttons are links signed for the recipient:
they work once, only for the person they were sent to, and ask for confirmation in the browser.
If someone already decided the request elsewhere, the button says so instead of acting twice.

## Embeds

Show trust information inside BI tools and portals:

* **Web components**: load `/cairn-ui/embed/sdk.js`, then use `<cairn-asset-card>`,
  `<cairn-quality-badge>` and `<cairn-lineage>`.
* **Embeddable pages** for tools that only take a URL:
  `/cairn/api/embed/v1/frame/{card,quality,lineage}?urn=…&token=…`.

Embed tokens are scoped to assets, bound to the origins in `CAIRN_EMBED_ALLOWED_ORIGINS` and can be
revoked. A page on any other origin gets neither: the browser refuses to frame the embeddable pages
(`Content-Security-Policy: frame-ancestors`) and refuses the web components' data requests (CORS). Packaged guides exist for Tableau, Looker and Power BI.

## Browser extension for BI tools

The **Cairn for BI** Chrome extension (Manifest V3, in `extension/`) shows the catalog card in a side panel for the Looker, Tableau or Power BI dashboard you have open. The card shows the description, owners, certification, data quality and checks, and an **Open in Cairn** link.

- **Pages it recognises:**
  - Looker dashboards, Looks and Explores on `*.looker.com`,
  - Tableau views and workbooks on Tableau Cloud and `*.tableau.com`, including routes in `#/site/…`,
  - Power BI reports and dashboards on `app.powerbi.com`.

  It follows navigation inside these single-page apps.
- **How it finds the asset:** it looks up the page through `GET /cairn/api/embed/v1/context?url=…`, using the person's own Cairn session. They see only what they may see, and a sign-in link when they aren't signed in. Query strings and in-page anchors are ignored, so filters and tiles don't stop a match.
- **What it reads:** only the page address, never the page content.
- **Custom domains:** a self-hosted Looker or Tableau Server, for example, are added in the extension's options. The browser asks for access to each host you add, and to nothing else.

**Rolling it out.** `node extension/build.mjs --cairn-url https://cairn.example.com` builds `extension/dist/` with your Cairn address and its host permission built in, ready to load unpacked, publish privately, or force-install through Chrome enterprise policy. Without `--cairn-url`, people enter the address in the options. `npm test` in `extension/` runs its unit tests.
