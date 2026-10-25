# DataHub trust embeds for BI tools and portals

The trust signals DataHub has for an asset (certification, data quality, freshness, owners, lineage)
can be shown wherever people look at data. Each surface below uses one of two building blocks, both
served by the DHE API and the `dhe-consumer-ui` image:

| Building block | URL | Use when |
|---|---|---|
| Web components | `<script src="https://DATAHUB_HOST/dhe-ui/embed/sdk.js" defer>` then `<dhe-asset-card>`, `<dhe-quality-badge>`, `<dhe-lineage>` | You control the HTML (portals, wikis, custom apps, Looker extensions) |
| iframe page | `https://DATAHUB_HOST/dhe/api/embed/v1/frame/{card,quality,lineage}?urn=…&token=…[&theme=dark]` | The tool can only embed a URL (Tableau, Power BI, Looker tiles) |

**Tokens.** Every embed carries a read-only token scoped to specific URNs and to `card`, `quality` or
`lineage`, with an expiry and, optionally, the origin allowed to frame it. Create tokens in DataHub →
Workspace → Settings → Embeds (or `POST /dhe/api/embed/v1/tokens`); the builder returns both the
HTML snippet and the iframe URL. Revoke a token there and the embed shows *“This embed link has
expired or was revoked.”* Tokens never grant search or write access.

**Framing.** Add each BI origin to `DHE_EMBED_ALLOWED_ORIGINS` (e.g.
`https://acme.looker.com,https://app.powerbi.com,https://tableau.acme.com`). The API sends it as
`Content-Security-Policy: frame-ancestors`, and the edge proxy allows framing only on `/dhe/api/embed/`.

- [Tableau](tableau/README.md) · [Looker](looker/README.md) · [Power BI](powerbi/README.md) · [Portals and wikis](portal/README.md)
