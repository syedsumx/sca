# Portals, wikis and custom apps

Include the SDK once per page, then place the elements where you need them. They render in a shadow
DOM (no CSS leaks either way), fetch with the token only (no cookies), and re-render when their
attributes change.

```html
<script src="https://DATAHUB_HOST/aegis-ui/embed/sdk.js" defer></script>

<aegis-asset-card    urn="URN" token="TOKEN"></aegis-asset-card>
<aegis-quality-badge urn="URN" token="TOKEN"></aegis-quality-badge>
<aegis-lineage       urn="URN" token="TOKEN" depth="2" theme="dark"></aegis-lineage>
```

| Attribute | Meaning |
|---|---|
| `urn` | DataHub URN the token was issued for |
| `token` | Embed token (Workspace → Settings → Embeds) |
| `base-url` | Aegis API base. Defaults to `data-aegis-base` on the script tag, else `<script origin>/aegis/api` |
| `theme` | `light` (default) or `dark` |
| `depth` | Lineage hops each way (`aegis-lineage` only, default 2) |

If the SDK is served from a different host than the API, set it once:
`<script src="https://cdn.example.com/aegis/sdk.js" data-aegis-base="https://DATAHUB_HOST/aegis/api" defer>`.

**Confluence / Notion / SharePoint** usually allow only iframes. Use the iframe URL from the embed
builder (`/aegis/api/embed/v1/frame/card?urn=…&token=…`) in their iframe/embed macro and add the wiki's
origin to `AEGIS_EMBED_ALLOWED_ORIGINS`.

**Styling.** The elements expose CSS custom properties on the host: `--aegis-fg`, `--aegis-bg`,
`--aegis-border`, `--aegis-accent`.
