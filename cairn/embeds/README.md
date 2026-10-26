# Cairn Core trust embeds for BI tools and portals

The trust signals Cairn Core has for an asset (certification, data quality, freshness, owners, lineage)
can be shown wherever people look at data. Each surface below uses one of two building blocks, both
served by the Cairn API and the `cairn-consumer-ui` image:

| Building block | URL | Use when |
|---|---|---|
| Web components | `<script src="https://CAIRN_HOST/cairn-ui/embed/sdk.js" defer>` then `<cairn-asset-card>`, `<cairn-quality-badge>`, `<cairn-lineage>` | You control the HTML (portals, wikis, custom apps, Looker extensions) |
| iframe page | `https://CAIRN_HOST/cairn/api/embed/v1/frame/{card,quality,lineage}?urn=…&token=…[&theme=dark]` | The tool can only embed a URL (Tableau, Power BI, Looker tiles) |

**Tokens.** Every embed carries a read-only token scoped to specific URNs and to `card`, `quality` or
`lineage`, with an expiry and, optionally, the origin allowed to frame it. Create tokens in Cairn Core →
Workspace → Settings → Embeds (or `POST /cairn/api/embed/v1/tokens`); the builder returns both the
HTML snippet and the iframe URL. Revoke a token there and the embed shows *“This embed link has
expired or was revoked.”* Tokens never grant search or write access.

**Framing.** Add each BI origin to `CAIRN_EMBED_ALLOWED_ORIGINS` (e.g.
`https://acme.looker.com,https://app.powerbi.com,https://tableau.acme.com`). The API sends it as
`Content-Security-Policy: frame-ancestors`, and the edge proxy allows framing only on `/cairn/api/embed/`.

- [Tableau](tableau/README.md) · [Looker](looker/README.md) · [Power BI](powerbi/README.md) · [Portals and wikis](portal/README.md)
